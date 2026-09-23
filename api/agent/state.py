"""Kompakter Gesprächszustand statt wachsendem Verlauf (docs/05 §5).

Nach jedem Zug wird der Stand zusammengefasst und dem Modell mitgegeben, statt den
ganzen bisherigen Wortlaut erneut zu schicken: der Zustand wächst nicht mit der
Gesprächsdauer, der Verlauf schon.
"""

import uuid
from contextlib import suppress
from typing import Any, Literal

from pydantic import BaseModel, Field

from api.agent.dispatch import ToolResult
from api.core.errors import InvalidInput
from api.domain.customers.phone import normalize_phone

Stage = Literal[
    "start",
    "collecting",
    "readback_pending",
    "confirmed",
    "callback",
    "transferred",
    "ended",
]


class ConversationState(BaseModel):
    call_id: uuid.UUID
    tenant_id: uuid.UUID
    intent: str | None = None
    stage: Stage = "start"
    slots: dict[str, Any] = Field(default_factory=dict)
    open_questions: list[str] = Field(default_factory=list)
    reservation_id: uuid.UUID | None = None
    order_id: uuid.UUID | None = None
    transferred: bool = False

    def to_prompt_json(self) -> dict[str, Any]:
        """Format aus docs/05 §5: geht bei jedem Zug ans Modell statt des Verlaufs."""
        data: dict[str, Any] = {"stage": self.stage, "open": self.open_questions}
        if self.intent:
            data["intent"] = self.intent
        if self.slots:
            data["slots"] = self.slots
        if self.reservation_id:
            # Ohne das kann ein zustandsloses LLMClient auf dem "Ja" nach dem
            # readback kein entity_id für `confirm` liefern (Codex-Review PR #101, P1).
            data["reservation_id"] = str(self.reservation_id)
        if self.order_id:
            # Dasselbe fuer Bestellungen: confirm braucht entity_id.
            data["order_id"] = str(self.order_id)
        return data


def initial_state(
    call_id: uuid.UUID, tenant_id: uuid.UUID, caller_id: str | None = None
) -> ConversationState:
    """Zustand zu Gespraechsbeginn. Der Gast ruft an, seine Nummer kennt die
    Rufnummernerkennung schon: sie steht vorab in `slots.phone`, und der Agent
    fragt nicht danach (Maxi, PR #127). Nennt der Gast eine andere, ueberschreibt
    sein `state_patch` sie. Unterdrueckt oder keine gueltige Nummer: der Slot
    bleibt leer, und der Agent fragt wie bisher."""
    slots: dict[str, Any] = {}
    if caller_id:
        with suppress(InvalidInput):
            slots["phone"] = normalize_phone(caller_id)
    return ConversationState(call_id=call_id, tenant_id=tenant_id, slots=slots)


def apply_state_patch(state: ConversationState, patch: dict[str, Any]) -> None:
    """Vom Modell gelieferte Gesprächsdetails in den kompakten Zustand übernehmen
    (`LLMTurn.state_patch`), bevor sie mit dem nächsten Zug verloren gehen. Reine
    Gesprächsangaben (Name, Datum, Personenzahl, ...), keine Fachdaten aus der DB
    (CLAUDE.md §2 Regel 1 betrifft Preise/Zeiten/Verfügbarkeit/Allergene, nicht das,
    was der Gast gesagt hat)."""
    state.slots.update(patch)


def apply_tool_result(state: ConversationState, name: str, result: ToolResult) -> None:
    """Schreibt ein erfolgreiches Tool-Ergebnis in den Zustand. Fehlschläge ändern
    den Zustand nicht: das Modell entscheidet auf Basis von `say`/`error_code`,
    was als Nächstes versucht wird (Verständnis-Leiter, T-2.2)."""
    if not result.ok:
        return
    # Aktiv ist der Vorgang, der zuletzt vorgelesen wurde: wechselt der Gast
    # zwischen Reservierung und Bestellung, faellt die andere ID heraus. Sonst
    # stuenden zwei bestaetigbare Vorgaenge im Prompt, und das naechste Ja
    # koennte den verlassenen bestaetigen (Codex PR #127).
    if name == "create_reservation":
        state.intent = "reservation"
        state.reservation_id = uuid.UUID(result.data["reservation_id"])
        state.order_id = None
        state.stage = "readback_pending"
    elif name == "draft_order":
        state.intent = "pickup"
        state.order_id = uuid.UUID(result.data["order_id"])
        state.reservation_id = None
        state.stage = "readback_pending"
    elif name == "confirm":
        state.stage = "confirmed"
    elif name == "create_callback":
        state.stage = "callback"
    elif name == "transfer_to_team" and result.data.get("available"):
        state.transferred = True
        state.stage = "transferred"
