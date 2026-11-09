"""The customer file provisioning reads (#573, docs/13 §8, deploy/customer.example.json).

What a new customer's stack needs in its database before the first call: the
restaurant, its time zone and state, the team number, the weekly hours per
service and the seats for reservations. Everything else has its own import
(menu: docs/14, delivery zones: `scripts/seed_zones.py`) or belongs to the
team on the tablet.
"""

from datetime import time
from typing import Annotated, Literal, Self, get_args
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from api.models.tenants import CUSTOMER_KEY, HOLIDAY_REGIONS

Day = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
# Monday is 0, as `opening_hours.weekday` counts.
DAYS: tuple[str, ...] = get_args(Day)
Service = Literal["dinein", "pickup", "delivery"]
DAY_SECONDS = 24 * 3600


def _wall_clock(value: time) -> time:
    # The columns hold the restaurant's local time; an offset would be dropped.
    if value.tzinfo is not None:
        raise ValueError("a time of day without an offset, like 11:30")
    return value


def _once_each(values: list[str]) -> list[str]:
    if len(set(values)) != len(values):
        raise ValueError("names a value twice")
    return values


Clock = Annotated[time, AfterValidator(_wall_clock)]
Days = Annotated[list[Day], Field(min_length=1), AfterValidator(_once_each)]
Services = Annotated[list[Service], Field(min_length=1), AfterValidator(_once_each)]


def _refuse_overlap(what: str, spans: list[tuple[str, time, time]]) -> None:
    """Two windows sharing a moment of the week would be read twice, and one of
    no length is a typo. A window past midnight runs into the next day, past
    Sunday into Monday."""

    def second(value: time) -> int:
        return value.hour * 3600 + value.minute * 60 + value.second

    ranges = []
    for day, start, end in spans:
        length = (second(end) - second(start)) % DAY_SECONDS
        if not length:
            raise ValueError(f"{what}: {day} {start:%H:%M} starts and ends at once")
        ranges.append(
            (DAYS.index(day) * DAY_SECONDS + second(start), length, day, start)
        )
    if not ranges:
        return
    ranges.sort()
    first = ranges[0]
    following = [*ranges[1:], (first[0] + 7 * DAY_SECONDS, *first[1:])]
    for (begin, length, day, start), (after, _, day2, start2) in zip(
        ranges, following, strict=True
    ):
        if begin + length > after:
            raise ValueError(
                f"{what}: {day} {start:%H:%M} overlaps {day2} {start2:%H:%M}"
            )


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class HoursWindow(_Strict):
    """Open from `opens` to `closes` on these days for these services; a
    `closes` before `opens` runs past midnight, as `domain/status/hours.py` reads it."""

    days: Days
    services: Services
    opens: Clock
    closes: Clock


class SeatsWindow(_Strict):
    """Up to `guests` seated guests in one slot of this window (`capacity`)."""

    days: Days
    start: Clock
    end: Clock
    guests: int = Field(ge=0)
    slot_minutes: int = Field(default=30, gt=0, le=240)


class CustomerSpec(_Strict):
    key: str = Field(pattern=CUSTOMER_KEY, max_length=32)
    name: str = Field(min_length=1, max_length=200)
    timezone: str = "Europe/Berlin"
    # Empty: the stack's TEAM_PHONE. Either way one the dialplan dials.
    team_phone: str | None = None
    holiday_region: str | None = None
    privacy_notice_address: str | None = None
    hours: list[HoursWindow] = Field(min_length=1)
    seats: list[SeatsWindow] = []

    @field_validator("timezone")
    @classmethod
    def _known_zone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(f"unknown time zone {value!r}") from None
        return value

    @field_validator("holiday_region")
    @classmethod
    def _state(cls, value: str | None) -> str | None:
        if value is not None and value not in HOLIDAY_REGIONS:
            raise ValueError(f"one of {', '.join(HOLIDAY_REGIONS)}")
        return value

    @field_validator("team_phone", "privacy_notice_address")
    @classmethod
    def _blank_is_none(cls, value: str | None) -> str | None:
        return value or None

    @model_validator(mode="after")
    def _one_reading_per_moment(self) -> Self:
        for service in get_args(Service):
            _refuse_overlap(
                f"hours of {service}",
                [
                    (day, w.opens, w.closes)
                    for w in self.hours
                    if service in w.services
                    for day in w.days
                ],
            )
        _refuse_overlap(
            "seats", [(day, w.start, w.end) for w in self.seats for day in w.days]
        )
        return self
