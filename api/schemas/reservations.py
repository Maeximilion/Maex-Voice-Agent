"""Verträge der Reservierungs-Tools (docs/04)."""

import uuid
from datetime import datetime
from typing import Literal

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
    idempotency_key: str = Field(min_length=1, max_length=128)
    guest_name: str = Field(min_length=1, max_length=200)
    phone: str = Field(min_length=3, max_length=40)
    party_size: int = Field(ge=1)
    reserved_for: AwareDatetime
    note: str | None = Field(default=None, max_length=500)


class ReservationDraft(BaseModel):
    reservation_id: uuid.UUID
    status: Literal["draft", "confirmed", "cancelled"]
    reserved_for: datetime
    party_size: int
    guest_name: str
    phone: str
    note: str | None = None
    readback: str
