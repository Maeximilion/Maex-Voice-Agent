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
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, get_args

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.agent import escalation, guards
from api.agent.dispatch import ToolResult, dispatch, log_refused
from api.agent.ladder import UnderstandingLadder
from api.agent.llm import LLMClient, LLMError
from api.agent.state import (
    ConversationState,
    apply_state_patch,
    apply_tool_result,
    note_intent,
)
from api.config import settings
from api.core.envelope import SAY_ON_FAILURE
from api.core.logging import get_logger, log
from api.core.time import to_local, utcnow
from api.domain.reservations.spoken import WEEKDAYS
from api.models import Tenant
from api.schemas.callbacks import CallbackReason

logger = get_logger("api.agent.loop")

# Schutz gegen ein Modell, das sich zwischen Tool-Aufrufen verheddert und nie zu
# einem Satz für den Kunden kommt: lieber sauber abbrechen als den Anruf endlos
# im Loop stehen zu lassen.
MAX_TOOL_HOPS = 6
SAY_TIMEOUT = "Wir sind jetzt schon eine Weile dran. Ich gebe an das Team weiter."
SAY_STUCK = "Da komme ich gerade nicht weiter. Ich gebe an das Team weiter."
SAY_ESCALATION = "Ich verbinde Sie sofort mit dem Team."
SAY_NOT_UNDERSTOOD = "Da komme ich gerade nicht weiter. Ich gebe an das Team weiter."
# The last resort of `_handoff`: the team is not reachable and there is no number
# for a callback. Every sentence above promises the team; here nobody takes
# over, so the guest hears exactly that (Codex PR #208, P1). Draft wording,
# checked with the announcement texts of docs/05 §6.
SAY_NOBODY_REACHABLE = (
    "Ich kann Ihnen gerade leider nicht weiterhelfen und erreiche im Restaurant "
    "niemanden. Bitte rufen Sie später noch einmal an."
)

ENDED_STAGES = frozenset({"transferred", "ended"})
# Name under which a refused order of the model (`LLMTurn.cart`) goes back to
# it and into `calls.tool_calls`. No tool: nothing is dispatched under it.
CART = "cart"

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
        # Timezone of the call's tenant, read once (`_local_now`).
        self._zone: str | None = None

    def run_turn(self, state: ConversationState, user_text: str) -> TurnResult:
        reason = escalation.check(user_text)
        if reason is not None:
            return self._handoff(state, SAY_ESCALATION, reason=reason, detail=user_text)

        # Before the model: the request of this very turn carries it already.
        note_intent(state, user_text)
        start = guards.begin_turn(state)
        pending_input = user_text
        # Ein Feld zählt höchstens einmal je Kundenzug: ohne das könnte ein Modell,
        # das denselben Fehlversuch über mehrere Tool-Hops hinweg noch einmal
        # meldet (erst beim Tool-Aufruf, dann noch einmal in der Antwort), die
        # Leiter mit weniger als den vorgesehenen zwei echten Kundenversuchen je
        # Stufe hochtreiben (Codex-Review PR #102, P2).
        reported_failures: set[str] = set()
        # The tools the model called in this guest turn, refused ones and a
        # refused `cart` included.
        called: list[str] = []
        for _ in range(MAX_TOOL_HOPS):
            if self._clock() - self._started > self._max_call_seconds:
                return self._handoff(state, SAY_TIMEOUT, detail=user_text)

            try:
                turn = self._llm.next_turn(
                    self._system_prompt,
                    # On a tool hop the input is the tool result: the guest's
                    # sentence of this turn rides along, or the model no
                    # longer knows what the result is for.
                    self._prompt_state(
                        state,
                        None if pending_input is user_text else user_text,
                        called,
                    ),
                    pending_input,
                )
            except LLMError as exc:
                # A model that is down or answers outside the contract is an
                # outage: the call goes to the team, never into a retry loop
                # with the guest waiting (CLAUDE.md §2 rule 5).
                log(
                    logger,
                    logging.ERROR,
                    "model failed, handing over to the team",
                    call_id=str(state.call_id),
                    error=str(exc),
                )
                return self._handoff(state, SAY_ON_FAILURE, detail=user_text)
            if turn.state_patch:
                # Only what was taken counts as understood: a name outside
                # `GUEST_SLOTS` is dropped and is no success on the ladder.
                for field_name in apply_state_patch(state, turn.state_patch):
                    self._ladder.record_success(field_name)
            # The limit is checked again once the model has answered: a slow
            # answer that arrives after the limit is neither spoken nor
            # dispatched, the call goes to the team (Codex PR #208, P2). What
            # the model heard is taken first: a phone number from this very
            # sentence is what the callback of the handoff needs when the
            # team is not reachable (Codex PR #222, P1).
            if self._clock() - self._started > self._max_call_seconds:
                return self._handoff(state, SAY_TIMEOUT, detail=user_text)

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

            if turn.cart is not None:
                # Rule 2 for what a model keeps across turns (guards.py). A
                # refused order stops the whole answer: neither its sentence
                # nor its tool call is carried out on an order the core did
                # not take. It costs a tool hop like any refused call.
                refused = guards.take_cart(state, turn.cart)
                if refused is not None:
                    log_refused(
                        self._session, state.call_id, state.tenant_id, CART, refused
                    )
                    called.append(CART)
                    pending_input = _tool_result_as_input(CART, refused)
                    continue

            if turn.tool_call is None:
                say = turn.say
                assert say is not None  # LLMTurn garantiert genau eins von beidem
                # What the guest hears decides which offered dishes count as
                # put to them (guards.py, rule 2).
                guards.note_said(state, say)
                return TurnResult(
                    state=state, say=[say], ended=state.stage in ENDED_STAGES
                )

            # Hard rules 2 and 3 are held here, not by the prompt (guards.py).
            refused = guards.refusal(
                state,
                start,
                user_text,
                turn.tool_call,
                cart_sent=turn.cart is not None,
            )
            if refused is not None:
                log_refused(
                    self._session,
                    state.call_id,
                    state.tenant_id,
                    turn.tool_call.name,
                    refused,
                )
                result = refused
            else:
                result = self._dispatch(state, turn.tool_call.name, turn.tool_call.args)
            apply_tool_result(state, turn.tool_call.name, result, turn.tool_call.args)
            called.append(turn.tool_call.name)
            pending_input = _tool_result_as_input(turn.tool_call.name, result)

        return self._handoff(state, SAY_STUCK, detail=user_text)

    def _prompt_state(
        self,
        state: ConversationState,
        guest_said: str | None = None,
        called: list[str] | None = None,
    ) -> dict[str, Any]:
        data = state.to_prompt_json()
        if guest_said:
            # Within one guest turn only: the next turn starts from the
            # compact state again, never from a transcript (docs/05 §5).
            data["guest_said"] = guest_said
        if called:
            # Also within the turn only. A model sees one tool result at a
            # time and not that it asked for it: without this a real model
            # called get_service_status again on its own result until the
            # hop limit, and the call ended with the team.
            data["called"] = list(called)
        # A model has no clock: "morgen um sieben" and "am Samstag" need today's
        # date and weekday. Local time of the restaurant, from the same clock
        # the tools get (`now`), so a replay with a fixed time stays the same.
        local = self._local_now(state)
        data["now"] = (
            f"{WEEKDAYS[local.weekday()]}, {local.isoformat(timespec='minutes')}"
        )
        hints = self._ladder.active_levels()
        if hints:
            # Sagt dem Modell, auf welcher Verständnis-Stufe eine Information
            # gerade steht (docs/05 §2), ohne den Verlauf mitzuschicken.
            data["ladder"] = hints
        return data

    def _local_now(self, state: ConversationState) -> datetime:
        """In the timezone of the tenant row, which is what the tools compute
        with: the day the model reads and the day `check_slot` means must be
        the same one, also around midnight. Read once per call; an unknown
        tenant falls back to the setting, the tools then answer `not_found`."""
        if self._zone is None:
            self._zone = (
                self._session.scalar(
                    select(Tenant.timezone).where(Tenant.id == state.tenant_id)
                )
                or settings.tenant_timezone
            )
        return to_local(self._now or utcnow(), self._zone)

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
        `fallback_say` is spoken only when a transfer or a callback really
        happened and its tool gave no sentence; the last resort has its own.

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
        return TurnResult(state=state, say=[SAY_NOBODY_REACHABLE], ended=True)


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
    if result.hint:
        # Why the core refused the call (guards.py): without it the model sees
        # only an error code and has nothing to act on.
        payload["hint"] = result.hint
    if result.say:
        # Die vorgeschriebene Formulierung heikler Fälle (ausverkauft, außerhalb
        # der Zone, ...) kommt aus dem Code, nicht vom Modell (docs/05 §5) - ohne
        # sie hier weiterzugeben, müsste das Modell aus dem bloßen error_code
        # selbst improvisieren.
        payload["say"] = result.say
    return json.dumps(payload, default=str, ensure_ascii=False)
