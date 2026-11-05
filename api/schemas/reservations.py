"""Verträge der Reservierungs-Tools (docs/04)."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from pydantic import AfterValidator, AwareDatetime, BaseModel, Field

from api.schemas.common import ToolRequest

# What the slot check adds to a moment: a time zone (at most 14 hours either
# way) and one day, for the window of the evening before and the one that
# closes after midnight. Not more, or the last days that do work are refused.
_CALENDAR_MARGIN = timedelta(days=1, hours=14)


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
        raise ValueError("moment is outside the calendar") from exc
    return value


ReservedFor = Annotated[AwareDatetime, AfterValidator(_on_the_calendar)]


class CheckSlotRequest(ToolRequest):
    reserved_for: ReservedFor
    party_size: int = Field(ge=1)


class SlotCheck(BaseModel):
    available: bool
    alternatives: list[datetime] = Field(default_factory=list)
    # The party is larger than the agent books itself (`service_config`): no
    # draft, the wish goes to the team as a callback.
    team_callback: bool = False
    say: str | None = Field(default=None, exclude=True)


# The longest note of a table, the guest's allergies included (`agent/allergies.py`).
RESERVATION_NOTE_MAX = 500


class CreateReservationRequest(ToolRequest):
    idempotency_key: str = Field(min_length=1, max_length=128)
    guest_name: str = Field(min_length=1, max_length=200)
    phone: str = Field(min_length=3, max_length=40)
    party_size: int = Field(ge=1)
    reserved_for: ReservedFor
    note: str | None = Field(default=None, max_length=RESERVATION_NOTE_MAX)


ReservationStatus = Literal["draft", "confirmed", "cancelled"]


class ReservationDraft(BaseModel):
    reservation_id: uuid.UUID
    status: ReservationStatus
    reserved_for: datetime
    party_size: int
    guest_name: str
    phone: str
    note: str | None = None
    readback: str


class TabletReservationRequest(BaseModel):
    """A table the team enters by hand (T-3.12): no call, the phone optional."""

    tenant_id: uuid.UUID
    idempotency_key: str = Field(min_length=1, max_length=128)
    guest_name: str = Field(min_length=1, max_length=200)
    phone: str | None = Field(default=None, max_length=40)
    party_size: int = Field(ge=1)
    reserved_for: ReservedFor
    note: str | None = Field(default=None, max_length=RESERVATION_NOTE_MAX)
    # None, one table, or the two tables of a combination of the floor plan.
    table_ids: list[uuid.UUID] = Field(default_factory=list, max_length=2)


class TabletDraft(BaseModel):
    """The entry before the team's confirming tap, with what the tap must know."""

    reservation_id: uuid.UUID
    status: ReservationStatus
    reserved_for: datetime
    party_size: int
    guest_name: str
    phone: str
    note: str | None = None
    tables: list[str] = Field(default_factory=list)
    # The capacity window of the time and the guests it already holds; both
    # None outside every window. A full window or none needs the team's word.
    window_start: datetime | None = None
    window_end: datetime | None = None
    booked_guests: int = 0
    max_guests: int | None = None
    needs_override: bool = False
