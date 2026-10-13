"""Call session of the own voice layer: one per call, provider neutral (docs/20 §4).

A platform used to hide what happens between the caller's audio and a finished
sentence of text. With our own layer that is this module: it feeds the ear,
decides when what the ear heard is the caller's turn, plays what the core says
through the mouth, and keeps the rules that protect a booking on the wire:

- a read-back is never interrupted, and an utterance is its answer only if it
  ends after the read-back was played to the end (CLAUDE.md §2 rule 3),
- words without speech energy are line noise, never a yes,
- queued speech is played in order, and a transfer or a hangup waits for the
  end of the last sentence,
- a failure of ear or mouth is a failure of the call and ends with the team
  (rule 5).

**One worker, one queue.** Everything that happens on a call is put into the
session's queue with the time it happened: audio and keys from the line, what
the ear heard, what the line finished playing, and what the core asks for. One
worker (`run`) takes the events strictly one after another, so the state below
is touched by one thread only. The core is synchronous and runs in that worker;
while it thinks, the events of the call wait in the queue. What the core asks of
the line through `Lines` (say, transfer, hangup) only enqueues, so it happens
after the turn that asked for it and in the order it was asked.

**What ends a session.** `outcome` says how: `hangup` (we ended the call),
`transfer` (handed to `transfer_target`), `closed` (the caller hung up) or
`failed`. Only `hangup` lets the dialplan end the call; after anything else it
rings the team (docs/20 §5), so a crash here never leaves a caller on a dead
line.

The media adapter (`adapters/asterisk.py`, T-1.11) creates a session per call,
runs its worker and feeds it; the ports for ear, mouth and line are in
`telephony/speech.py`.
"""

import math
import queue
import threading
import time
from collections.abc import Callable
from typing import Any, Literal

from api.core.logging import get_logger
from api.telephony.port import CallEvents
from api.telephony.speech import Ear, Heard, Line, Mouth

# A partial of this many words stops a sentence that may be interrupted. One
# word is a cough or a "hm" as often as it is an answer.
BARGE_IN_WORDS = 2
# Speech energy without a transcript, this many times in a row, is a failure of
# the ear. Once can be a cough the engine made nothing of.
UNHEARD_LIMIT = 2
# Ends an entry on the keypad.
END_KEY = "#"
KEYPAD_GAP_SECONDS = 3.0

Outcome = Literal["open", "hangup", "transfer", "closed", "failed"]

logger = get_logger(__name__)


class CallSession:
    def __init__(
        self,
        session_id: str,
        *,
        line: Line,
        ear: Ear,
        mouth: Mouth,
        events: CallEvents,
        silence_seconds: float,
        caller_id: str | None = None,
        keypad_gap_seconds: float = KEYPAD_GAP_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.session_id = session_id
        self.caller_id = caller_id
        self.outcome: Outcome = "open"
        self.transfer_target: str | None = None
        self._line = line
        self._ear = ear
        self._mouth = mouth
        self._events = events
        self._silence_seconds = silence_seconds
        self._keypad_gap_seconds = keypad_gap_seconds
        self._clock = clock
        self._inbox: queue.SimpleQueue[tuple[str, float, Any]] = queue.SimpleQueue()

        self._answered = False
        self._mark = 0
        # One entry per sentence that is queued on the line and not reported as
        # played: may it be interrupted?
        self._outstanding: list[bool] = []
        self._playing_since: float | None = None
        # An utterance or a key that ended at or before this is no answer: it
        # fell into a sentence that may not be interrupted. Infinite while one
        # is still playing.
        self._protected_until = -math.inf
        self._ending: tuple[str, str | None] | None = None
        self._digits = ""
        self._digits_due: float | None = None
        self._silence_due: float | None = None
        self._unheard = 0
        self._failed = False

    # -- what happens on the call: any thread, only enqueues ---------------------------

    def start(self) -> None:
        """The call arrived. Nothing is answered yet: the first sentence does that."""
        self._post("start")

    def audio(self, frame: bytes) -> None:
        self._post("audio", frame)

    def key(self, digit: str) -> None:
        self._post("key", digit)

    def played(self, mark: int) -> None:
        """The line played everything up to `mark` to the caller."""
        self._post("played", mark)

    def closed(self) -> None:
        """The caller's leg is gone."""
        self._post("closed")

    # -- what the core asks for (through `Lines`): only enqueues -----------------------

    def say(self, text: str, interruptible: bool = False) -> None:
        self._post("say", (text, interruptible))

    def transfer(self, target: str) -> None:
        self._post("transfer", target)

    def hangup(self) -> None:
        self._post("hangup")

    # -- the worker ---------------------------------------------------------------------

    @property
    def over(self) -> bool:
        return self.outcome != "open"

    def run(self) -> None:
        """The worker of this call: one event after another until the call is
        over, and the timers in between."""
        while not self.over:
            try:
                event = self._inbox.get(timeout=self._until_next_timer())
            except queue.Empty:
                event = None
            self._step(event)

    def drain(self) -> None:
        """Handle what is queued, then the timers, on the calling thread. For
        tests, which set the clock instead of waiting."""
        while not self.over:
            try:
                event = self._inbox.get_nowait()
            except queue.Empty:
                self._step(None)
                if self._inbox.empty():
                    break  # no timer was due, or what it asked for is handled
                continue
            self._step(event)

    def _post(self, kind: str, payload: Any = None) -> None:
        self._inbox.put((kind, self._clock(), payload))

    def _until_next_timer(self) -> float | None:
        due = [d for d in (self._digits_due, self._silence_due) if d is not None]
        return max(0.0, min(due) - self._clock()) if due else None

    def _step(self, event: tuple[str, float, Any] | None) -> None:
        try:
            if event is not None:
                kind, at, payload = event
                getattr(self, f"_on_{kind}")(at, payload)
            if not self.over:
                self._timers(self._clock())
        except Exception:
            # The core or the line raised. Nothing is hung up here: without an
            # explicit hangup the dialplan rings the team (docs/20 §5).
            logger.exception(
                "call session failed, the dialplan has to ring the team",
                extra={"extra": {"session_id": self.session_id}},
            )
            self._end("failed")

    # -- events -------------------------------------------------------------------------

    def _on_start(self, at: float, _: None) -> None:
        self._ear.open(lambda heard: self._post("heard", heard))
        self._turn(lambda: self._events.on_call_started(self.session_id))

    def _on_audio(self, at: float, frame: bytes) -> None:
        if not self._listening():
            return
        try:
            self._ear.feed(frame, at)
        except Exception:
            logger.exception("the ear failed")
            self._voice_failed()

    def _on_heard(self, at: float, heard: Heard) -> None:
        if not self._listening():
            return
        if not heard.final:
            self._caller_is_active(at)
            if self._may_interrupt() and len(heard.text.split()) >= BARGE_IN_WORDS:
                self._stop_playback()
            return
        if not heard.speech:
            return  # words without speech energy: line noise, never an answer
        if not heard.text.strip():
            # Speech the ear made nothing of. Once is a cough; again and the
            # ear does not hear this caller (docs/20 §4).
            self._unheard += 1
            if self._unheard >= UNHEARD_LIMIT:
                self._voice_failed()
            else:
                self._caller_is_active(at)
            return
        self._unheard = 0
        if heard.ended_at <= self._protected_until:
            return  # said into a sentence that may not be interrupted
        if self._playing_since is not None and heard.ended_at >= self._playing_since:
            # Everything still playing may be interrupted, or the line above
            # had dropped the utterance. What was said before the answer began
            # to play is the next turn and does not stop it.
            self._stop_playback()
        self._turn(lambda: self._events.on_user_turn(self.session_id, heard.text))

    def _on_key(self, at: float, digit: str) -> None:
        if not self._listening() or at <= self._protected_until:
            return
        if self._outstanding:
            self._stop_playback()
        self._silence_due = None
        if digit == END_KEY:
            self._hand_over_digits()
        else:
            self._digits += digit
            self._digits_due = at + self._keypad_gap_seconds

    def _on_played(self, at: float, mark: int) -> None:
        if mark != self._mark or not self._outstanding:
            return  # an older sentence, or one that was dropped by a flush
        protected = not all(self._outstanding)
        self._outstanding = []
        self._playing_since = None
        if protected:
            self._protected_until = at
        if self._ending is not None:
            self._perform_ending()
        else:
            self._wait_for_the_caller(at)

    def _on_say(self, at: float, payload: tuple[str, bool]) -> None:
        text, interruptible = payload
        if self._ending is not None:
            logger.warning("a sentence behind a transfer or hangup is not spoken")
            return
        try:
            audio = b"".join(self._mouth.speak(text))
        except Exception:
            logger.exception("the mouth failed")
            # The outage sentence of a failure is best effort: the transfer
            # behind it is what counts.
            self._voice_failed()
            return
        if not self._answered:
            self._line.answer()
            self._answered = True
        self._mark += 1
        if not self._outstanding:
            self._playing_since = at
        self._outstanding.append(interruptible)
        if not interruptible:
            self._protected_until = math.inf
        self._silence_due = None
        self._line.play(audio, self._mark)

    def _on_transfer(self, at: float, target: str) -> None:
        self._queue_ending(("transfer", target))

    def _on_hangup(self, at: float, _: None) -> None:
        self._queue_ending(("hangup", None))

    def _on_settled(self, at: float, _: None) -> None:
        """A turn is over and what it asked for is queued: if nothing is being
        said now, the silence counts from here."""
        self._wait_for_the_caller(at)

    def _on_closed(self, at: float, _: None) -> None:
        self._end("closed")

    def _timers(self, now: float) -> None:
        if self._digits_due is not None and now >= self._digits_due:
            self._hand_over_digits()
        if self._silence_due is not None and now >= self._silence_due:
            self._turn(lambda: self._events.on_silence(self.session_id))

    # -- pieces -------------------------------------------------------------------------

    def _listening(self) -> bool:
        """Once the call is going to the team or ending, nothing the caller
        says or presses is taken any more."""
        return not self._failed and self._ending is None

    def _may_interrupt(self) -> bool:
        """Only if every sentence still to be played may be interrupted: a
        flush drops them all, and a read-back behind a short "Gern." must be
        played to its end."""
        return bool(self._outstanding) and all(self._outstanding)

    def _stop_playback(self) -> None:
        self._line.flush()
        self._outstanding = []
        self._playing_since = None

    def _turn(self, deliver: Callable[[], None]) -> None:
        """Hand something to the core. What it asks for is queued behind this
        event; `settled` comes after it."""
        self._silence_due = None
        deliver()
        self._post("settled")

    def _hand_over_digits(self) -> None:
        digits, self._digits, self._digits_due = self._digits, "", None
        if digits:
            self._turn(lambda: self._events.on_dtmf(self.session_id, digits))

    def _wait_for_the_caller(self, at: float) -> None:
        if self._listening() and not self._outstanding and not self._digits:
            self._silence_due = at + self._silence_seconds

    def _caller_is_active(self, at: float) -> None:
        if self._silence_due is not None:
            self._silence_due = at + self._silence_seconds

    def _voice_failed(self) -> None:
        """Ear or mouth failed: once, the core is told, and it sends the call
        to the team. What was queued is dropped, the caller hears the outage
        sentence next."""
        if self._failed:
            return
        self._failed = True
        self._silence_due = None
        self._digits, self._digits_due = "", None
        if self._outstanding:
            self._stop_playback()
        self._events.on_voice_failure(self.session_id)

    def _queue_ending(self, ending: tuple[str, str | None]) -> None:
        # The first one stands, except that a transfer replaces a hangup that
        # still waits: a failure during the farewell goes to the team.
        if self._ending is not None and (self._ending[0], ending[0]) != (
            "hangup",
            "transfer",
        ):
            return
        self._ending = ending
        self._silence_due = None
        self._digits, self._digits_due = "", None
        if not self._outstanding:
            self._perform_ending()

    def _perform_ending(self) -> None:
        assert self._ending is not None
        kind, target = self._ending
        if kind == "transfer":
            assert target is not None
            self._line.transfer(target)
            self.transfer_target = target
            self._end("transfer")
        else:
            self._line.hangup()
            self._end("hangup")

    def _end(self, outcome: Outcome) -> None:
        if self.over:
            return
        self.outcome = outcome
        try:
            self._ear.close()
        except Exception:
            logger.exception("the ear could not be closed")
        if outcome in ("closed", "failed"):
            # The core closes its call log. After our own hangup or transfer it
            # has done so already.
            try:
                self._events.on_call_ended(self.session_id)
            except Exception:
                logger.exception("the core could not be told that the call ended")


class Lines:
    """`port.TelephonyPort` over the live sessions: what the core asks of a
    call goes to that call's session, which only enqueues it."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, CallSession] = {}

    def add(self, session: CallSession) -> None:
        with self._lock:
            self._sessions[session.session_id] = session

    def remove(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)

    def caller_id(self, session_id: str) -> str | None:
        session = self._get(session_id)
        return session.caller_id if session is not None else None

    def say(self, session_id: str, text: str, interruptible: bool = False) -> None:
        session = self._get(session_id)
        if session is None:
            logger.warning("a sentence for a call that is gone is not spoken")
            return
        session.say(text, interruptible)

    def transfer(self, session_id: str, target: str) -> None:
        session = self._get(session_id)
        if session is None:
            # No leg to hand over. The caller of the core must know.
            raise LookupError(f"no live call {session_id!r} to transfer")
        session.transfer(target)

    def hangup(self, session_id: str) -> None:
        session = self._get(session_id)
        if session is not None:
            session.hangup()

    def start_recording(self, session_id: str) -> None:
        """Nothing is recorded: not built, and not allowed before the legal
        check in docs/09 is ticked (CLAUDE.md §9)."""
        raise RuntimeError("recording is not part of the voice layer")

    def _get(self, session_id: str) -> CallSession | None:
        with self._lock:
            return self._sessions.get(session_id)
