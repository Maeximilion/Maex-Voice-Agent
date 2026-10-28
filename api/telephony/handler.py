"""Provider-neutral call handler: events from the line in, agent core and call log behind.

Every adapter feeds this one handler, so what happens on a call does not depend on
the provider (docs/11 §telephony). The code, not the model, says the AI disclosure
before anything else (docs/05 §6). The mode decides whether the agent answers at
all (docs/02 §4). Each turn runs through the same `agent/loop.py` as `sim/` and the
evals; when the conversation says transferred or over, the handler dials the team
or says goodbye and hangs up. It closes the call log when the adapter reports the
end of the leg (`on_call_ended`), not when it asks for it: the last sentence is
still playing then, and the line may still fail.

Every failure ends with the team (CLAUDE.md §2 rule 5): an exception anywhere in a
call, from the database, the model or the adapter, is answered with the outage
sentence and a transfer to the team extension. If even that transfer fails, the
error is raised to whoever fed the event, and the line in front of the handler
has to reach the team: with the own voice layer the call session ends as failed
without a hangup (`telephony/session.py`) and the dialplan rings the team
(docs/02 §5, docs/20 §5).

Live calls are held in this process. A turn for a call this process does not know
(after a restart) goes to the team instead of being answered without context.

With the own voice layer (docs/20 §4) the handler also says which sentence the
caller may talk over, answers a silent line with two questions from the code
before it ends the call, and sends a call whose ear or mouth failed to the team.
"""

import re
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from fractions import Fraction
from math import ceil

from sqlalchemy import text
from sqlalchemy.orm import Session

from api.agent.guards import names_dish, note_said
from api.agent.llm import LLMClient, Usage, cost_cents
from api.agent.loop import SAY_HUMAN_ANSWER, ConversationLoop
from api.agent.outcome import CLOSING_STAGES, call_outcome, call_usage, log_usage
from api.agent.prompt import build_system_prompt
from api.agent.state import ConversationState, initial_state
from api.config import settings
from api.core.logging import bind_call_id, get_logger
from api.core.time import utcnow
from api.domain.callbacks import team_missed
from api.domain.callbacks.create import SAY_NOTED
from api.domain.calls import (
    CallRouting,
    call_routing,
    end_call,
    start_call,
    takes_call,
)
from api.domain.menu.items import active_count
from api.schemas.calls import EndCallRequest, Outcome, StartCallRequest
from api.telephony.port import TelephonyPort
from api.telephony.speech import SpeechUsage
from api.telephony.speech_spike import spoken_name

# docs/05 §6. Drafts until the legal check in docs/09; the greeting carries no
# recording notice because nothing is recorded.
GREETING = "Guten Tag, hier ist der KI-Assistent von {name}.{privacy} Was kann ich für Sie tun?"
# Where the full privacy notice is (docs/09 L4), from service_config; left out
# while the restaurant has set none.
PRIVACY_POINTER = " Hinweise zum Datenschutz finden Sie unter {address}."
SAY_OUTAGE = (
    "Bei mir gibt es gerade eine technische Störung. "
    "Ich verbinde Sie direkt mit dem Restaurant."
)
# The outage sentence when it is the first of the call, before the greeting
# (the database down at the start, a call this process does not know): the AI
# says it is one first (docs/09 L1), without the restaurant's name, which may
# not be readable then.
SAY_OUTAGE_FIRST = f"Hier ist der KI-Assistent des Restaurants. {SAY_OUTAGE}"
# The caller hears a goodbye before the line goes dead. The code adds it unless
# the agent's last sentence already parts ("bis dann" after a reservation, "bis
# gleich" after a pickup), so it is never said twice.
FAREWELL = "Vielen Dank für Ihren Anruf. Auf Wiederhören."
# After a team leg nobody answered, played by the dialplan from files the
# process renders at its start (`app.announce`), so it is heard with `voice`
# down too; by their file names. "noted" only once the card is written
# (`on_team_missed`), the other on anything else (docs/20 §5). Drafts.
SAY_NOBODY_ANSWERED = "Im Restaurant hat gerade leider niemand abgenommen."
ANNOUNCEMENTS = {
    "missed_noted": f"{SAY_NOBODY_ANSWERED} {SAY_NOTED} {FAREWELL}",
    "missed_later": (
        f"{SAY_NOBODY_ANSWERED} Bitte rufen Sie später noch einmal an. {FAREWELL}"
    ),
}
# What the code asks into a silent line, in this order; after the last one the
# next silence ends the call with the farewell (docs/20 §4). Drafts.
STILL_THERE = (
    "Sind Sie noch da?",
    "Ich höre Sie leider nicht. Sind Sie noch dran?",
)
# In these stages nothing the agent says may be talked over: a read-back and
# its question are played to the end (rule 3), and so is what is said before a
# transfer.
_NOT_INTERRUPTIBLE = frozenset({"readback_pending", "transferred"})
_GOODBYE = re.compile(
    r"auf wiederh(ö|oe)ren|auf wiedersehen|tschüss|tschuess"
    r"|bis (dann|gleich|bald|später|spaeter|morgen)\b"
    r"|schönen (tag|abend)|schoenen (tag|abend)",
    re.IGNORECASE,
)

# Keys other than digits (`*`, `#`) only end an entry on the keypad.
_DIGIT = re.compile(r"[0-9]")
# Closed calls are remembered so a late event is ignored instead of being read
# as a call this process lost. Bounded: a restaurant line has a few hundred a day.
CLOSED_MEMORY = 10_000
# Numbers guests named in calls handed to the team, for the card of a team leg
# nobody answers; the call log keeps only the caller id. The report comes
# about a minute after the handover (the last sentence, two legs of 30 s), and
# an answered leg sends none: older entries are dropped, so a number neither
# outlives its call for long nor lands on another call with the same id
# (Codex on PR #345).
NAMED_PHONES_KEPT = 1000
NAMED_PHONE_SECONDS = 300.0
# How long the log of a call that ended as it should waits on a lock. Longer
# than the line's bound for a turn (`telephony/app.py`, `DB_TIMEOUT_MS`): after
# our hangup or transfer nobody waits on it, and a lock held for a moment must
# not cost the call its end (Codex on PR #298). Not for ever: the call's thread
# and its connection are given back when a lock is not coming free.
END_CALL_TIMEOUT_MS = 30_000

logger = get_logger(__name__)

SessionFactory = Callable[[], Session]
LLMFactory = Callable[[Session], LLMClient]


@dataclass
class _LiveCall:
    session_id: str
    session: Session
    call_id: uuid.UUID
    tenant_id: uuid.UUID
    team_phone: str
    state: ConversationState
    loop: ConversationLoop | None = None
    # The model client of this call, made by the factory and closed with the call.
    llm: LLMClient | None = None
    # Silences in a row that the code has answered with a question.
    silences: int = 0
    # Its log is written, or being written: taken under the lock by the one
    # caller of `_finish` (`_claim`).
    finished: bool = False


class CallHandler:
    """Implements `port.CallEvents` for one tenant's line."""

    def __init__(
        self,
        port: TelephonyPort,
        *,
        tenant_id: uuid.UUID,
        session_factory: SessionFactory,
        llm_factory: LLMFactory,
        now: datetime | None = None,
        fallback_team_phone: str | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._port = port
        self._tenant_id = tenant_id
        self._session_factory = session_factory
        self._llm_factory = llm_factory
        # A fixed instant keeps a played call reproducible; without it every event
        # runs on the real clock.
        self._now = now
        # What a call's length is measured with (`max_call_seconds`); a test
        # sets it instead of waiting.
        self._clock = clock
        # The only number left when the database cannot be read.
        self._fallback_team_phone = fallback_team_phone or settings.team_phone
        self._lock = threading.Lock()
        self._live: dict[str, _LiveCall] = {}
        # Calls the core has sent to the team or ended, until the adapter reports
        # the end of the leg; with the outcome to log, None to derive it.
        self._ending: dict[str, tuple[_LiveCall, Outcome | None]] = {}
        self._starting: set[str] = set()
        # Calls whose leg ended while they were still being opened; they are
        # closed as soon as the opening is done.
        self._ended_while_starting: set[str] = set()
        # The calls the agent took and whose leg has not ended: the count for
        # the limit of concurrent AI calls (D16, T-10.9). In memory: a call row
        # a crash left open in the database must not keep the agent off.
        self._ai_calls: set[str] = set()
        self._closed: OrderedDict[str, None] = OrderedDict()
        # What a card for a team leg nobody answers needs and only this
        # process knows: a number the guest named, the card the core wrote.
        self._named_phones: OrderedDict[str, tuple[str | None, str | None, float]] = (
            OrderedDict()
        )

    # -- events from the line ----------------------------------------------------

    def on_call_started(self, session_id: str) -> None:
        with self._lock:
            if self._known(session_id):
                return  # repeated start event: one call row, one greeting
            self._starting.add(session_id)

        session: Session | None = None
        try:
            session = self._session_factory()
            routing = call_routing(session, self._tenant_id, self._now)
            caller_id = self._port.caller_id(session_id)
            call_id = start_call(
                session,
                StartCallRequest(
                    tenant_id=self._tenant_id,
                    external_session_id=session_id,
                    caller_id=caller_id,
                ),
                now=self._now,
            ).call_id
        except Exception:
            _close_quietly(session)
            # Out of the start and the decision in one step: an end of the
            # leg that comes after it finds the call closed, never a start
            # that is still running (Codex on PR #298).
            with self._lock:
                hung_up = session_id in self._ended_while_starting
                self._ended_while_starting.discard(session_id)
                self._starting.discard(session_id)
                self._remember_closed(session_id)
            if hung_up:
                # Nobody is on the line to hand to the team.
                logger.exception(
                    "call could not be opened, the caller had hung up already",
                    extra={"extra": {"session_id": session_id}},
                )
                return
            logger.exception(
                "call could not be opened, handing it to the team",
                extra={"extra": {"session_id": session_id}},
            )
            self._fail(session_id, None)
            return

        bind_call_id(str(call_id))
        live = _LiveCall(
            session_id=session_id,
            session=session,
            call_id=call_id,
            tenant_id=self._tenant_id,
            team_phone=routing.team_phone,
            state=initial_state(call_id, self._tenant_id, caller_id=caller_id),
        )
        with self._lock:
            self._starting.discard(session_id)
            hung_up = session_id in self._ended_while_starting
            self._ended_while_starting.discard(session_id)
            takes = False
            if hung_up:
                self._remember_closed(session_id)
                self._claim(live)
            else:
                self._live[session_id] = live
                # The decision and the place in one step: two calls that start
                # at once cannot both take the last one (T-10.9).
                takes = takes_call(routing, len(self._ai_calls))
                if takes:
                    self._ai_calls.add(session_id)
        if routing.ai_answers and not takes and not hung_up:
            logger.info(
                "call limit reached, the team takes the call",
                extra={
                    "extra": {"session_id": session_id, "limit": routing.call_limit}
                },
            )
        routing = replace(routing, ai_answers=takes)
        if hung_up:
            # The end of the leg came while the call row was written (#207):
            # no greeting into a dead line, and the log is closed now.
            self._finish(live)
            return
        self._guarded(live, lambda: self._open(live, routing))

    def on_user_turn(self, session_id: str, text: str) -> None:
        live = self._lookup(session_id)
        if live is not None:
            self._guarded(live, lambda: self._turn(live, text))

    def on_dtmf(self, session_id: str, digits: str) -> None:
        """Digits from the keypad go to the agent as the caller's answer, the way a
        spoken number would (docs/05 §2, step 4)."""
        keyed = "".join(_DIGIT.findall(digits))
        if keyed:
            self.on_user_turn(session_id, keyed)

    def on_silence(self, session_id: str) -> None:
        live = self._lookup(session_id)
        if live is not None:
            self._guarded(live, lambda: self._silent(live))

    def on_voice_failure(self, session_id: str) -> None:
        """Ear or mouth failed: the outage sentence, as far as it can still be
        spoken, and the team (rule 5). Also on a call the core was ending: a
        goodbye nobody may have heard is no reason to hang up."""
        with self._lock:
            ending = self._ending.get(session_id)
        live = ending[0] if ending is not None else self._lookup(session_id)
        if live is not None:
            logger.error(
                "the voice layer failed, handing the call to the team",
                extra={"extra": {"session_id": session_id}},
            )
            self._fail(session_id, live)

    def on_call_ended(self, session_id: str, speech: SpeechUsage | None = None) -> None:
        with self._lock:
            self._ai_calls.discard(session_id)  # the leg is gone, its place is free
            if session_id in self._starting:
                self._ended_while_starting.add(session_id)
                return  # `on_call_started` closes it once the call is open
            live, outcome = self._live.pop(session_id, None), None
            if live is None:
                live, outcome = self._ending.pop(session_id, (None, None))
            if live is None or not self._claim(live):
                return  # closed already, or never known
            self._remember_closed(session_id)
        self._finish(live, outcome, speech=speech)

    def on_team_missed(self, session_id: str) -> bool:
        with self._lock:
            self._forget_old_named_phones()
            phone, summary, _ = self._named_phones.pop(session_id, (None, None, 0.0))
        session: Session | None = None
        try:
            session = self._session_factory()
            card = team_missed(
                session,
                self._tenant_id,
                session_id,
                phone=phone,
                summary=summary,
                now=self._now or utcnow(),
            )
        except Exception as exc:  # noqa: BLE001 - the caller hears "call again"
            # The type only: a message can carry the guest's number.
            logger.error(
                "no callback card for a team leg nobody answered",
                extra={
                    "extra": {"session_id": session_id, "error": type(exc).__name__}
                },
            )
            return False
        finally:
            _close_quietly(session)
        return card is not None

    # -- the call ----------------------------------------------------------------

    def _open(self, live: _LiveCall, routing: CallRouting) -> None:
        if not routing.ai_answers:
            # Agent switched off or in shadow mode: the team takes the call, and
            # the agent says nothing at all.
            self._transfer(live, routing.team_phone)
            return
        live.llm = self._llm_factory(live.session)
        live.loop = ConversationLoop(
            live.session,
            live.llm,
            # The same prompt as the text phone and the evals (sim/session.py):
            # a real model learns the tools from it.
            build_system_prompt(tools=True),
            # The tenant's limit, not the setting of the same name: that one
            # is only what the seed writes into a new tenant.
            max_call_seconds=routing.max_call_seconds,
            # Only here a guest holds a receiver and waits (#252).
            turn_deadline_seconds=routing.turn_deadline_seconds,
            now=self._now,
            clock=self._clock,
        )
        live.state.greeted = True
        self._port.say(live.session_id, greeting(routing))

    def _turn(self, live: _LiveCall, text: str) -> None:
        if live.loop is None:
            raise RuntimeError("turn on a call the agent does not answer")
        bind_call_id(str(live.call_id))
        live.silences = 0
        known = set(live.state.known_item_ids)
        result = live.loop.run_turn(live.state, text)
        ending = result.ended or live.state.stage in CLOSING_STAGES
        # The core takes the sentence that ends a turn as heard
        # (`guards.note_said`): digits behind a question for the guest's number
        # are that number, and a dish that was named may enter the order. Such
        # a sentence is played to the end, or the guest would be measured
        # against words they talked over (Codex PR #286).
        # So is the answer to "Sind Sie ein Mensch?": a yes to the offer in it
        # hands the call over (agent/loop.py, ultrareview of PR #372).
        # The same for the question for the allergy as a kitchen note: its
        # answer is consent only to a question heard to the end (L13; Codex
        # PR #368).
        counted_as_heard = (
            live.state.asked_for_phone
            or live.state.known_item_ids != known
            or live.state.last_say == SAY_HUMAN_ANSWER
            or live.state.held_call is not None
        )
        interruptible = (
            not ending
            and not counted_as_heard
            and live.state.stage not in _NOT_INTERRUPTIBLE
        )
        menu_size = active_count(live.session, live.tenant_id)
        said = " ".join(result.say)
        self._port.expect(live.session_id, key_terms(live.state, said, menu_size))
        for line in result.say:
            self._port.say(live.session_id, line, interruptible=interruptible)
        if live.state.stage == "transferred":
            self._transfer(live, live.state.transfer_to or live.team_phone)
        elif ending:
            self._hangup(live, last_said=result.say[-1] if result.say else "")

    def _silent(self, live: _LiveCall) -> None:
        """The caller says nothing: ask twice, then say goodbye and end the
        call. A draft that waits for its yes stays a draft."""
        if live.silences >= len(STILL_THERE):
            self._hangup(live, last_said="")
            return
        question = STILL_THERE[live.silences]
        live.silences += 1
        # The last thing the guest heard is this question now, not the
        # read-back: a "Ja" to it confirms nothing (rule 3), the core reads the
        # draft out again first.
        note_said(live.state, question)
        # And the key terms follow it: the names asked about before would bend
        # a "Ja" into a dish (Codex PR #292).
        menu_size = active_count(live.session, live.tenant_id)
        self._port.expect(live.session_id, key_terms(live.state, question, menu_size))
        self._port.say(live.session_id, question, interruptible=True)

    def _transfer(self, live: _LiveCall, target: str) -> None:
        live.state.stage = "transferred"
        self._keep_named_phone(live)
        self._await_end(live)
        self._port.transfer(live.session_id, target)

    def _hangup(self, live: _LiveCall, *, last_said: str) -> None:
        if not _GOODBYE.search(last_said):
            self._port.say(live.session_id, FAREWELL)
        self._await_end(live)
        self._port.hangup(live.session_id)

    def _guarded(self, live: _LiveCall, step: Callable[[], None]) -> None:
        try:
            step()
        except Exception:
            logger.exception(
                "call failed, handing it to the team",
                extra={"extra": {"session_id": live.session_id}},
            )
            self._fail(live.session_id, live)

    def _fail(self, session_id: str, live: _LiveCall | None) -> None:
        """Outage sentence, then the team. The sentence is best effort, the
        transfer is not: if the line refuses it, the call is logged as an
        error at once and the error is raised to the call session, which ends as
        failed so the dialplan rings the team (docs/20 §5)."""
        with self._lock:
            self._live.pop(session_id, None)
            self._ending.pop(session_id, None)
            self._starting.discard(session_id)
            self._ended_while_starting.discard(session_id)
            self._remember_closed(session_id)
            # Not for a call whose end was handled already: an adapter on
            # more than one thread can report it in the meantime, and its
            # session is the other thread's then.
            still_open = live is not None and not live.finished
            if live is not None and still_open:
                self._ending[session_id] = (live, "error")
        if live is not None and still_open:
            try:
                live.session.rollback()
            except Exception:
                logger.exception("rollback after the failure failed")
        target = live.team_phone if live is not None else self._fallback_team_phone
        if live is not None:
            self._keep_named_phone(live)
        try:
            try:
                greeted = live is not None and live.state.greeted
                self._port.say(session_id, SAY_OUTAGE if greeted else SAY_OUTAGE_FIRST)
            except Exception:
                logger.exception("outage sentence could not be spoken")
            self._port.transfer(session_id, target)
        except Exception:
            logger.exception(
                "transfer to the team failed, the dialplan has to ring the team",
                extra={"extra": {"session_id": session_id}},
            )
            # No end of the leg will be reported for a transfer that never began.
            # The agent's place is free: the AI leg is unusable, and the
            # dialplan rings the team. Otherwise the place stays taken until
            # the end of the leg, after the outage sentence (T-10.9).
            with self._lock:
                self._ending.pop(session_id, None)
                self._ai_calls.discard(session_id)
                claimed = live is not None and self._claim(live)
            if live is not None and claimed:
                self._finish(live, outcome="error", caller_waits=True)
            raise

    def _finish(
        self,
        live: _LiveCall,
        outcome: Outcome | None = None,
        *,
        caller_waits: bool = False,
        speech: SpeechUsage | None = None,
    ) -> None:
        """Write the call log and release the session. Never raises: the call has
        already gone to the team or ended, and a failure here must not send it
        through the outage path a second time. Called once per call, by
        whoever claimed it (`_claim`)."""
        derived, intent = call_outcome(live.state)
        try:
            usage = call_usage(live.llm)
            log_usage(logger, live.call_id, usage)
            # A statement that failed and was caught leaves the transaction
            # aborted, and nothing more would run in it.
            live.session.rollback()
            # The line bounds its statements (`db.bounded_sessions`); this
            # write waits longer, in this transaction only, which the commit
            # of `end_call` ends. Not when the transfer to the team failed:
            # no hangup went out then, and the dialplan rings the team only
            # once the session has ended and its connection is closed, after
            # this write (ultra review on PR #310). A transfer that went out
            # was a hangup to the line: nobody waits on its log, an error or
            # not (own review on PR #310).
            if not caller_waits:
                live.session.execute(
                    text(f"set local statement_timeout = {END_CALL_TIMEOUT_MS}")
                )
            end_call(
                live.session,
                EndCallRequest(
                    call_id=live.call_id,
                    tenant_id=live.tenant_id,
                    outcome=outcome or derived,
                    intent=intent,
                    cost_cents=_cost(live, usage, speech, outcome),
                    model=usage.model,
                ),
                now=self._now or utcnow(),
            )
        except Exception:
            logger.exception("call log could not be closed")
        finally:
            _close_quietly(live.session)
            _close_model(live.llm)

    # -- bookkeeping ---------------------------------------------------------------

    def _lookup(self, session_id: str) -> _LiveCall | None:
        """The live call, or None when the event is to be ignored. A call this
        process never saw goes to the team first."""
        with self._lock:
            live = self._live.get(session_id)
            if live is not None or self._known(session_id):
                return live
            self._remember_closed(session_id)
        logger.warning(
            "event for an unknown call, handing it to the team",
            extra={"extra": {"session_id": session_id}},
        )
        self._fail(session_id, None)
        return None

    def _known(self, session_id: str) -> bool:
        return (
            session_id in self._live
            or session_id in self._ending
            or session_id in self._starting
            or session_id in self._closed
        )

    def _await_end(self, live: _LiveCall) -> None:
        """The core is done with the call: nothing the caller says reaches it
        any more, and the log waits for the end of the leg."""
        with self._lock:
            self._live.pop(live.session_id, None)
            self._ending[live.session_id] = (live, None)
            self._remember_closed(live.session_id)

    def _claim(self, live: _LiveCall) -> bool:
        """Under `_lock`: True for the one caller that writes the call's log.
        The end of the leg and a failure may both come to it, on two threads,
        and a claim taken later than the event would leave a gap between."""
        if live.finished:
            return False
        live.finished = True
        return True

    def _remember_closed(self, session_id: str) -> None:
        self._closed[session_id] = None
        self._closed.move_to_end(session_id)
        while len(self._closed) > CLOSED_MEMORY:
            self._closed.popitem(last=False)

    def _keep_named_phone(self, live: _LiveCall) -> None:
        """Before the handover, so a report that comes at once finds it. Only a
        number other than the caller id, which the call log has, and the card
        the core wrote for the handover (`state.handoff_summary`)."""
        phone = live.state.slots.get("phone")
        if phone == live.state.caller_phone:
            phone = None
        summary = live.state.handoff_summary
        if not phone and not summary:
            return
        with self._lock:
            self._forget_old_named_phones()
            self._named_phones[live.session_id] = (phone, summary, self._clock())
            self._named_phones.move_to_end(live.session_id)
            while len(self._named_phones) > NAMED_PHONES_KEPT:
                self._named_phones.popitem(last=False)

    def _forget_old_named_phones(self) -> None:
        """Under `_lock`. Oldest first, in the order they were kept."""
        while self._named_phones:
            *_, kept_at = next(iter(self._named_phones.values()))
            if self._clock() - kept_at <= NAMED_PHONE_SECONDS:
                return
            self._named_phones.popitem(last=False)


def greeting(routing: CallRouting) -> str:
    """The first sentence of a call the AI answers: the AI notice, and where the
    privacy notice is once the restaurant has set it (docs/09 L1, L4)."""
    address = (routing.privacy_notice_address or "").strip()
    privacy = PRIVACY_POINTER.format(address=address) if address else ""
    return GREETING.format(name=routing.tenant_name, privacy=privacy)


def _cost(
    live: _LiveCall, usage: Usage, speech: SpeechUsage | None, outcome: str | None
) -> int | None:
    """The call's cost for its log; never stops the log from being written.
    A team leg is any handover: the tool's, or the outage path's (`_fail`,
    outcome "error"), which the dialplan also takes for a failed session."""
    leg = live.state.transferred or live.state.stage == "transferred"
    try:
        return call_cost_cents(usage, speech, transferred=leg or outcome == "error")
    except Exception:
        logger.exception("the cost of the call could not be computed")
        return None


def call_cost_cents(
    usage: Usage, speech: SpeechUsage | None, *, transferred: bool
) -> int | None:
    """The cost of a phone call in whole cents (T-10.6): the model's
    (`cost_cents`) and the voice layer's, ear and mouth by their prices, each
    rounded up. None when any part is unknown: a model cost unknown, no
    measurement, a price missing, or a transfer leg with a price above 0,
    whose length the line does not report yet. Unknown is never 0.
    shortcut: the transfer leg is unpriced until the trunk (T-10.12) reports
    its length; the router of the test phase costs nothing."""
    model = cost_cents(usage)
    ear = settings.voice_ear_cents_per_hour
    mouth = settings.voice_mouth_cents_per_mchar
    outbound = settings.voice_outbound_cents_per_hour
    if model is None or speech is None or ear is None or mouth is None:
        return None
    if transferred and outbound != 0:
        return None
    # The seconds come from whole frames: a float like 0.1 must not round a
    # whole cent up (`limit_denominator`).
    voice = Fraction(speech.heard_seconds).limit_denominator(1_000_000) * ear / 3600
    voice += Fraction(speech.spoken_characters * mouth, 1_000_000)
    return model + ceil(voice)


def _close_quietly(session: Session | None) -> None:
    if session is None:
        return
    try:
        session.close()
    except Exception:
        logger.exception("database session could not be closed")


def _close_model(llm: LLMClient | None) -> None:
    """A client that talks to a model server holds connections (`ChatCompletionsLLM`);
    one per call and never closed, they pile up over a day of calls (#252). A
    client with nothing to close has no `close`."""
    close = getattr(llm, "close", None)
    if close is None:
        return
    try:
        close()
    except Exception:
        logger.exception("model client could not be closed")


def key_terms(state: ConversationState, said: str, menu_size: int) -> list[str]:
    """The dish names in question after a turn, for the speech engine (docs/20
    §6): first those the agent named in what it said (the guest answers "die
    26 oder die 26d?" next), then those of the order, which a correction
    names. They come from the searches of this call, so from the active menu in
    the database, and are never the menu (CLAUDE.md §2 rule 6): terms that
    would hold every dish of it, `menu_size`, are not handed over at all."""
    heard = said.lower()
    ids = [
        *(
            item_id
            for item_id, seen in state.seen_items.items()
            # As a guest says the name: the agent does too, without "(4 Stück)".
            if names_dish(
                heard, seen["number"].lower(), spoken_name(seen["name"]).lower()
            )
        ),
        *(line["menu_item_id"] for line in state.cart),
    ]
    known = list(dict.fromkeys(i for i in ids if i in state.seen_items))
    if len(known) >= menu_size:
        return []
    names = (spoken_name(state.seen_items[item_id]["name"]) for item_id in known)
    return list(dict.fromkeys(name for name in names if name))
