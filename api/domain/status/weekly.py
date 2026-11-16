"""The standard weekly hours, changed by the owner behind the admin PIN (#571,
D34 point 3, docs/23 §6).

`opening_hours` holds one row per span: weekday, service, start and end on the
restaurant's clock. The owner replaces the spans of one weekday; every reader
(`status/hours.py`) sees the new rows from its next read on. A span whose end
is not after its start runs past midnight, and is taken only with the owner's
word (`overnight`), as for a date's hours (#596): swapped times would open the
night of every week to come.

A change leaves an audit row with the hours before and after. It pushes
nothing: the push of D34 point 4 tells the owner what the staff changed.
"""

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime, time
from itertools import pairwise
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from api.core import time as clock
from api.core.errors import Conflict, InvalidInput
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
OVERLAP = "Zwei Zeiten überschneiden sich. Bitte Beginn und Ende prüfen."
TOO_MANY = f"Höchstens {MAX_SPANS} Zeiten je Tag und Bereich."
CHANGED_SINCE = (
    "Die Zeiten dieses Tages haben sich gerade geändert. Bitte noch einmal prüfen."
)


@dataclass(frozen=True, order=True)
class Span:
    """One opening span on the restaurant's clock. `overnight` is the owner's
    word that an end before the start means the next day."""

    opens: time
    closes: time
    overnight: bool = False

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
    out keeps its hours. `seen` is the `token` of the hours the form showed:
    hours changed since (a second device) raise Conflict, so a save never
    writes over hours the owner did not see. False when nothing changes, and
    then no row is written. Raises InvalidInput. Commits."""
    if weekday not in range(7) or not set(spans) <= set(SERVICES):
        raise InvalidInput(f"no such weekday or service: {weekday}, {sorted(spans)}")
    for typed in spans.values():
        _checked(typed)
    # The lock of the staff changes: their "shorter than now" reads these rows.
    _locked(session, tenant_id)
    before = week(session, tenant_id)[weekday]
    if seen != token(before):
        session.rollback()
        raise Conflict(f"weekday {weekday} changed since shown", say=CHANGED_SINCE)
    after = {
        service: sorted(_stored(span) for span in spans.get(service, before[service]))
        for service in SERVICES
    }
    if after == before:
        session.commit()
        return False
    changed = [service for service in SERVICES if after[service] != before[service]]
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
            payload={
                "weekday": weekday,
                "from": _texts(before),
                "to": _texts(after),
            },
        )
    )
    session.commit()
    return True


def tables_outside_hours(
    session: Session, tenant_id: uuid.UUID, now: datetime | None = None
) -> list[BookingToCall]:
    """The confirmed tables from now on that no dine-in window holds, as the
    hours stand: the weekly ones and every special day. The team calls these
    guests itself (#710, Maxi 2026-11-13): nothing is cancelled and no guest
    is messaged. By time, on the restaurant's clock."""
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


def _stored(span: Span) -> Span:
    """As `week` reads it back, so "nothing changed" compares like with like."""
    return Span(span.opens, span.closes, span.closes <= span.opens)


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
    if any(later[0] < earlier[1] for earlier, later in pairwise(ends)):
        raise InvalidInput("spans overlap", say=OVERLAP)


def _texts(day: Day) -> dict[str, list[str]]:
    return {service: [span.text() for span in day[service]] for service in SERVICES}
