"""Verträge der Reservierungs-Tools (docs/04)."""

from datetime import datetime
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, Field

from api.schemas.common import ToolRequest


class CheckSlotRequest(ToolRequest):
    reserved_for: AwareDatetime
    party_size: int = Field(ge=1)


class SlotCheck(BaseModel):
    available: bool
    alternatives: list[datetime] = Field(default_factory=list)
    say: str | None = Field(default=None, exclude=True)


class CreateReservationRequest(ToolRequest):
    idempotency_key: str
    guest_name: str
    phone: str
    party_size: int = Field(ge=1)
    reserved_for: AwareDatetime
    note: str | None = None


class CreateReservationResponse(BaseModel):
    reservation_id: UUID
    status: str
    readback: str
