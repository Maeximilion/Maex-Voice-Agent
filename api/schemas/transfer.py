"""Vertrag des Übergabe-Tools (docs/04 §transfer_to_team)."""

from pydantic import BaseModel, Field

from api.schemas.callbacks import CallbackReason
from api.schemas.common import ToolRequest

TransferReason = CallbackReason


class TransferToTeamRequest(ToolRequest):
    reason: TransferReason


class TransferResult(BaseModel):
    transfer_to: str
    available: bool
    say: str | None = Field(default=None, exclude=True)
