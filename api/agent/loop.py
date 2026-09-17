"""Kundenzug → Modell → Tool-Aufrufe → Antwort; Abbruch bei `max_call_seconds` (docs/11 §agent).

Die eigentliche Verständnis-Leiter (Nachfragen, Buchstabieren, Eskalation vor dem
Modell) kommt erst mit T-2.2 (`ladder.py`, `escalation.py`). Hier steht nur der
Antrieb: das Modell bekommt den Kundenzug und den kompakten Zustand, ruft null
oder mehrere Tools auf, und irgendwann einen Satz für den Kunden — und wenn der
Loop selbst nicht mehr weiterkommt (Zeitlimit, zu viele Tool-Hops), übernimmt er
die Übergabe an das Team wirklich, statt sie dem Kunden nur anzukündigen
(CLAUDE.md §2 Regel 5: kein Anruf geht verloren).
"""

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.orm import Session

from api.agent.dispatch import ToolResult, dispatch
from api.agent.llm import LLMClient
from api.agent.state import ConversationState, apply_state_patch, apply_tool_result
from api.config import settings

# Schutz gegen ein Modell, das sich zwischen Tool-Aufrufen verheddert und nie zu
# einem Satz für den Kunden kommt: lieber sauber abbrechen als den Anruf endlos
# im Loop stehen zu lassen.
MAX_TOOL_HOPS = 6
SAY_TIMEOUT = "Wir sind jetzt schon eine Weile dran. Ich gebe an das Team weiter."
SAY_STUCK = "Da komme ich gerade nicht weiter. Ich gebe an das Team weiter."

ENDED_STAGES = frozenset({"transferred", "ended"})

# Weder TransferReason noch CallbackReason kennen einen eigenen Grund für "der
# Loop selbst ist abgebrochen" (docs/03 §Enums) - das eigene Erfinden eines neuen
# Werts wäre eine Schema-Änderung außerhalb von T-2.1. "human_requested" trifft
# die Absicht am ehesten: es braucht jetzt einen Menschen. Assumption, docs/01.
HANDOFF_REASON = "human_requested"


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
        now: datetime | None = None,
        clock: Callable[[], float] = time.monotonic,  # austauschbar für Tests
    ):
        self._session = session
        self._llm = llm
        self._system_prompt = system_prompt
        self._max_call_seconds = (
            settings.max_call_seconds if max_call_seconds is None else max_call_seconds
        )
        self._now = now
        self._clock = clock
        self._started = clock()

    def run_turn(self, state: ConversationState, user_text: str) -> TurnResult:
        pending_input = user_text
        for _ in range(MAX_TOOL_HOPS):
            if self._clock() - self._started > self._max_call_seconds:
                return self._handoff(state, SAY_TIMEOUT)

            turn = self._llm.next_turn(
                self._system_prompt, state.to_prompt_json(), pending_input
            )
            if turn.state_patch:
                apply_state_patch(state, turn.state_patch)

            if turn.tool_call is None:
                say = turn.say
                assert say is not None  # LLMTurn garantiert genau eins von beidem
                return TurnResult(
                    state=state, say=[say], ended=state.stage in ENDED_STAGES
                )

            result = self._dispatch(state, turn.tool_call.name, turn.tool_call.args)
            apply_tool_result(state, turn.tool_call.name, result)
            pending_input = _tool_result_as_input(turn.tool_call.name, result)

        return self._handoff(state, SAY_STUCK)

    def _dispatch(self, state: ConversationState, name: str, args: dict) -> ToolResult:
        return dispatch(
            self._session, state.call_id, state.tenant_id, name, args, now=self._now
        )

    def _handoff(self, state: ConversationState, fallback_say: str) -> TurnResult:
        """Übergabe wirklich ausführen statt nur anzukündigen: erst versuchen, live zu
        verbinden; ist niemand erreichbar und kennen wir eine Rufnummer, stattdessen
        einen Rückruf anlegen. Ohne bekannte Rufnummer bleibt nur der ehrliche
        Fallback-Satz — raten (CLAUDE.md §2 Regel 2) ist keine Option."""
        transfer = self._dispatch(state, "transfer_to_team", {"reason": HANDOFF_REASON})
        apply_tool_result(state, "transfer_to_team", transfer)
        if transfer.ok and transfer.data.get("available"):
            return TurnResult(
                state=state, say=[transfer.say or fallback_say], ended=True
            )

        phone = state.slots.get("phone")
        if phone:
            callback = self._dispatch(
                state,
                "create_callback",
                {
                    "phone": phone,
                    "reason": HANDOFF_REASON,
                    "summary": "Anruf konnte nicht automatisch abgeschlossen werden.",
                },
            )
            apply_tool_result(state, "create_callback", callback)
            if callback.ok:
                return TurnResult(
                    state=state, say=[callback.say or fallback_say], ended=True
                )

        state.stage = "ended"
        return TurnResult(state=state, say=[fallback_say], ended=True)


def _tool_result_as_input(name: str, result: ToolResult) -> str:
    """Ergebnis dem Modell als nächster Zug zurückgeben, ohne echten Kundeninput."""
    if result.ok:
        payload = {"tool": name, "ok": True, "data": result.data}
    else:
        payload = {"tool": name, "ok": False, "error_code": result.error_code}
    if result.say:
        # Die vorgeschriebene Formulierung heikler Fälle (ausverkauft, außerhalb
        # der Zone, ...) kommt aus dem Code, nicht vom Modell (docs/05 §5) - ohne
        # sie hier weiterzugeben, müsste das Modell aus dem bloßen error_code
        # selbst improvisieren.
        payload["say"] = result.say
    return json.dumps(payload, default=str, ensure_ascii=False)
