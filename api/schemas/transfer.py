"""Vertrag des Übergabe-Tools (docs/04 §transfer_to_team)."""

from typing import Literal

from pydantic import BaseModel, Field

from api.schemas.common import ToolRequest

# Eigener Typ statt CallbackReason: Storno ist laut docs/05 §"Harte Regeln" ein
# eigener Sofort-Auslöser für transfer_to_team, aber kein Rückruf-Grund in
# docs/03 (CALLBACK_REASONS). Ein Alias hätte ihn stillschweigend abgelehnt
# (Codex-Review PR #98, P1).
# "outage" is the core's own: a fault of ours ended the turn (model down, too
# slow or in circles, past the call limit), told apart from a guest it did not
# understand in calls.transfer_reason (#769). A model never names it
# (`agent/loop.py`).
TransferReason = Literal[
    "complaint",
    "not_understood",
    "human_requested",
    "out_of_scope",
    "cancellation",
    "outage",
]


class TransferToTeamRequest(ToolRequest):
    reason: TransferReason


class TransferResult(BaseModel):
    transfer_to: str
    available: bool
    say: str | None = Field(default=None, exclude=True)
