"""Verträge der Reservierungs-Tools (docs/04)."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from pydantic import AfterValidator, AwareDatetime, BaseModel, Field

from api.schemas.common import ToolRequest

# The slot check looks at the day before and the day after in local time.
_CALENDAR_MARGIN = timedelta(days=2)


def _on_the_calendar(value: datetime) -> datetime:
    """A moment the date arithmetic behind it can handle. The first and the
    last days of `datetime` overflow as soon as a time zone or a day is added:
    not an error the tools know, so a call that named the year 9999 ended as
    an outage. Nothing about how far ahead a table can be booked: that is the
    restaurant's rule and belongs in the database."""
    try:
        at = value.astimezone(UTC)
        _ = at - _CALENDAR_MARGIN, at + _CALENDAR_MARGIN
    except OverflowError as exc:
        raise ValueError("Zeitpunkt liegt außerhalb des Kalenders") from exc
    return value


ReservedFor = Annotated[AwareDatetime, AfterValidator(_on_the_calendar)]


class CheckSlotRequest(ToolRequest):
    reserved_for: ReservedFor
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
    reserved_for: ReservedFor
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
