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
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

from api.agent.guards import names_dish, note_said
from api.agent.llm import LLMClient, cost_cents
from api.agent.loop import ConversationLoop
from api.agent.outcome import CLOSING_STAGES, call_outcome, call_usage, log_usage
from api.agent.prompt import build_system_prompt
from api.agent.state import ConversationState, initial_state
from api.config import settings
from api.core.logging import bind_call_id, get_logger
from api.core.time import utcnow
from api.domain.calls import CallRouting, call_routing, end_call, start_call
from api.domain.menu.items import active_count
from api.schemas.calls import EndCallRequest, Outcome, StartCallRequest
from api.telephony.port import TelephonyPort
from api.telephony.speech_spike import spoken_name

# docs/05 §6. Drafts until the legal check in docs/09; the greeting carries no
# recording notice because nothing is recorded.
GREETING = "Guten Tag, hier ist der KI-Assistent von {name}. Was kann ich für Sie tun?"
SAY_OUTAGE = (
    "Bei mir gibt es gerade eine technische Störung. "
    "Ich verbinde Sie direkt mit dem Restaurant."
)
# The caller hears a goodbye before the line goes dead. The code adds it unless
# the agent's last sentence already parts ("bis dann" after a reservation, "bis
# gleich" after a pickup), so it is never said twice.
FAREWELL = "Vielen Dank für Ihren Anruf. Auf Wiederhören."
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
        self._closed: OrderedDict[str, None] = OrderedDict()

    # -- events from the line ----------------------------------------------------

    def on_call_started(self, session_id: str) -> None:
        with self._lock:
            if self._known(session_id):
                return  # repeated start event: one call row, one greeting
            self._starting.add(session_id)

        session: Session | None = None
        try:
            session = self._session_factory()
            routing = call_routing(session, self._tenant_id)
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
            if hung_up:
                self._remember_closed(session_id)
            else:
                self._live[session_id] = live
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

    def on_call_ended(self, session_id: str) -> None:
        with self._lock:
            if session_id in self._starting:
                self._ended_while_starting.add(session_id)
                return  # `on_call_started` closes it once the call is open
            live, outcome = self._live.pop(session_id, None), None
            if live is None:
                live, outcome = self._ending.pop(session_id, (None, None))
            if live is None:
                return  # closed already, or never known
            self._remember_closed(session_id)
        self._finish(live, outcome)

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
        self._port.say(live.session_id, GREETING.format(name=routing.tenant_name))

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
        counted_as_heard = (
            live.state.asked_for_phone or live.state.known_item_ids != known
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
            if live is not None:
                self._ending[session_id] = (live, "error")
        if live is not None:
            try:
                live.session.rollback()
            except Exception:
                logger.exception("rollback after the failure failed")
        target = live.team_phone if live is not None else self._fallback_team_phone
        try:
            try:
                self._port.say(session_id, SAY_OUTAGE)
            except Exception:
                logger.exception("outage sentence could not be spoken")
            self._port.transfer(session_id, target)
        except Exception:
            logger.exception(
                "transfer to the team failed, the dialplan has to ring the team",
                extra={"extra": {"session_id": session_id}},
            )
            # No end of the leg will be reported for a transfer that never began.
            with self._lock:
                self._ending.pop(session_id, None)
            if live is not None:
                self._finish(live, outcome="error")
            raise

    def _finish(self, live: _LiveCall, outcome: Outcome | None = None) -> None:
        """Write the call log and release the session. Never raises: the call has
        already gone to the team or ended, and a failure here must not send it
        through the outage path a second time."""
        derived, intent = call_outcome(live.state)
        try:
            usage = call_usage(live.llm)
            log_usage(logger, live.call_id, usage)
            # The line may bound its statements (`db.bounded_sessions`). A
            # guest waits on a turn, nobody waits on the log of a call that is
            # over: a lock held for a moment must not cost the call its end
            # and outcome (Codex on PR #298). Unbounded for this transaction
            # only; the commit of `end_call` ends it.
            live.session.execute(text("set local statement_timeout = 0"))
            end_call(
                live.session,
                EndCallRequest(
                    call_id=live.call_id,
                    tenant_id=live.tenant_id,
                    outcome=outcome or derived,
                    intent=intent,
                    cost_cents=cost_cents(usage),
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

    def _remember_closed(self, session_id: str) -> None:
        self._closed[session_id] = None
        self._closed.move_to_end(session_id)
        while len(self._closed) > CLOSED_MEMORY:
            self._closed.popitem(last=False)


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
