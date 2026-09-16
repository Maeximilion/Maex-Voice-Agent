"""Verträge der Reservierungs-Tools (docs/04)."""

from datetime import datetime

from pydantic import AwareDatetime, BaseModel, Field

from api.schemas.common import ToolRequest


class CheckSlotRequest(ToolRequest):
    reserved_for: AwareDatetime
    party_size: int = Field(ge=1)


class SlotCheck(BaseModel):
    available: bool
    alternatives: list[datetime] = Field(default_factory=list)
    say: str | None = Field(default=None, exclude=True)
