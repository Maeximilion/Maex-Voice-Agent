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
into the next day's span would close it at the night's end. The same holds
between the weekday and the dates with hours of their own beside it, or "like"
it, while that night can still be read. A save never brings such an overlap,
and never lets a night cut further into a next day's span that runs on past it;
an old overlap it leaves as it was, or eases, blocks nothing (#877).

Hours are compared as the form shows them, to the minute, and service by
service: a service the owner did not change is neither checked nor written, so
a row the form cannot show exactly (a second, more spans than the form takes)
stays as it is and never blocks the other services.

A refusal names every fault of the save at once (`FieldErrors`), each by its
service and, where it is about one span, that span ("pickup 18:00-01:00"), so
the form can mark the row it came from. The rules are the same whichever fault
comes first: a save is written only when none stands.

A change leaves an audit row with the hours before and after. It pushes
nothing: the push of D34 point 4 tells the owner what the staff changed.
"""

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from itertools import pairwise
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from api.core import time as clock
from api.core.errors import Conflict, FieldErrors, InvalidInput
from api.domain.reservations.spoken import WEEKDAYS
from api.domain.status.hours import (
    HoursData,
    load_hours,
    open_window_at,
    regular_hours,
    windows_for_day,
)
from api.domain.status.staff_changes import (
    ACTOR_OWNER,
    NO_SPAN,
    BookingToCall,
    _locked,
    past_midnight,
)
from api.models import AuditLog, OpeningHours, Reservation, SpecialDay, Tenant
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
    now: datetime | None = None,
) -> bool:
    """Replace the spans of one weekday for the services named; a service left
    out keeps its hours, and so does one whose spans read as they are stored.
    `seen` is the `token` of the hours the form showed: hours changed since (a
    second device) raise Conflict, so a save never writes over hours the owner
    did not see. False when nothing changes, whatever the token: then no row
    is written, and a save tapped twice is no conflict. The night rule reads
    the dates with hours of their own whose nights end after `now`. Raises
    InvalidInput. Commits."""
    if weekday not in range(7) or not set(spans) <= set(SERVICES):
        raise InvalidInput(f"no such weekday or service: {weekday}, {sorted(spans)}")
    # The lock of the staff changes: their "shorter than now" reads these rows.
    _config, zone = _locked(session, tenant_id)
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
    faults: dict[str, str] = {}
    clean: list[str] = []
    for service in changed:
        found = _faults(service, after[service])
        if not found:
            found = _night_faults(
                stored, weekday, service, after[service], before[service]
            )
        faults.update(found)
        if not found:
            # A span fault is keyed by its span, not by the service.
            clean.append(service)
    if clean:
        moment = now or clock.utcnow()
        faults.update(
            _dated_night_faults(session, tenant_id, zone, moment, weekday, clean, after)
        )
    if faults:
        session.rollback()
        raise FieldErrors(f"weekday {weekday}: {sorted(faults)}", faults)
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


def _faults(service: str, spans: list[Span]) -> dict[str, str]:
    """What keeps the spans of one service from being written, by span where
    a fault is about one: too many, one without a length, an end before the
    start without the owner's word; then, when each span holds alone, two
    that overlap or touch (the later one is named)."""
    if len(spans) > MAX_SPANS:
        return {service: TOO_MANY}
    faults = {}
    for span in spans:
        if span.opens == span.closes:
            faults[_key(service, span)] = NO_SPAN
        elif span.closes < span.opens and not span.overnight:
            faults[_key(service, span)] = past_midnight(span.closes)
    if faults:
        return faults
    ordered = sorted(spans, key=_minutes)
    for earlier, later in pairwise(ordered):
        if _minutes(later)[0] <= _minutes(earlier)[1]:
            faults.setdefault(_key(service, later), OVERLAP)
    return faults


def _key(service: str, span: Span) -> str:
    return f"{service} {span.text()}"


def _night_faults(
    stored: dict[int, Day],
    weekday: int,
    service: str,
    spans: list[Span],
    was: list[Span],
) -> dict[str, str]:
    """A span past midnight ends before the next weekday's first span of the
    service starts, and the night of the weekday before ends before these
    start. `hours.open_window_at` reads the day before first: a night that
    reached into the next day's span would be the window found there, and its
    end the closing time (review of #820). The neighbours as they are stored;
    `was` the weekday's spans before the save (`_deepens`)."""
    before, after = (weekday - 1) % 7, (weekday + 1) % 7
    for night, late, early, late_was, early_was in (
        (before, stored[before][service], spans, stored[before][service], was),
        (weekday, spans, stored[after][service], was, stored[after][service]),
    ):
        if _deepens(late, early, late_was, early_was):
            say = NIGHT_OVERLAP.format(
                night=WEEKDAYS[night], next=WEEKDAYS[(night + 1) % 7]
            )
            return {service: say}
    return {}


def _overlap(late: list[Span], early: list[Span]) -> int:
    """How far, in minutes, the night of `late` reaches past the first start
    of `early`, the next day's spans: zero when it ends as that one starts (a
    touch, refused like two spans of one day), below zero when it ends before,
    or when there is no night or no span to reach."""
    reach = max((_minutes(span)[1] for span in late), default=0) - DAY_MINUTES
    first = min((_minutes(span)[0] for span in early), default=DAY_MINUTES)
    return reach - first


def _cut(late: list[Span], early: list[Span]) -> int:
    """How far, in minutes, the night of `late` reaches into the one span of
    `early` that starts inside it, or as it ends, and runs on past it: the
    stretch a reader takes for the night's window and closes at the night's
    end, though that span holds longer. Below zero when no span is cut; a span
    wholly inside the night is read inside it and closes no earlier than it."""
    reach = max((_minutes(span)[1] for span in late), default=0) - DAY_MINUTES
    cuts = (
        reach - start for start, end in map(_minutes, early) if start <= reach < end
    )
    return max(cuts, default=-1)


def _deepens(
    late: list[Span], early: list[Span], late_was: list[Span], early_was: list[Span]
) -> bool:
    """A save that brings a night overlap where there was none, or lets a
    night cut further into a next day's span than before it (#877), also a
    span it held wholly before. An overlap from before the rule, or from a
    writer that did not check it, may stay or ease, so the hours beside it
    can still be shortened or moved out of it."""
    if _overlap(late, early) < 0:
        return False
    if _overlap(late_was, early_was) < 0:
        return True
    return _cut(late, early) > _cut(late_was, early_was)


def night_deepened(
    was: HoursData, will: HoursData, night: date, service: str, zone: ZoneInfo
) -> bool:
    """`_deepens` between the date `night` and the next date, read as every
    reader reads them: a date's own hours, the weekday it is like, or the
    weekly hours. `was` and `will` are the hours before and after a save."""

    def spans(data: HoursData, on: date) -> list[Span]:
        return [
            Span(opens.time(), closes.time())
            for opens, closes in windows_for_day(data, on, service, zone)
        ]

    early = night + timedelta(days=1)
    return _deepens(
        spans(will, night), spans(will, early), spans(was, night), spans(was, early)
    )


def _dated_night_faults(
    session: Session,
    tenant_id: uuid.UUID,
    zone: str,
    now: datetime,
    weekday: int,
    services: list[str],
    after: Day,
) -> dict[str, str]:
    """The night rule between the dates with hours of their own and the dates
    beside them, with the weekday's new spans (#877): a date beside the
    weekday, or one "like" it, would otherwise be read inside a night the
    weekly check never saw. Only nights that end after `now`: a night over
    is read by nobody, and its date is no longer the owner's to change. The
    pairs of two weekly days are `_night_faults`'. A fault names both dates."""
    today = clock.business_day(now, zone)
    rows = session.scalars(
        select(SpecialDay).where(
            SpecialDay.tenant_id == tenant_id,
            SpecialDay.status == "set",
            SpecialDay.date >= today - timedelta(days=1),
        )
    )
    special = {row.date: row for row in rows}
    one = timedelta(days=1)
    nights = sorted(
        {night for day in special for night in (day - one, day) if night + one >= today}
    )
    if not nights:
        return {}
    regular = regular_hours(session, tenant_id)
    kept = tuple(
        row for row in regular if row.weekday != weekday or row.service not in services
    )
    # Transient rows, never added to the session: the spans the save writes.
    new = tuple(
        OpeningHours(
            tenant_id=tenant_id,
            weekday=weekday,
            service=service,
            opens_at=span.opens,
            closes_at=span.closes,
        )
        for service in services
        for span in after[service]
    )
    was, will = HoursData(regular, special), HoursData(kept + new, special)
    tz = ZoneInfo(zone)
    faults: dict[str, str] = {}

    def running(night: date, service: str) -> bool:
        return any(
            end > now
            for data in (was, will)
            for _start, end in windows_for_day(data, night, service, tz)
        )

    for service in services:
        for night in nights:
            if running(night, service) and night_deepened(
                was, will, night, service, tz
            ):
                faults[service] = NIGHT_OVERLAP.format(
                    night=_dated(night), next=_dated(night + one)
                )
                break
    return faults


def _dated(day: date) -> str:
    return f"{WEEKDAYS[day.weekday()]}, {day:%d.%m.}"


def _texts(day: Day) -> dict[str, list[str]]:
    """Each service's spans to the minute, in the order of the texts: stored
    rows sort by their seconds too, typed ones cannot."""
    return {
        service: sorted(span.text() for span in day[service]) for service in SERVICES
    }
