"""Staff changes and their check cards (#570 part b, D34 point 4, docs/23 §6).

The staff, signed in without the owner's PIN, change the team number and the
hours of a given date. Both are temporary and reviewed:

- A team number set by the staff holds to the end of the business day
  (`service_config.team_phone_today`), then the standard holds again, unless
  the owner keeps it. Read only through `team_phone_at`.
- A date's hours hold at once for today, and for a later date when they close
  it or shorten it. Longer or extra hours for a later date wait for the owner.
  Asking for what held before the staff touched the date holds at once too: it
  takes a staff change back.

Every change shows a check card (`staff_changes`, one open card per kind and
day) until the owner keeps it or undoes it with the admin PIN. A second change
of the same thing moves the card's `after` and keeps the `before` of the first,
so undo always returns to what held before the staff came. A card for a day
that has passed is no longer shown: its change ended with that day.

Every staff change also pushes to the owner and to the operator's `ops`
topic (T12): an outbox event in the same transaction, sent by n8n
(`n8n/team_events.json`). A tap that changes nothing pushes nothing, nor does
the owner's own answer on a card.

Holiday answers (T-9.5) stay as they are (Maxi, 09.11.2026): the system
suggested the date, the staff only confirm it.
"""

import hashlib
import uuid
from dataclasses import asdict, dataclass
from datetime import date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import Text, cast, func, literal, select
from sqlalchemy.dialects.postgresql import aggregate_order_by
from sqlalchemy.orm import Session

from api.config import settings
from api.core import time as clock
from api.core.errors import Conflict, InvalidInput, NotFound
from api.core.time import business_day_bounds_utc
from api.domain.customers.phone import E164, costly, normalize_phone
from api.domain.reservations.spoken import WEEKDAYS
from api.domain.status.hours import (
    HoursData,
    Window,
    load_hours,
    open_window_at,
    regular_hours,
    windows_for_day,
)
from api.events import enqueue
from api.events.types import DAY_HOURS_CHANGED, TEAM_PHONE_CHANGED
from api.models import (
    AuditLog,
    Reservation,
    ServiceConfig,
    SpecialDay,
    StaffChange,
    Tenant,
)
from api.models.tenants import SERVICES

ACTOR_TABLET = "gui:tablet"
ACTOR_OWNER = "gui:owner"
TEAM_PHONE = "team_phone"
DAY_HOURS = "day_hours"
OPEN, KEPT, UNDONE = "open", "kept", "undone"
SET = "set"
NO_SPAN = "Bitte Beginn und Ende angeben, das Ende nicht gleich dem Beginn."
OVERNIGHT = (
    "Das Ende liegt vor dem Beginn. Geht der Tag über Mitternacht, "
    "bitte „Ende nach Mitternacht“ ankreuzen."
)
PAST_DAY = "Ein vergangener Tag lässt sich nicht mehr ändern."
NO_NUMBER = "Das ist keine Telefonnummer."
NOT_LISTED = "Diese Nummer kann die Telefonanlage nicht anrufen. Weitere Nummern gibt der Betreiber frei."


@dataclass(frozen=True)
class BookingToCall:
    """A confirmed table a held closure or shortening leaves without hours."""

    guest_name: str
    at: datetime
    phone: str
    party_size: int

    @property
    def costly(self) -> bool:
        return costly(self.phone)


@dataclass(frozen=True)
class DayHours:
    """What a date holds: the weekly hours (all empty), closed, hours of its
    own, or the hours of another weekday. `note` is a holiday's name, kept."""

    closed: bool = False
    opens: time | None = None
    closes: time | None = None
    like_weekday: int | None = None
    note: str | None = None

    @property
    def normal(self) -> bool:
        return not self.closed and self.opens is None and self.like_weekday is None

    def text(self) -> str:
        """As the tablet and the push say it."""
        if self.closed:
            return "geschlossen"
        if self.opens is not None and self.closes is not None:
            return f"{self.opens:%H:%M} bis {self.closes:%H:%M} Uhr"
        if self.like_weekday is not None:
            return f"wie {WEEKDAYS[self.like_weekday]}"
        return "normale Zeiten der Woche"

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for key in ("opens", "closes"):
            data[key] = data[key].strftime("%H:%M") if data[key] else None
        return data

    @classmethod
    def of(cls, data: dict[str, Any]) -> "DayHours":
        def at(text: str | None) -> time | None:
            return time.fromisoformat(text) if text else None

        return cls(
            closed=data["closed"],
            opens=at(data["opens"]),
            closes=at(data["closes"]),
            like_weekday=data["like_weekday"],
            note=data["note"],
        )

    @classmethod
    def of_row(cls, row: SpecialDay | None) -> "DayHours":
        if row is None:
            return cls()
        return cls(row.closed, row.opens_at, row.closes_at, row.like_weekday, row.note)

    def row(self, tenant_id: uuid.UUID, day: date) -> SpecialDay:
        """A transient row, to read the windows it would give."""
        return SpecialDay(
            tenant_id=tenant_id,
            date=day,
            closed=self.closed,
            opens_at=self.opens,
            closes_at=self.closes,
            like_weekday=self.like_weekday,
            note=self.note,
            status=SET,
        )


def dialable_numbers() -> list[str]:
    """The numbers the dialplan transfers to: TEAM_PHONE and TRANSFER_TARGETS,
    compared exactly as written there (docs/20 §5, T-10.13). A team number
    elsewhere would never ring, so the staff choose from these only."""
    listed = [settings.team_phone, *settings.transfer_targets.split(",")]
    return list(dict.fromkeys(n.strip() for n in listed if n.strip()))


def sheet_numbers() -> list[str]:
    """The dialable numbers the sheet offers: those written in E.164. The
    dialplan compares exactly as written and a typed number is normalised, so
    a listed national number (`0721...`) could be offered and never accepted
    (#596). The dialplan itself still dials it, so `dialable_numbers` keeps it
    for the tenant's own number (`provision`)."""
    return [n for n in dialable_numbers() if E164.match(n)]


def team_phone_at(config: ServiceConfig, now: datetime) -> str:
    """The number a call goes to at `now`: today's while it holds, else the
    standard. The one reader of the team number."""
    until = config.team_phone_today_until
    if config.team_phone_today and until is not None and now < until:
        return config.team_phone_today
    return config.team_phone


def change_team_phone(
    session: Session,
    tenant_id: uuid.UUID,
    raw: str,
    now: datetime | None = None,
    actor: str = ACTOR_TABLET,
) -> StaffChange | None:
    """The staff set the team number for today. None when it is the number
    that holds already. Raises InvalidInput. Commits."""
    try:
        number = normalize_phone(raw)
    except InvalidInput:
        raise InvalidInput("team number refused", say=NO_NUMBER) from None
    if number not in sheet_numbers():
        raise InvalidInput("team number not dialable", say=NOT_LISTED)
    now = now or clock.utcnow()
    config, zone = _locked(session, tenant_id)
    holding = team_phone_at(config, now)
    if number == holding:
        session.commit()
        return None
    today = clock.business_day(now, zone)
    if number == config.team_phone:
        config.team_phone_today = config.team_phone_today_until = None
    else:
        config.team_phone_today = number
        config.team_phone_today_until = clock.business_day_bounds_utc(today, zone)[1]
    card = _open_card(session, tenant_id, TEAM_PHONE, today) or _new_card(
        session, tenant_id, TEAM_PHONE, today, {"number": holding}
    )
    _audit(session, tenant_id, actor, "staff_change.team_phone", card, holding, number)
    enqueue(
        session,
        tenant_id=tenant_id,
        event_type=TEAM_PHONE_CHANGED,
        payload={"day": today.isoformat(), "from": holding, "to": number},
    )
    card.after, card.held = {"number": number}, True
    if card.after == card.before:
        _close(card, UNDONE, now)
    session.commit()
    return card


def day_hours(session: Session, tenant_id: uuid.UUID, day: date) -> DayHours:
    """What the date holds now, for the sheet."""
    return DayHours.of_row(_set_row(session, tenant_id, day, lock=False))


def days_hours(
    session: Session, tenant_id: uuid.UUID, days: list[date]
) -> dict[date, DayHours]:
    """What each date holds now, in one read: for the check cards."""
    rows = session.scalars(
        select(SpecialDay).where(
            SpecialDay.tenant_id == tenant_id,
            SpecialDay.date.in_(days),
            SpecialDay.status == SET,
        )
    )
    held = {row.date: DayHours.of_row(row) for row in rows}
    return {day: held.get(day, DayHours()) for day in days}


def change_day_hours(
    session: Session,
    tenant_id: uuid.UUID,
    day: date,
    hours: DayHours,
    now: datetime | None = None,
    actor: str = ACTOR_TABLET,
    overnight: bool = False,
) -> StaffChange | None:
    """The staff change a date's hours. Held at once for today, when shorter,
    or when back to what held before the staff came; otherwise the card waits
    for the owner, today's too when times on a date "like" another weekday
    would drop that weekday and its seats (#710). Asking for what holds now withdraws a waiting request. None
    when nothing changes. An end before the start runs past midnight and is
    refused unless the staff say so (`overnight`): swapped times would open
    the night. Raises InvalidInput. Commits."""
    if hours.closed:
        hours = DayHours(closed=True, note=hours.note)
    elif (hours.opens is None) != (hours.closes is None) or (
        hours.opens is not None and hours.opens == hours.closes
    ):
        raise InvalidInput("hours without a span", say=NO_SPAN)
    elif hours.opens and hours.closes and hours.closes < hours.opens and not overnight:
        raise InvalidInput("hours past midnight not confirmed", say=OVERNIGHT)
    now = now or clock.utcnow()
    _config, zone = _locked(session, tenant_id)
    today = clock.business_day(now, zone)
    if day < today:
        session.rollback()
        raise InvalidInput("a past day", say=PAST_DAY)
    row = _set_row(session, tenant_id, day, lock=True)
    holding = DayHours.of_row(row)
    if hours.note is None:
        hours = DayHours(hours.closed, hours.opens, hours.closes, None, holding.note)
    card = _open_card(session, tenant_id, DAY_HOURS, day)
    if hours == holding and (card is None or card.after == hours.as_dict()):
        session.commit()
        return None
    if card is None:
        card = _new_card(session, tenant_id, DAY_HOURS, day, holding.as_dict())
    # What the card showed until now: what held, or the request that waited.
    shown = DayHours.of(card.after)
    origin = DayHours.of(card.before)
    held = (
        hours in (holding, origin)
        or (day == today and not _drops_weekday(hours, holding))
        or _within(session, tenant_id, day, hours, holding, zone)
    )
    if hours == shown and held == card.held:
        # The request that waits already, sent again: nothing changes.
        session.commit()
        return None
    if held and hours != holding:
        _write(session, tenant_id, day, hours, row)
    _audit(
        session,
        tenant_id,
        actor,
        "staff_change.day_hours",
        card,
        holding.as_dict(),
        hours.as_dict(),
        held=held,
    )
    card.after, card.held = hours.as_dict(), held
    enqueue(
        session,
        tenant_id=tenant_id,
        event_type=DAY_HOURS_CHANGED,
        payload={
            "day": day.isoformat(),
            "from": shown.text(),
            "to": hours.text(),
            "held": held,
        },
    )
    if held and hours == origin:
        _close(card, UNDONE, now)
    session.commit()
    return card


def decide(
    session: Session,
    tenant_id: uuid.UUID,
    card_id: uuid.UUID,
    keep: bool,
    now: datetime | None = None,
    actor: str = ACTOR_OWNER,
    seen: datetime | None = None,
) -> None:
    """The owner's answer on a card, behind the admin PIN. Keep: a team
    number becomes the standard, waiting hours hold. Undo: back to what held
    before the staff came. A card decided already, on another tablet, is no
    error, and neither is one of a day that has passed (a stale tablet): its
    change ended with that day, and today's must stay as it is. `seen` is the
    card's `updated_at` the owner's tablet showed: a card the staff changed
    since (a second tablet) raises Conflict, so no keep applies a number or
    hours the owner never saw (#596). Commits."""
    now = now or clock.utcnow()
    config, zone = _locked(session, tenant_id)
    card = session.scalars(
        select(StaffChange)
        .where(StaffChange.id == card_id, StaffChange.tenant_id == tenant_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()
    if card is None:
        session.rollback()
        raise NotFound(f"Staff change {card_id} not found")
    if card.status != OPEN or card.day < clock.business_day(now, zone):
        session.commit()
        return
    if seen is not None and seen != card.updated_at:
        session.rollback()
        raise Conflict(f"Staff change {card_id} changed since it was shown")
    if card.kind == TEAM_PHONE:
        if keep:
            config.team_phone = card.after["number"]
        config.team_phone_today = config.team_phone_today_until = None
    elif keep:
        if not card.held:
            _write(session, tenant_id, card.day, DayHours.of(card.after))
    else:
        _write(session, tenant_id, card.day, DayHours.of(card.before))
    _close(card, KEPT if keep else UNDONE, now)
    session.add(
        AuditLog(
            tenant_id=tenant_id,
            actor=actor,
            action=f"staff_change.{card.status}",
            entity="staff_change",
            entity_id=card.id,
            payload={"kind": card.kind, "day": card.day.isoformat()},
        )
    )
    session.commit()


def open_cards(
    session: Session, tenant_id: uuid.UUID, today: date
) -> list[StaffChange]:
    """The cards the owner has not decided, from today on, by date."""
    return list(
        session.scalars(
            select(StaffChange)
            .where(
                StaffChange.tenant_id == tenant_id,
                StaffChange.status == OPEN,
                StaffChange.day >= today,
            )
            .order_by(StaffChange.day, StaffChange.created_at)
        )
    )


def bookings_outside_hours(
    session: Session,
    tenant_id: uuid.UUID,
    day: date,
    hours: DayHours,
    zone: str,
    before: DayHours | None = None,
) -> list[BookingToCall]:
    """The confirmed tables of the business day that `hours` would leave
    without a dine-in window: all of them when closed, the ones outside the
    times when shortened. The team calls these guests itself (#710, Maxi
    2026-11-13): nothing is cancelled and no guest is messaged. By time."""
    tz = ZoneInfo(zone)
    regular = regular_hours(session, tenant_id)

    def dinein(held: DayHours) -> list[tuple[datetime, datetime]]:
        special = {} if held.normal else {day: held.row(tenant_id, day)}
        return windows_for_day(HoursData(regular, special), day, "dinein", tz)

    # The date's span: its business day plus the windows that held before the
    # change, which may start before or end after the cutoff (02:00 to 04:00,
    # 22:00 to 10:00). The query bounds come from this span, never a constant.
    span = [business_day_bounds_utc(day, zone), *dinein(before or DayHours())]
    # After the change: the date with its new hours, its neighbours as set. A
    # table counts as kept when any window still holds it, also one of the day
    # before or after (open_window_at checks the previous day too).
    after = load_hours(session, tenant_id, day, days=1)
    if hours.normal:
        after.special.pop(day, None)
    else:
        after.special[day] = hours.row(tenant_id, day)
    rows = session.scalars(
        select(Reservation)
        .where(
            Reservation.tenant_id == tenant_id,
            Reservation.status == "confirmed",
            Reservation.deleted_at.is_(None),
            Reservation.reserved_for >= min(a for a, _ in span),
            Reservation.reserved_for < max(b for _, b in span),
        )
        .order_by(Reservation.reserved_for)
    )
    return [
        BookingToCall(
            r.guest_name, r.reserved_for.astimezone(tz), r.phone, r.party_size
        )
        for r in rows
        if any(a <= r.reserved_for < b for a, b in span)
        and open_window_at(after, r.reserved_for, "dinein", tz) is None
    ]


def cards_change_token(session: Session, tenant_id: uuid.UUID, today: date) -> str:
    """A fingerprint of the open cards for the event stream (gui/sse.py),
    hashed in the database: no card rows with their JSONB per tablet tick."""
    each = func.concat(
        cast(StaffChange.id, Text), ":", cast(StaffChange.updated_at, Text)
    )
    digest = session.scalar(
        select(
            func.md5(
                func.coalesce(
                    func.string_agg(each, aggregate_order_by(literal(","), each)), ""
                )
            )
        ).where(
            StaffChange.tenant_id == tenant_id,
            StaffChange.status == OPEN,
            StaffChange.day >= today,
        )
    )
    if digest == hashlib.md5(b"").hexdigest():  # no open card, nothing to list
        return ""
    # The guest lists on the cards hang on the confirmed tables (#710).
    tables = session.execute(
        select(func.count(), func.max(Reservation.updated_at)).where(
            Reservation.tenant_id == tenant_id,
            Reservation.status == "confirmed",
            Reservation.deleted_at.is_(None),
            Reservation.reserved_for >= business_day_bounds_utc(today)[0],
        )
    ).one()
    return str(digest)[:16] + hashlib.md5(repr(tuple(tables)).encode()).hexdigest()[:8]


# --- helpers ---------------------------------------------------------------------


def _locked(session: Session, tenant_id: uuid.UUID) -> tuple[ServiceConfig, str]:
    """The restaurant's settings row, locked: every staff change and decision
    of one restaurant runs one after the other."""
    config = session.scalars(
        select(ServiceConfig)
        .where(ServiceConfig.tenant_id == tenant_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()
    tenant = session.get(Tenant, tenant_id)
    if config is None or tenant is None:
        session.rollback()
        raise NotFound(f"service_config missing for tenant {tenant_id}")
    return config, tenant.timezone


def _open_card(
    session: Session, tenant_id: uuid.UUID, kind: str, day: date
) -> StaffChange | None:
    return session.scalars(
        select(StaffChange)
        .where(
            StaffChange.tenant_id == tenant_id,
            StaffChange.kind == kind,
            StaffChange.day == day,
            StaffChange.status == OPEN,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()


def _new_card(
    session: Session, tenant_id: uuid.UUID, kind: str, day: date, before: dict[str, Any]
) -> StaffChange:
    card = StaffChange(
        tenant_id=tenant_id,
        kind=kind,
        day=day,
        before=before,
        after=before,
        held=True,
        status=OPEN,
    )
    session.add(card)
    session.flush()
    return card


def _close(card: StaffChange, status: str, now: datetime) -> None:
    card.status, card.decided_at = status, now


def _set_row(
    session: Session, tenant_id: uuid.UUID, day: date, lock: bool
) -> SpecialDay | None:
    query = select(SpecialDay).where(
        SpecialDay.tenant_id == tenant_id,
        SpecialDay.date == day,
        SpecialDay.status == SET,
    )
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    return session.scalars(query).first()


def _write(
    session: Session,
    tenant_id: uuid.UUID,
    day: date,
    hours: DayHours,
    row: SpecialDay | None = None,
) -> None:
    """Make the date hold `hours`: no counting row for the weekly hours. `row`
    is the date's locked row when the caller has it already."""
    row = row or _set_row(session, tenant_id, day, lock=True)
    if hours.normal:
        if row is not None:
            session.delete(row)
        session.flush()
        return
    if row is None:
        row = hours.row(tenant_id, day)
        session.add(row)
    else:
        row.closed, row.opens_at, row.closes_at = (
            hours.closed,
            hours.opens,
            hours.closes,
        )
        row.like_weekday, row.note = hours.like_weekday, hours.note
    session.flush()


def _drops_weekday(after: DayHours, before: DayHours) -> bool:
    """Times on a date "like" another weekday cannot keep that weekday (one
    meaning per row, `ck_special_days_like_weekday_alone`): its seats, call
    mode and call limit go to the plain weekday's. Closing drops nothing."""
    return (
        before.like_weekday is not None
        and not after.closed
        and after.like_weekday != before.like_weekday
    )


def _within(
    session: Session,
    tenant_id: uuid.UUID,
    day: date,
    after: DayHours,
    before: DayHours,
    zone: str,
) -> bool:
    """Whether every service is open at most when it was before: closed, or
    shorter hours. Then the change holds at once (D34 point 4). Times on a
    date "like" another weekday drop that weekday and with it its seats
    (`reservations/capacity.py`), so they wait for the owner (#596)."""
    if _drops_weekday(after, before):
        return False
    regular = regular_hours(session, tenant_id)
    tz = ZoneInfo(zone)

    def windows(hours: DayHours, service: str) -> list[Window]:
        special = {} if hours.normal else {day: hours.row(tenant_id, day)}
        return windows_for_day(HoursData(regular, special), day, service, tz)

    for service in SERVICES:
        old = _merged(windows(before, service))
        for start, end in windows(after, service):
            if not any(a <= start and end <= b for a, b in old):
                return False
    return True


def _merged(windows: list[Window]) -> list[Window]:
    merged: list[Window] = []
    for start, end in sorted(windows):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def _audit(
    session: Session,
    tenant_id: uuid.UUID,
    actor: str,
    action: str,
    card: StaffChange,
    before: object,
    after: object,
    **fact: object,
) -> None:
    session.add(
        AuditLog(
            tenant_id=tenant_id,
            actor=actor,
            action=action,
            entity="staff_change",
            entity_id=card.id,
            payload={"day": card.day.isoformat(), "from": before, "to": after, **fact},
        )
    )
