"""Kompakter Gesprächszustand statt wachsendem Verlauf (docs/05 §5).

Nach jedem Zug wird der Stand zusammengefasst und dem Modell mitgegeben, statt den
ganzen bisherigen Wortlaut erneut zu schicken: der Zustand wächst nicht mit der
Gesprächsdauer, der Verlauf schon.
"""

import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field

from api.agent.dispatch import ToolResult

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
    transferred: bool = False

    def to_prompt_json(self) -> dict[str, Any]:
        """Format aus docs/05 §5: geht bei jedem Zug ans Modell statt des Verlaufs."""
        data: dict[str, Any] = {"stage": self.stage, "open": self.open_questions}
        if self.intent:
            data["intent"] = self.intent
        if self.slots:
            data["slots"] = self.slots
        return data


def apply_tool_result(state: ConversationState, name: str, result: ToolResult) -> None:
    """Schreibt ein erfolgreiches Tool-Ergebnis in den Zustand. Fehlschläge ändern
    den Zustand nicht: das Modell entscheidet auf Basis von `say`/`error_code`,
    was als Nächstes versucht wird (Verständnis-Leiter, T-2.2)."""
    if not result.ok:
        return
    if name == "create_reservation":
        state.intent = state.intent or "reservation"
        state.reservation_id = uuid.UUID(result.data["reservation_id"])
        state.stage = "readback_pending"
    elif name == "confirm":
        state.stage = "confirmed"
    elif name == "create_callback":
        state.stage = "callback"
    elif name == "transfer_to_team" and result.data.get("available"):
        state.transferred = True
        state.stage = "transferred"
