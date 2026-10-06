"""Provider-neutral call handler: platform events in, agent core and call log behind.

Every adapter feeds this one handler, so what happens on a call does not depend on
the provider (docs/11 §telephony). The code, not the model, says the AI disclosure
before anything else (docs/05 §6). The mode decides whether the agent answers at
all (docs/02 §4). Each turn runs through the same `agent/loop.py` as `sim/` and the
evals; when the conversation says transferred or over, the handler dials the team
or says goodbye and hangs up, and writes the call log.

Every failure ends with the team (CLAUDE.md §2 rule 5): an exception anywhere in a
call, from the database, the model or the adapter, is answered with the outage
sentence and a transfer to the team extension. If even that transfer fails, the
error is raised to the adapter, so its webhook fails and the platform's own
fallback takes the call (docs/02 §5).

Live calls are held in this process. A turn for a call this process does not know
(after a restart) goes to the team instead of being answered without context.
"""

import re
import threading
import uuid
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from api.agent.llm import LLMClient
from api.agent.loop import ConversationLoop
from api.agent.outcome import CLOSING_STAGES, call_outcome
from api.agent.prompt import build_system_prompt
from api.agent.state import ConversationState, initial_state
from api.config import settings
from api.core.logging import bind_call_id, get_logger
from api.core.time import utcnow
from api.domain.calls import CallRouting, call_routing, end_call, start_call
from api.schemas.calls import EndCallRequest, Outcome, StartCallRequest
from api.telephony.port import TelephonyPort

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
_GOODBYE = re.compile(
    r"auf wiederh(ö|oe)ren|auf wiedersehen|tschüss|tschuess"
    r"|bis (dann|gleich|bald|später|spaeter|morgen)\b"
    r"|schönen (tag|abend)|schoenen (tag|abend)",
    re.IGNORECASE,
)

# Keys other than digits (`*`, `#`) only end an entry on the keypad.
_DIGIT = re.compile(r"[0-9]")
# Closed calls are remembered so a late webhook is ignored instead of being read
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
    ):
        self._port = port
        self._tenant_id = tenant_id
        self._session_factory = session_factory
        self._llm_factory = llm_factory
        # A fixed instant keeps a played call reproducible; without it every event
        # runs on the real clock.
        self._now = now
        # The only number left when the database cannot be read.
        self._fallback_team_phone = fallback_team_phone or settings.team_phone
        self._lock = threading.Lock()
        self._live: dict[str, _LiveCall] = {}
        self._starting: set[str] = set()
        self._closed: OrderedDict[str, None] = OrderedDict()

    # -- events from the platform ------------------------------------------------

    def on_call_started(self, session_id: str) -> None:
        with self._lock:
            if self._known(session_id):
                return  # platform retry: one call row, one greeting
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
            logger.exception(
                "call could not be opened, handing it to the team",
                extra={"extra": {"session_id": session_id}},
            )
            _close_quietly(session)
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
            self._live[session_id] = live
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

    def on_call_ended(self, session_id: str) -> None:
        with self._lock:
            live = self._live.pop(session_id, None)
            if live is None:
                return  # closed by us already, or never known
            self._remember_closed(session_id)
        self._finish(live)

    # -- the call ----------------------------------------------------------------

    def _open(self, live: _LiveCall, routing: CallRouting) -> None:
        if not routing.ai_answers:
            # Agent switched off or in shadow mode: the team takes the call, and
            # the agent says nothing at all.
            self._transfer(live, routing.team_phone)
            return
        live.loop = ConversationLoop(
            live.session,
            self._llm_factory(live.session),
            # The same prompt as the text phone and the evals (sim/session.py):
            # a real model learns the tools from it.
            build_system_prompt(tools=True),
            now=self._now,
        )
        live.state.greeted = True
        self._port.say(live.session_id, GREETING.format(name=routing.tenant_name))

    def _turn(self, live: _LiveCall, text: str) -> None:
        if live.loop is None:
            raise RuntimeError("turn on a call the agent does not answer")
        bind_call_id(str(live.call_id))
        result = live.loop.run_turn(live.state, text)
        for line in result.say:
            self._port.say(live.session_id, line)
        if live.state.stage == "transferred":
            self._transfer(live, live.state.transfer_to or live.team_phone)
        elif result.ended or live.state.stage in CLOSING_STAGES:
            self._hangup(live, last_said=result.say[-1] if result.say else "")

    def _transfer(self, live: _LiveCall, target: str) -> None:
        self._close(live.session_id)
        live.state.stage = "transferred"
        self._port.transfer(live.session_id, target)
        self._finish(live)

    def _hangup(self, live: _LiveCall, *, last_said: str) -> None:
        if not _GOODBYE.search(last_said):
            self._port.say(live.session_id, FAREWELL)
        self._close(live.session_id)
        self._port.hangup(live.session_id)
        self._finish(live)

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
        transfer is not: if the platform refuses it, the error goes to the adapter."""
        with self._lock:
            self._live.pop(session_id, None)
            self._starting.discard(session_id)
            self._remember_closed(session_id)
        target = live.team_phone if live is not None else self._fallback_team_phone
        try:
            try:
                self._port.say(session_id, SAY_OUTAGE)
            except Exception:
                logger.exception("outage sentence could not be spoken")
            self._port.transfer(session_id, target)
        except Exception:
            logger.exception(
                "transfer to the team failed, the platform fallback has to take the call",
                extra={"extra": {"session_id": session_id}},
            )
            raise
        finally:
            if live is not None:
                try:
                    live.session.rollback()
                except Exception:
                    logger.exception("rollback after the failure failed")
                self._finish(live, outcome="error")

    def _finish(self, live: _LiveCall, outcome: Outcome | None = None) -> None:
        """Write the call log and release the session. Never raises: the call has
        already gone to the team or ended, and a failure here must not send it
        through the outage path a second time."""
        derived, intent = call_outcome(live.state)
        try:
            end_call(
                live.session,
                EndCallRequest(
                    call_id=live.call_id,
                    tenant_id=live.tenant_id,
                    outcome=outcome or derived,
                    intent=intent,
                ),
                now=self._now or utcnow(),
            )
        except Exception:
            logger.exception("call log could not be closed")
        finally:
            _close_quietly(live.session)

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
            or session_id in self._starting
            or session_id in self._closed
        )

    def _close(self, session_id: str) -> None:
        with self._lock:
            self._live.pop(session_id, None)
            self._remember_closed(session_id)

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
