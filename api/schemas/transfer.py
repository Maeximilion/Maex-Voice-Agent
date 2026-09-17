"""Vertrag des Übergabe-Tools (docs/04 §transfer_to_team)."""

from typing import Literal

from pydantic import BaseModel, Field

from api.schemas.common import ToolRequest

# Eigener Typ statt CallbackReason: Storno ist laut docs/05 §"Harte Regeln" ein
# eigener Sofort-Auslöser für transfer_to_team, aber kein Rückruf-Grund in
# docs/03 (CALLBACK_REASONS). Ein Alias hätte ihn stillschweigend abgelehnt
# (Codex-Review PR #98, P1).
TransferReason = Literal[
    "complaint", "not_understood", "human_requested", "out_of_scope", "cancellation"
]


class TransferToTeamRequest(ToolRequest):
    reason: TransferReason


class TransferResult(BaseModel):
    transfer_to: str
    available: bool
    say: str | None = Field(default=None, exclude=True)
