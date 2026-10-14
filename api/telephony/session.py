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
the ear heard and what the line finished playing. One worker (`run`) takes the
events strictly one after another, so the state below is touched by one thread
only. The core is synchronous and runs in that worker; while it thinks, the
events of the call wait in the queue.

**What the core asks for comes first.** Say, transfer and hangup (through
`Lines`) are kept while a turn runs and carried out the moment it returns, in
the order they were asked, before anything that queued up behind the turn. So a
read-back protects itself before a "Ja" that was already waiting is looked at:
the core has "said" the read-back by then, the guest has not heard a word of
it.

**Timers run on the time of the events.** A timer fires only when nothing
older is waiting in the queue: a key, an answer or a report from the line that
arrived in time is handled as in time, however late the worker gets to it.

**What ends a session.** `outcome` says how: `hangup` (we ended the call),
`transfer` (handed to `transfer_target`), `closed` (the caller hung up) or
`failed`. Only `hangup` lets the dialplan end the call; after anything else it
rings the team (docs/20 §5), so a crash here never leaves a caller on a dead
line. A call whose voice failed is never hung up by us.

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
# An entry on the keypad is handed over at this length whatever the gaps: keys
# without end would never begin a turn. No number on a telephone is longer.
KEYPAD_MAX_DIGITS = 20
# How long the line may take to report the end of playback, beyond the length
# of the audio itself. Without the report nobody knows what the caller heard:
# it is a failure of the call and goes to the team, and a transfer or hangup
# that waited for it happens all the same (CLAUDE.md §2 rules 3 and 5).
PLAYED_GRACE_SECONDS = 5.0
# How long the mouth may take for one sentence. A hung engine would hold the
# one worker of the call, and with it the caller's hangup and the way to the
# team (rule 5).
SPEAK_SECONDS = 10.0
# How long the ear may take from its last word that the caller is speaking to
# the finished utterance. An ear that begins an utterance and never ends it
# leaves the call waiting for nothing: a failure (rule 5).
UTTERANCE_SECONDS = 10.0
# And how long one utterance may be at all, whatever the ear reports: a
# detector stuck in "speech" would keep the call open for ever.
UTTERANCE_LIMIT_SECONDS = 60.0
# Raw 16 bit mono, as the line is dialled (`adapters/asterisk.py`).
SAMPLE_RATE = 16000

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
        speak_seconds: float = SPEAK_SECONDS,
        sample_rate: int = SAMPLE_RATE,
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
        self._speak_seconds = speak_seconds
        self._sample_rate = sample_rate
        self._clock = clock
        self._inbox: queue.SimpleQueue[tuple[str, float, Any]] = queue.SimpleQueue()
        # The time of an event and its place in the queue are taken together.
        self._posting = threading.Lock()
        # What the core asked for in the turn that is running, and the thread
        # that runs it. None outside a turn.
        self._asked: list[tuple[str, Any]] | None = None
        self._turn_thread: int | None = None

        self._answered = False
        self._mark = 0
        # One entry per sentence that is queued on the line and not reported as
        # played: may it be interrupted?
        self._outstanding: list[bool] = []
        self._playing_since: float | None = None
        # When what is queued ends, by the length of its audio.
        self._playing_until: float | None = None
        # An utterance or a key that ended at or before this is no answer: it
        # fell into a sentence that may not be interrupted. Infinite while one
        # is still playing.
        self._protected_until = -math.inf
        self._ending: tuple[str, str | None] | None = None
        self._digits = ""
        self._digits_due: float | None = None
        self._silence_due: float | None = None
        # The ear said that the caller is speaking and owes the finished
        # utterance by then; and when this utterance began.
        self._utterance_due: float | None = None
        self._utterance_began: float | None = None
        self._unheard = 0
        # The voice failed on this call, and the core knows.
        self._failed = False
        self._failure_told = False
        # The mouth failed on this call: it is not asked again.
        self._mouth_failed = False

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

    def broken(self) -> None:
        """The line cannot go on: the telephone system sent what the adapter
        does not understand. A failure of the voice layer like a failing
        engine, not a caller who hung up (T-1.11)."""
        self._post("broken")

    # -- what the core asks for (through `Lines`) --------------------------------------

    def say(self, text: str, interruptible: bool = False) -> None:
        self._ask("say", (text, interruptible))

    def expect(self, terms: list[str]) -> None:
        self._ask("expect", list(terms))

    def transfer(self, target: str) -> None:
        self._ask("transfer", target)

    def hangup(self) -> None:
        self._ask("hangup")

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
                    break  # no timer was due, or what it caused is handled
                continue
            self._step(event)

    def _post(self, kind: str, payload: Any = None) -> None:
        # Under one lock: the timers run on the time of the events, so an
        # event must never stand behind one that happened after it.
        with self._posting:
            self._inbox.put((kind, self._clock(), payload))

    def _ask(self, kind: str, payload: Any = None) -> None:
        """From the core. Inside a turn it is kept and carried out when the
        turn returns, before anything that queued up behind the turn; outside
        one it is an event like any other."""
        if self._asked is not None and threading.get_ident() == self._turn_thread:
            self._asked.append((kind, payload))
        else:
            self._post(kind, payload)

    def _until_next_timer(self) -> float | None:
        due = [
            d
            for d in (
                self._digits_due,
                self._silence_due,
                self._utterance_due,
                self._played_due(),
            )
            if d is not None
        ]
        return max(0.0, min(due) - self._clock()) if due else None

    def _step(self, event: tuple[str, float, Any] | None) -> None:
        try:
            # Timers first, and only up to the time of this event: what was
            # due before it happened before it, and nothing that is due later
            # overtakes an event that waited in the queue.
            self._timers(event[1] if event is not None else self._clock())
            if event is not None and not self.over:
                kind, at, payload = event
                getattr(self, f"_on_{kind}")(at, payload)
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
            # The caller is speaking: no silence, and the ear owes the end of
            # this utterance. If that never comes, nothing else would.
            self._silence_due = None
            if self._utterance_began is None:
                self._utterance_began = at
            self._utterance_due = min(
                at + UTTERANCE_SECONDS,
                self._utterance_began + UTTERANCE_LIMIT_SECONDS,
            )
            if (
                self._may_interrupt()
                # Speech that is older than the answer on the line stops
                # nothing: it began while the core was still working. By the
                # time of the audio, not of the report: the ear may be late.
                and self._playing_since is not None
                and heard.ended_at >= self._playing_since
                and len(heard.text.split()) >= BARGE_IN_WORDS
            ):
                self._stop_playback()
            return
        self._utterance_due = self._utterance_began = None
        if not heard.speech:
            # Words without speech energy: line noise, never an answer.
            self._wait_for_the_caller(at)
            return
        if not heard.text.strip():
            # Speech the ear made nothing of. Once is a cough; again and the
            # ear does not hear this caller (docs/20 §4).
            self._unheard += 1
            if self._unheard >= UNHEARD_LIMIT:
                self._voice_failed()
            else:
                self._wait_for_the_caller(at)
            return
        self._unheard = 0
        if heard.ended_at <= self._protected_until:
            # Said into a sentence that may not be interrupted.
            self._wait_for_the_caller(at)
            return
        if self._playing_since is not None and heard.ended_at >= self._playing_since:
            # Everything still playing may be interrupted, or the line above
            # had dropped the utterance. What was said before the answer began
            # to play is the next turn and does not stop it.
            self._stop_playback()
        self._turn(lambda: self._events.on_user_turn(self.session_id, heard.text))

    def _on_key(self, at: float, digit: str) -> None:
        if not self._listening() or at <= self._protected_until:
            return
        if self._playing_since is not None and at >= self._playing_since:
            # A key that was pressed before the answer was on the line stops
            # nothing, like speech.
            self._stop_playback()
        self._silence_due = None
        if digit == END_KEY:
            if self._digits:
                self._hand_over_digits()
            else:
                self._wait_for_the_caller(at)  # a hash alone hands nothing over
        else:
            self._digits += digit
            if len(self._digits) >= KEYPAD_MAX_DIGITS:
                self._hand_over_digits()
            else:
                self._digits_due = at + self._keypad_gap_seconds

    def _on_played(self, at: float, mark: int) -> None:
        if mark != self._mark or not self._outstanding:
            return  # an older sentence, or one that was dropped by a flush
        protected = not all(self._outstanding)
        self._outstanding = []
        self._playing_since = self._playing_until = None
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
        if not text.strip():
            return  # nothing to say: not spoken, and it protects nothing
        if self._mouth_failed:
            # The outage sentence after a failure of the mouth. Asking the
            # engine again would cost the caller a second deadline of silence
            # before the team is rung.
            logger.warning("the mouth failed on this call, the sentence is not spoken")
            return
        try:
            audio = self._synthesize(text)
        except Exception:
            logger.exception("the mouth failed")
            self._mouth_failed = True
            # The outage sentence of a failure is best effort: the transfer
            # behind it is what counts.
            self._voice_failed()
            return
        if not self._answered:
            self._line.answer()
            self._answered = True
        self._mark += 1
        # Now, not when the sentence was asked for: the mouth took its time.
        # What the caller said meanwhile was no barge-in, and the audio cannot
        # be through before it was queued, however long ago the sentence
        # before it ended.
        now = self._clock()
        if not self._outstanding:
            self._playing_since = now
        self._playing_until = max(self._playing_until or now, now) + len(audio) / (
            2 * self._sample_rate
        )
        self._outstanding.append(interruptible)
        if not interruptible:
            self._protected_until = math.inf
        self._silence_due = None
        self._line.play(audio, self._mark)

    def _on_expect(self, at: float, terms: list[str]) -> None:
        self._ear.expect(terms)

    def _on_transfer(self, at: float, target: str) -> None:
        self._queue_ending(("transfer", target))

    def _on_hangup(self, at: float, _: None) -> None:
        self._queue_ending(("hangup", None))

    def _on_closed(self, at: float, _: None) -> None:
        self._end("closed")

    def _on_broken(self, at: float, _: None) -> None:
        self._voice_failed()

    def _timers(self, now: float) -> None:
        played_due = self._played_due()
        if played_due is not None and now >= played_due:
            logger.error(
                "the line did not report the end of playback",
                extra={"extra": {"session_id": self.session_id}},
            )
            if self._ending is not None and self._ending[0] == "transfer":
                # The team does not wait for a report that will not come.
                self._stop_playback()
                self._perform_ending()
            else:
                # Whether the caller heard the sentence is not known. Never
                # counted as heard: a read-back would be answered by a "Ja"
                # to nothing (rule 3), and a hangup behind a farewell nobody
                # may have heard would tell the dialplan that all is well.
                self._voice_failed()
        if self.over:
            return
        if self._utterance_due is not None and now >= self._utterance_due:
            logger.error(
                "the ear began an utterance and did not finish it",
                extra={"extra": {"session_id": self.session_id}},
            )
            self._voice_failed()
            return
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

    def _played_due(self) -> float | None:
        """When the session stops waiting for the line's report."""
        if self._playing_until is None:
            return None
        return self._playing_until + PLAYED_GRACE_SECONDS

    def _stop_playback(self) -> None:
        self._outstanding = []
        self._playing_since = self._playing_until = None
        self._line.flush()

    def _synthesize(self, text: str) -> bytes:
        """The audio of one sentence, or an exception: the engine's own, a
        timeout when it does not finish, or an error when it made nothing.

        The engine runs on a thread of its own, and one that hangs is left
        behind there: audio that comes too late is thrown away and does no
        harm. The line is not treated like this. A command to the telephone
        system that is given up on could still be carried out later, a hangup
        while the team is being rung; so the line must not block at all
        (`speech.Line`)."""
        done: queue.SimpleQueue[tuple[bytes, Exception | None]] = queue.SimpleQueue()

        def work() -> None:
            try:
                done.put((b"".join(self._mouth.speak(text)), None))
            except Exception as exc:  # noqa: BLE001 - raised again by the worker
                done.put((b"", exc))

        threading.Thread(target=work, daemon=True).start()
        try:
            audio, error = done.get(timeout=self._speak_seconds)
        except queue.Empty:
            raise TimeoutError("the mouth did not finish the sentence") from None
        if error is not None:
            raise error
        if not audio:
            # An empty stream without an error. Queued, the line would report
            # it as played, and a read-back nobody heard would count as heard.
            raise RuntimeError("the mouth made no audio for the sentence")
        return audio

    def _turn(self, deliver: Callable[[], None]) -> None:
        """Hand something to the core, then carry out what it asked for, in
        order and before the next event. A turn inside a turn (the core is told
        of a failure while its sentences are being spoken) keeps its own list."""
        self._silence_due = None
        before = (self._asked, self._turn_thread)
        asked: list[tuple[str, Any]] = []
        self._asked, self._turn_thread = asked, threading.get_ident()
        try:
            deliver()
        finally:
            self._asked, self._turn_thread = before
        for kind, payload in asked:
            if self.over:
                return
            getattr(self, f"_on_{kind}")(self._clock(), payload)
        if self.over:
            return
        if self._failed and self._ending is None:
            # The voice failed and the core sends the call nowhere (it may
            # have closed it already). The session listens to nothing any
            # more, so it must not stay on the line: ended as failed, the
            # dialplan rings the team (docs/20 §5).
            self._end("failed")
        else:
            self._wait_for_the_caller(self._clock())

    def _hand_over_digits(self) -> None:
        digits, self._digits, self._digits_due = self._digits, "", None
        if digits:
            self._turn(lambda: self._events.on_dtmf(self.session_id, digits))

    def _wait_for_the_caller(self, at: float) -> None:
        if (
            self._listening()
            and not self._outstanding
            and not self._digits
            # The ear still owes an utterance: the caller is not silent.
            and self._utterance_due is None
        ):
            self._silence_due = at + self._silence_seconds

    def _voice_failed(self) -> None:
        """Ear or mouth failed, or the line does not say what it played: once,
        the core is told, and it sends the call to the team. What was queued
        is dropped, the caller hears the outage sentence next."""
        if self._failed:
            return
        self._failed = True
        self._silence_due = self._utterance_due = self._utterance_began = None
        self._digits, self._digits_due = "", None
        if self._ending is not None and self._ending[0] == "hangup":
            # A hangup that waited for its farewell is not carried out on a
            # failed call.
            self._ending = None
        if self._outstanding:
            try:
                self._stop_playback()
            except Exception:
                # The line is broken as well. The core still has to close its
                # call, and the dialplan rings the team.
                logger.exception("what was queued could not be dropped")
        self._failure_told = True
        self._turn(lambda: self._events.on_voice_failure(self.session_id))

    def _queue_ending(self, ending: tuple[str, str | None]) -> None:
        if self._failed and ending[0] == "hangup":
            # A call whose voice failed is not hung up by us, whoever asked
            # for it and when: the transfer, or the dialplan, takes it to the
            # team (rule 5).
            return
        # The first one stands, except that a transfer replaces a hangup that
        # still waits: a failure during the farewell goes to the team.
        if self._ending is not None and (self._ending[0], ending[0]) != (
            "hangup",
            "transfer",
        ):
            return
        self._ending = ending
        self._silence_due = self._utterance_due = self._utterance_began = None
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
        # The core closes its call log: the caller hung up, or the session
        # broke, which it logs as a failure. Its outage sentence and transfer
        # reach nobody then; the dialplan rings the team. After our own hangup
        # or transfer the core has closed the call already.
        try:
            if outcome == "closed":
                self._events.on_call_ended(self.session_id)
            elif outcome == "failed" and not self._failure_told:
                self._failure_told = True
                self._events.on_voice_failure(self.session_id)
        except Exception:
            logger.exception("the core could not be told how the call ended")


class Lines:
    """`port.TelephonyPort` over the live sessions: what the core asks of a
    call goes to that call's session."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, CallSession] = {}

    def add(self, session: CallSession) -> None:
        """Raises `ValueError` for a call that is live already: a second
        session would take the first one's place, and with it what the core
        asks of that call (T-1.11)."""
        with self._lock:
            if session.session_id in self._sessions:
                raise ValueError(f"call {session.session_id!r} is live already")
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

    def expect(self, session_id: str, terms: list[str]) -> None:
        session = self._get(session_id)
        if session is not None:
            session.expect(terms)

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
