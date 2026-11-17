"""The standard weekly hours, changed by the owner behind the admin PIN (#571,
D34 point 3, docs/23 §6).

`opening_hours` holds one row per span: weekday, service, start and end on the
restaurant's clock. The owner replaces the spans of one weekday; every reader
(`status/hours.py`) sees the new rows from its next read on. A span whose end
is not after its start runs past midnight, and is taken only with the owner's
word (`overnight`), as for a date's hours (#596): swapped times would open the
night of every week to come. Two spans of one service neither overlap nor
touch: every reader takes a span's end for a closing time, so 11:30 to 14:00
and 14:00 to 22:00 would close the kitchen at 14:00 for the agent and refuse
the last tables before it. For the same reason a span past midnight ends
before the next weekday's first span of the service starts, Sunday's before
Monday's: every reader looks at the day before first, so a night that reached
into the next day's span would close it at the night's end.

Hours are compared as the form shows them, to the minute, and service by
service: a service the owner did not change is neither checked nor written, so
a row the form cannot show exactly (a second, more spans than the form takes)
stays as it is and never blocks the other services.

A change leaves an audit row with the hours before and after. It pushes
nothing: the push of D34 point 4 tells the owner what the staff changed.
"""

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import datetime, time
from itertools import pairwise
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from api.core import time as clock
from api.core.errors import Conflict, InvalidInput
from api.domain.reservations.spoken import WEEKDAYS
from api.domain.status.hours import load_hours, open_window_at
from api.domain.status.staff_changes import (
    ACTOR_OWNER,
    NO_SPAN,
    OVERNIGHT,
    BookingToCall,
    _locked,
)
from api.models import AuditLog, OpeningHours, Reservation, Tenant
from api.models.tenants import SERVICES

DAY_MINUTES = 24 * 60
# A bound on what one form can write, not a rule about the restaurant.
MAX_SPANS = 4
DINEIN = "dinein"
OVERLAP = (
    "Zwei Zeiten überschneiden sich oder schließen direkt aneinander an. "
    "Bitte als eine Zeit eintragen."
)
NIGHT_OVERLAP = (
    "Die Zeit über Mitternacht am {night} reicht in die Zeiten am {next} hinein "
    "oder schließt direkt daran an. Bitte eine der beiden Zeiten ändern."
)
TOO_MANY = f"Höchstens {MAX_SPANS} Zeiten je Tag und Bereich."
CHANGED_SINCE = (
    "Die Zeiten dieses Tages haben sich gerade geändert. Bitte noch einmal prüfen."
)


@dataclass(frozen=True, order=True)
class Span:
    """One opening span on the restaurant's clock. `overnight` is the owner's
    word that an end before the start means the next day; it is no part of
    what a span is, so two spans with the same times are the same span."""

    opens: time
    closes: time
    overnight: bool = field(default=False, compare=False)

    def text(self) -> str:
        return f"{self.opens:%H:%M}-{self.closes:%H:%M}"


Day = dict[str, list[Span]]


def week(session: Session, tenant_id: uuid.UUID) -> dict[int, Day]:
    """Every weekday (Monday is 0) with the spans of every service, by start."""
    days: dict[int, Day] = {
        weekday: {service: [] for service in SERVICES} for weekday in range(7)
    }
    rows = session.scalars(
        select(OpeningHours)
        .where(OpeningHours.tenant_id == tenant_id)
        .execution_options(populate_existing=True)
    )
    for row in rows:
        days[row.weekday][row.service].append(_span(row))
    for day in days.values():
        for spans in day.values():
            spans.sort()
    return days


def token(day: Day) -> str:
    """What a form saw of one weekday: a save against other hours is refused."""
    text = ";".join(
        f"{service}={','.join(span.text() for span in day[service])}"
        for service in SERVICES
    )
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def set_weekday(
    session: Session,
    tenant_id: uuid.UUID,
    weekday: int,
    spans: Day,
    seen: str,
    actor: str = ACTOR_OWNER,
) -> bool:
    """Replace the spans of one weekday for the services named; a service left
    out keeps its hours, and so does one whose spans read as they are stored.
    `seen` is the `token` of the hours the form showed: hours changed since (a
    second device) raise Conflict, so a save never writes over hours the owner
    did not see. False when nothing changes, whatever the token: then no row
    is written, and a save tapped twice is no conflict. Raises InvalidInput.
    Commits."""
    if weekday not in range(7) or not set(spans) <= set(SERVICES):
        raise InvalidInput(f"no such weekday or service: {weekday}, {sorted(spans)}")
    # The lock of the staff changes: their "shorter than now" reads these rows.
    _locked(session, tenant_id)
    stored = week(session, tenant_id)
    before = stored[weekday]
    after = {**before, **{service: sorted(typed) for service, typed in spans.items()}}
    shown, asked = _texts(before), _texts(after)
    changed = [service for service in SERVICES if asked[service] != shown[service]]
    if not changed:
        session.commit()
        return False
    if seen != token(before):
        session.rollback()
        raise Conflict(f"weekday {weekday} changed since shown", say=CHANGED_SINCE)
    try:
        for service in changed:
            _checked(after[service])
            _checked_nights(stored, weekday, service, after[service])
    except InvalidInput:
        session.rollback()
        raise
    session.execute(
        delete(OpeningHours).where(
            OpeningHours.tenant_id == tenant_id,
            OpeningHours.weekday == weekday,
            OpeningHours.service.in_(changed),
        )
    )
    session.add_all(
        OpeningHours(
            tenant_id=tenant_id,
            weekday=weekday,
            service=service,
            opens_at=span.opens,
            closes_at=span.closes,
        )
        for service in changed
        for span in after[service]
    )
    session.add(
        AuditLog(
            tenant_id=tenant_id,
            actor=actor,
            action="opening_hours.changed",
            entity="opening_hours",
            payload={"weekday": weekday, "from": shown, "to": asked},
        )
    )
    session.commit()
    return True


def tables_outside_hours(
    session: Session, tenant_id: uuid.UUID, now: datetime | None = None
) -> list[BookingToCall]:
    """The confirmed tables from now on that no dine-in window holds, as the
    hours stand: the weekly ones and every special day, less the tables the
    team booked outside the hours on purpose (`off_hours`, #819). The team
    calls these guests itself (#710, Maxi 2026-11-13): nothing is cancelled
    and no guest is messaged. By time, on the restaurant's clock."""
    now = now or clock.utcnow()
    tz = ZoneInfo(session.get_one(Tenant, tenant_id).timezone)
    rows = session.scalars(
        select(Reservation)
        .where(
            Reservation.tenant_id == tenant_id,
            Reservation.status == "confirmed",
            Reservation.deleted_at.is_(None),
            Reservation.reserved_for >= now,
            # The team never calls a self-test's guest (#574).
            Reservation.is_test.is_(False),
            # Nor one it booked outside the hours on purpose (#819).
            Reservation.off_hours.is_(False),
        )
        .order_by(Reservation.reserved_for)
    ).all()
    if not rows:
        return []
    first = rows[0].reserved_for.astimezone(tz).date()
    last = rows[-1].reserved_for.astimezone(tz).date()
    # One read of the hours for the whole span; `load_hours` takes the day
    # before too, whose night may hold a table after midnight.
    hours = load_hours(session, tenant_id, first, days=(last - first).days)
    return [
        BookingToCall(
            r.guest_name, r.reserved_for.astimezone(tz), r.phone, r.party_size
        )
        for r in rows
        if open_window_at(hours, r.reserved_for, DINEIN, tz) is None
    ]


# --- helpers ---------------------------------------------------------------------


def _span(row: OpeningHours) -> Span:
    return Span(row.opens_at, row.closes_at, row.closes_at <= row.opens_at)


def _minutes(span: Span) -> tuple[int, int]:
    start = span.opens.hour * 60 + span.opens.minute
    end = span.closes.hour * 60 + span.closes.minute
    return start, end + DAY_MINUTES if end <= start else end


def _checked(spans: list[Span]) -> None:
    if len(spans) > MAX_SPANS:
        raise InvalidInput("too many spans", say=TOO_MANY)
    for span in spans:
        if span.opens == span.closes:
            raise InvalidInput("a span without a length", say=NO_SPAN)
        if span.closes < span.opens and not span.overnight:
            raise InvalidInput("a span past midnight not confirmed", say=OVERNIGHT)
    ends = sorted(_minutes(span) for span in spans)
    if any(later[0] <= earlier[1] for earlier, later in pairwise(ends)):
        raise InvalidInput("spans overlap or touch", say=OVERLAP)


def _checked_nights(
    stored: dict[int, Day], weekday: int, service: str, spans: list[Span]
) -> None:
    """A span past midnight ends before the next weekday's first span of the
    service starts, and the night of the weekday before ends before these
    start. `hours.open_window_at` reads the day before first: a night that
    reached into the next day's span would be the window found there, and its
    end the closing time (review of #820). The neighbours as they are stored."""
    before, after = (weekday - 1) % 7, (weekday + 1) % 7
    for night, late, early in (
        (before, stored[before][service], spans),
        (weekday, spans, stored[after][service]),
    ):
        # Minutes into the next day; below zero without a span past midnight.
        reach = max((_minutes(span)[1] for span in late), default=0) - DAY_MINUTES
        if any(_minutes(span)[0] <= reach for span in early):
            raise InvalidInput(
                "a span past midnight reaches the next weekday's spans",
                say=NIGHT_OVERLAP.format(
                    night=WEEKDAYS[night], next=WEEKDAYS[(night + 1) % 7]
                ),
            )


def _texts(day: Day) -> dict[str, list[str]]:
    """Each service's spans to the minute, in the order of the texts: stored
    rows sort by their seconds too, typed ones cannot."""
    return {
        service: sorted(span.text() for span in day[service]) for service in SERVICES
    }
