"""Kundenzug → Modell → Tool-Aufrufe → Antwort; Abbruch bei `max_call_seconds` (docs/11 §agent).

Die eigentliche Verständnis-Leiter (Nachfragen, Buchstabieren, Eskalation) kommt
erst mit T-2.2 (`ladder.py`, `escalation.py`). Hier steht nur der Antrieb: das
Modell bekommt den Kundenzug und den kompakten Zustand, ruft null oder mehrere
Tools auf, und irgendwann einen Satz für den Kunden.
"""

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from api.agent.dispatch import ToolResult, dispatch
from api.agent.llm import LLMClient
from api.agent.state import ConversationState, apply_tool_result
from api.config import settings

# Schutz gegen ein Modell, das sich zwischen Tool-Aufrufen verheddert und nie zu
# einem Satz für den Kunden kommt: lieber sauber abbrechen als den Anruf endlos
# im Loop stehen zu lassen.
MAX_TOOL_HOPS = 6
SAY_TIMEOUT = "Wir sind jetzt schon eine Weile dran. Ich gebe an das Team weiter."
SAY_STUCK = "Da komme ich gerade nicht weiter. Ich gebe an das Team weiter."

ENDED_STAGES = frozenset({"transferred", "ended"})


@dataclass
class TurnResult:
    state: ConversationState
    say: list[str] = field(default_factory=list)
    ended: bool = False


class ConversationLoop:
    def __init__(
        self,
        session: Session,
        llm: LLMClient,
        system_prompt: str,
        *,
        max_call_seconds: int | None = None,
        clock: Callable[[], float] = time.monotonic,  # austauschbar für Tests
    ):
        self._session = session
        self._llm = llm
        self._system_prompt = system_prompt
        self._max_call_seconds = (
            settings.max_call_seconds if max_call_seconds is None else max_call_seconds
        )
        self._clock = clock
        self._started = clock()

    def run_turn(self, state: ConversationState, user_text: str) -> TurnResult:
        if self._clock() - self._started > self._max_call_seconds:
            state.stage = "ended"
            return TurnResult(state=state, say=[SAY_TIMEOUT], ended=True)

        pending_input = user_text
        for _ in range(MAX_TOOL_HOPS):
            turn = self._llm.next_turn(
                self._system_prompt, state.to_prompt_json(), pending_input
            )
            if turn.tool_call is None:
                say = turn.say
                assert say is not None  # LLMTurn garantiert genau eins von beidem
                return TurnResult(
                    state=state, say=[say], ended=state.stage in ENDED_STAGES
                )

            result = dispatch(
                self._session,
                state.call_id,
                state.tenant_id,
                turn.tool_call.name,
                turn.tool_call.args,
            )
            apply_tool_result(state, turn.tool_call.name, result)
            pending_input = _tool_result_as_input(turn.tool_call.name, result)

        state.stage = "ended"
        return TurnResult(state=state, say=[SAY_STUCK], ended=True)


def _tool_result_as_input(name: str, result: ToolResult) -> str:
    """Ergebnis dem Modell als nächster Zug zurückgeben, ohne echten Kundeninput."""
    if result.ok:
        payload = {"tool": name, "ok": True, "data": result.data}
    else:
        payload = {"tool": name, "ok": False, "error_code": result.error_code}
    return json.dumps(payload, default=str, ensure_ascii=False)
