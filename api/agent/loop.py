"""Kundenzug → Modell → Tool-Aufrufe → Antwort; Abbruch bei `max_call_seconds` (docs/11 §agent).

Zwei Prüfungen laufen vor jedem Modell-Aufruf, nicht danach (docs/11 §agent:
"escalation.py prüft vor dem Modell"): `escalation.check()` auf dem rohen
Kundentext (Beschwerde, Mensch-Wunsch, Storno — docs/05 §4) und die
Verständnis-Leiter (`ladder.py`, docs/05 §2), die Fehlversuche je Information
zählt und nach drei Stufenwechseln ohne Erfolg selbst eskaliert. Bricht der
Loop aus eigenem Antrieb ab (Zeitlimit, zu viele Tool-Hops, Leiter erschöpft),
übernimmt er die Übergabe an das Team wirklich, statt sie dem Kunden nur
anzukündigen (CLAUDE.md §2 Regel 5: kein Anruf geht verloren).
"""

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, get_args

from sqlalchemy.orm import Session

from api.agent import escalation
from api.agent.dispatch import ToolResult, dispatch
from api.agent.ladder import UnderstandingLadder
from api.agent.llm import LLMClient
from api.agent.state import ConversationState, apply_state_patch, apply_tool_result
from api.config import settings
from api.schemas.callbacks import CallbackReason

# Schutz gegen ein Modell, das sich zwischen Tool-Aufrufen verheddert und nie zu
# einem Satz für den Kunden kommt: lieber sauber abbrechen als den Anruf endlos
# im Loop stehen zu lassen.
MAX_TOOL_HOPS = 6
SAY_TIMEOUT = "Wir sind jetzt schon eine Weile dran. Ich gebe an das Team weiter."
SAY_STUCK = "Da komme ich gerade nicht weiter. Ich gebe an das Team weiter."
SAY_ESCALATION = "Ich verbinde Sie sofort mit dem Team."
SAY_NOT_UNDERSTOOD = "Da komme ich gerade nicht weiter. Ich gebe an das Team weiter."

ENDED_STAGES = frozenset({"transferred", "ended"})

_CALLBACK_REASONS = frozenset(get_args(CallbackReason))
# Fallback-Grund für Rückrufe, wenn der eigentliche Grund (z. B. "cancellation")
# in CallbackReason gar nicht existiert (docs/03: Storno ist nur ein
# Transfer-Grund, kein Rückruf-Grund — siehe api/schemas/transfer.py).
_CALLBACK_REASON_FALLBACK: CallbackReason = "human_requested"

_HANDOFF_SUMMARIES = {
    "complaint": "Kunde hat sich beschwert.",
    "human_requested": "Kunde wollte mit einem Menschen sprechen.",
    "cancellation": "Kunde wollte etwas stornieren.",
    "not_understood": "Anliegen konnte automatisch nicht geklärt werden.",
}
_DEFAULT_HANDOFF_SUMMARY = "Anruf konnte nicht automatisch abgeschlossen werden."
# Deckelt die Rohtext-Beigabe zur Zusammenfassung (CreateCallbackRequest.summary
# erlaubt bis zu 1000 Zeichen) - großzügig für eine gesprochene Äußerung, aber
# eine harte Grenze statt eines ungeprüften Anhängens.
_SUMMARY_DETAIL_MAX = 400


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
        self._ladder = UnderstandingLadder()

    def run_turn(self, state: ConversationState, user_text: str) -> TurnResult:
        reason = escalation.check(user_text)
        if reason is not None:
            return self._handoff(state, SAY_ESCALATION, reason=reason, detail=user_text)

        pending_input = user_text
        # Ein Feld zählt höchstens einmal je Kundenzug: ohne das könnte ein Modell,
        # das denselben Fehlversuch über mehrere Tool-Hops hinweg noch einmal
        # meldet (erst beim Tool-Aufruf, dann noch einmal in der Antwort), die
        # Leiter mit weniger als den vorgesehenen zwei echten Kundenversuchen je
        # Stufe hochtreiben (Codex-Review PR #102, P2).
        reported_failures: set[str] = set()
        for _ in range(MAX_TOOL_HOPS):
            if self._clock() - self._started > self._max_call_seconds:
                return self._handoff(state, SAY_TIMEOUT, detail=user_text)

            turn = self._llm.next_turn(
                self._system_prompt, self._prompt_state(state), pending_input
            )
            if turn.state_patch:
                apply_state_patch(state, turn.state_patch)
                for field_name in turn.state_patch:
                    self._ladder.record_success(field_name)

            if (
                turn.understanding_failure
                and turn.understanding_failure not in reported_failures
            ):
                reported_failures.add(turn.understanding_failure)
                self._ladder.record_failure(turn.understanding_failure)
                if self._ladder.should_end_call(turn.understanding_failure):
                    return self._handoff(
                        state,
                        SAY_NOT_UNDERSTOOD,
                        reason="not_understood",
                        detail=user_text,
                    )

            if turn.tool_call is None:
                say = turn.say
                assert say is not None  # LLMTurn garantiert genau eins von beidem
                return TurnResult(
                    state=state, say=[say], ended=state.stage in ENDED_STAGES
                )

            result = self._dispatch(state, turn.tool_call.name, turn.tool_call.args)
            apply_tool_result(state, turn.tool_call.name, result)
            pending_input = _tool_result_as_input(turn.tool_call.name, result)

        return self._handoff(state, SAY_STUCK, detail=user_text)

    def _prompt_state(self, state: ConversationState) -> dict[str, Any]:
        data = state.to_prompt_json()
        hints = self._ladder.active_levels()
        if hints:
            # Sagt dem Modell, auf welcher Verständnis-Stufe eine Information
            # gerade steht (docs/05 §2), ohne den Verlauf mitzuschicken.
            data["ladder"] = hints
        return data

    def _dispatch(self, state: ConversationState, name: str, args: dict) -> ToolResult:
        return dispatch(
            self._session, state.call_id, state.tenant_id, name, args, now=self._now
        )

    def _handoff(
        self,
        state: ConversationState,
        fallback_say: str,
        reason: str = "not_understood",
        detail: str | None = None,
    ) -> TurnResult:
        """Übergabe wirklich ausführen statt nur anzukündigen: erst versuchen, live zu
        verbinden; ist niemand erreichbar und kennen wir eine Rufnummer, stattdessen
        einen Rückruf anlegen. Ohne bekannte Rufnummer bleibt nur der ehrliche
        Fallback-Satz — raten (CLAUDE.md §2 Regel 2) ist keine Option.

        `detail` ist der auslösende Kundenzug: ohne ihn bekäme das Team bei einer
        Vorab-Eskalation (kein Modell-Aufruf) nur eine feste Floskel statt der
        eigentlichen Bitte ("Reservierung morgen 18 Uhr auf Müller stornieren"),
        obwohl sonst nirgends ein Transkript gespeichert ist (Codex-Review
        PR #102, P2)."""
        transfer = self._dispatch(state, "transfer_to_team", {"reason": reason})
        apply_tool_result(state, "transfer_to_team", transfer)
        if transfer.ok and transfer.data.get("available"):
            return TurnResult(
                state=state, say=[transfer.say or fallback_say], ended=True
            )

        phone = state.slots.get("phone")
        if phone:
            callback_reason = (
                reason if reason in _CALLBACK_REASONS else _CALLBACK_REASON_FALLBACK
            )
            callback = self._dispatch(
                state,
                "create_callback",
                {
                    "phone": phone,
                    "reason": callback_reason,
                    "summary": _handoff_summary(reason, detail),
                },
            )
            apply_tool_result(state, "create_callback", callback)
            if callback.ok:
                return TurnResult(
                    state=state, say=[callback.say or fallback_say], ended=True
                )

        state.stage = "ended"
        return TurnResult(state=state, say=[fallback_say], ended=True)


def _handoff_summary(reason: str, detail: str | None) -> str:
    base = _HANDOFF_SUMMARIES.get(reason, _DEFAULT_HANDOFF_SUMMARY)
    if not detail:
        return base
    return f"{base} Kunde sagte: {detail[:_SUMMARY_DETAIL_MAX]}"


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
