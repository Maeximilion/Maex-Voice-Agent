"""Staff changes and their check cards (#570 part b, D34 point 4, docs/23 §6).

The staff, signed in without the owner's PIN, change the team number and the
hours of a given date. Both are temporary and reviewed:

- A team number set by the staff holds to the end of the business day
  (`service_config.team_phone_today`), then the standard holds again, unless
  the owner keeps it. Read only through `team_phone_at`.
- A date's hours hold at once for today, and for a later date when they close
  it or shorten it. Longer or extra hours for a later date wait for the owner.
  Asking for what held before the staff touched the date holds at once too: it
  takes a staff change back. Times on a date "like" another weekday wait for
  the owner, today's too and after a held closure of that date (#710).

Every change shows a check card (`staff_changes`, one open card per kind and
day) until the owner keeps it or undoes it with the admin PIN. A second change
of the same thing moves the card's `after` and keeps the `before` of the first,
so undo always returns to what held before the staff came. A card for a day
that has passed is no longer shown: its change ended with that day.

Every staff change also pushes to the owner and to the operator's `ops`
topic (T12): an outbox event in the same transaction, sent by n8n
(`n8n/team_events.json`). A tap that changes nothing pushes nothing, nor does
the owner's own answer on a card.

The operator may set today's team number the same way, over the ops API
(`domain/operator.py`): the card and the push then say it was the operator,
not the team (#811). Only the operator sets the standard number directly
(`set_standard_team_phone`), with a push and without a card; the owner makes a
number the standard by keeping a card.

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
from api.domain.status.holidays import holidays as statutory_holidays
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
# Whose change a card and a push name (#811): the operator's actors begin so
# (`core/auth.py`, `jobs/gui_access.py`), every other change is the team's.
OPERATOR_ACTOR = "operator:"
BY_OPERATOR, BY_TEAM = "operator", "team"
ACTION_TEAM_PHONE = "staff_change.team_phone"
ACTION_STANDARD = "service_config.team_phone_standard"
DAY_HOURS = "day_hours"
OPEN, KEPT, UNDONE = "open", "kept", "undone"
SET = "set"
DISMISSED = "dismissed"
NO_SPAN = "Bitte Beginn und Ende angeben, das Ende nicht gleich dem Beginn."
OVERNIGHT = (
    "Das Ende liegt vor dem Beginn. Geht der Tag über Mitternacht, "
    "bitte „Ende nach Mitternacht“ ankreuzen."
)
PAST_DAY = "Ein vergangener Tag lässt sich nicht mehr ändern."
CARD_OPEN = (
    "Für diesen Tag wartet eine Änderung des Teams auf der Übersicht. "
    "Bitte erst dort übernehmen oder zurücknehmen."
)
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
    """The staff, or the operator, set the team number for today. None when
    it is the number that holds already. Raises InvalidInput. Commits."""
    number = _dialable(raw)
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
    _audit(session, tenant_id, actor, ACTION_TEAM_PHONE, card, holding, number)
    enqueue(
        session,
        tenant_id=tenant_id,
        event_type=TEAM_PHONE_CHANGED,
        payload={
            "day": today.isoformat(),
            "from": holding,
            "to": number,
            "by": _by(actor),
        },
    )
    card.after, card.held = {"number": number}, True
    if card.after == card.before:
        _close(card, UNDONE, now)
    session.commit()
    return card


def set_standard_team_phone(
    session: Session, tenant_id: uuid.UUID, raw: str, now: datetime, actor: str
) -> bool:
    """The operator sets the standard team number (#811 question 2), checked
    as the number for today. A number set for today still holds to the end of
    its day, and its card stays: the staff chose it for today. No card of its
    own: the owner gets the push. False when it is the standard already.
    Raises InvalidInput. Commits."""
    number = _dialable(raw)
    config, zone = _locked(session, tenant_id)
    before = config.team_phone
    if number == before:
        session.commit()
        return False
    config.team_phone = number
    session.add(
        AuditLog(
            tenant_id=tenant_id,
            actor=actor,
            action=ACTION_STANDARD,
            entity="service_config",
            entity_id=tenant_id,
            payload={"from": before, "to": number},
        )
    )
    enqueue(
        session,
        tenant_id=tenant_id,
        event_type=TEAM_PHONE_CHANGED,
        payload={
            "day": clock.business_day(now, zone).isoformat(),
            "from": before,
            "to": number,
            "standard": True,
            "by": _by(actor),
        },
    )
    session.commit()
    return True


def changed_by(session: Session, cards: list[StaffChange]) -> dict[uuid.UUID, str]:
    """Who made the last change of each team number card, the team or the
    operator (#811 question 1), read from the cards' audit rows: the card
    itself keeps only what changed."""
    ids = [card.id for card in cards if card.kind == TEAM_PHONE]
    if not ids:
        return {}
    rows = session.execute(
        select(AuditLog.entity_id, AuditLog.actor)
        .where(AuditLog.action == ACTION_TEAM_PHONE, AuditLog.entity_id.in_(ids))
        .order_by(AuditLog.id)
    ).all()
    # A card holds one day's changes, a handful of rows: the last one wins.
    return {card_id: _by(actor) for card_id, actor in rows if card_id is not None}


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
    would drop that weekday and its seats (#710), also after a held closure
    of that date. Asking for what holds now withdraws a waiting request. None
    when nothing changes. An end before the start runs past midnight and is
    refused unless the staff say so (`overnight`): swapped times would open
    the night. Raises InvalidInput. Commits."""
    hours = _checked(hours, overnight)
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
    # The weekday the card started from counts too: after a held closure the
    # row reads closed, and times then would still drop "wie Sonntag", also
    # on a rest day whose closed row they fit in.
    drops = _drops_weekday(hours, holding, day) or _drops_weekday(hours, origin, day)
    held = hours in (holding, origin) or (
        not drops
        and (day == today or _within(session, tenant_id, day, hours, holding, zone))
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


def set_day_hours(
    session: Session,
    tenant_id: uuid.UUID,
    day: date,
    hours: DayHours,
    now: datetime | None = None,
    actor: str = ACTOR_OWNER,
    overnight: bool = False,
) -> bool:
    """The owner sets a date's hours behind the admin PIN (#571): they hold at
    once, also what a staff request would wait for, with an audit row and
    without a check card or a push, which tell the owner what the staff did.
    A date whose staff change is still open is decided on its card first
    (Conflict): a second answer here would leave the card saying what no
    longer holds. False when the date holds these hours already. The rules
    for a span are the staff's (`change_day_hours`). Raises InvalidInput.
    Commits."""
    hours = _checked(hours, overnight)
    now = now or clock.utcnow()
    _config, zone = _locked(session, tenant_id)
    if day < clock.business_day(now, zone):
        session.rollback()
        raise InvalidInput("a past day", say=PAST_DAY)
    if _open_card(session, tenant_id, DAY_HOURS, day) is not None:
        session.rollback()
        raise Conflict(f"an open staff change for {day}", say=CARD_OPEN)
    row = _set_row(session, tenant_id, day, lock=True)
    holding = DayHours.of_row(row)
    if hours.note is None:
        hours = DayHours(hours.closed, hours.opens, hours.closes, None, holding.note)
    if hours == holding:
        session.commit()
        return False
    _write(session, tenant_id, day, hours, row)
    session.add(
        AuditLog(
            tenant_id=tenant_id,
            actor=actor,
            action="special_day.set",
            entity="special_day",
            payload={
                "day": day.isoformat(),
                "from": holding.as_dict(),
                "to": hours.as_dict(),
            },
        )
    )
    session.commit()
    return True


def coming_days(
    session: Session, tenant_id: uuid.UUID, today: date
) -> list[tuple[date, DayHours]]:
    """The dates from today on that hold hours of their own, by date."""
    rows = session.scalars(
        select(SpecialDay)
        .where(
            SpecialDay.tenant_id == tenant_id,
            SpecialDay.status == SET,
            SpecialDay.date >= today,
        )
        .order_by(SpecialDay.date)
    )
    return [(row.date, DayHours.of_row(row)) for row in rows]


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
    after = load_hours(session, tenant_id, day, days=1)

    def dinein(held: DayHours) -> list[tuple[datetime, datetime]]:
        special = {} if held.normal else {day: held.row(tenant_id, day)}
        return windows_for_day(HoursData(after.regular, special), day, "dinein", tz)

    # The date's span: its business day plus the windows that held before the
    # change, which may start before or end after the cutoff (02:00 to 04:00,
    # 22:00 to 10:00). The query bounds come from this span, never a constant.
    span = [business_day_bounds_utc(day, zone), *dinein(before or DayHours())]
    # After the change: the date with its new hours, its neighbours as set. A
    # table counts as kept when any window still holds it, also one of the day
    # before or after (open_window_at checks the previous day too).
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
            # The team never calls a self-test's guest (#574).
            Reservation.is_test.is_(False),
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


def _checked(hours: DayHours, overnight: bool) -> DayHours:
    """A date's hours as they may be written: closed alone, or a span with a
    length whose end before the start has the word for it (#596)."""
    if hours.closed:
        return DayHours(closed=True, note=hours.note)
    if (hours.opens is None) != (hours.closes is None) or (
        hours.opens is not None and hours.opens == hours.closes
    ):
        raise InvalidInput("hours without a span", say=NO_SPAN)
    if hours.opens and hours.closes and hours.closes < hours.opens and not overnight:
        raise InvalidInput("hours past midnight not confirmed", say=OVERNIGHT)
    return hours


def _dialable(raw: str) -> str:
    """A team number as the staff and the operator may set it: in E.164 and one
    the dialplan dials (`sheet_numbers`). Raises InvalidInput."""
    try:
        number = normalize_phone(raw)
    except InvalidInput:
        raise InvalidInput("team number refused", say=NO_NUMBER) from None
    if number not in sheet_numbers():
        raise InvalidInput("team number not dialable", say=NOT_LISTED)
    return number


def _by(actor: str) -> str:
    return BY_OPERATOR if actor.startswith(OPERATOR_ACTOR) else BY_TEAM


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
    is the date's locked row when the caller has it already. A holiday (a row
    with its name, or a date that is a statutory holiday in the restaurant's
    state) returned to the weekly hours stays as the answer "open as always"
    (`dismissed`, T-9.5): deleted, the holiday job would suggest it again and
    one tap on the board would undo what was decided here. For the same reason
    a suggestion still waiting beside the row is answered with it (review of
    #820): left open, "closed" tapped on its card would close the date again."""
    row = row or _set_row(session, tenant_id, day, lock=True)
    if hours.normal:
        if row is not None:
            # The date's suggestion or dismissal, one at most
            # (`uq_special_days_suggestion`).
            other = session.scalars(
                select(SpecialDay)
                .where(
                    SpecialDay.tenant_id == tenant_id,
                    SpecialDay.date == day,
                    SpecialDay.status != SET,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            ).first()
            if other is not None:
                other.status = DISMISSED
                session.delete(row)
            elif name := row.note or _holiday_name(session, tenant_id, day):
                row.status, row.note = DISMISSED, name
                row.closed, row.opens_at, row.closes_at = False, None, None
            else:
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


def _holiday_name(session: Session, tenant_id: uuid.UUID, day: date) -> str | None:
    """The statutory holiday `day` is in the restaurant's state, if it is one
    and the state is named: the dates the holiday job suggests."""
    region = session.scalar(
        select(ServiceConfig.holiday_region).where(ServiceConfig.tenant_id == tenant_id)
    )
    return dict(statutory_holidays(region, day.year)).get(day) if region else None


def _drops_weekday(after: DayHours, before: DayHours, day: date) -> bool:
    """Times on a date "like" another weekday cannot keep that weekday (one
    meaning per row, `ck_special_days_like_weekday_alone`): its seats, call
    mode and call limit go to the plain weekday's (`hours.weekday_of`).
    Closing drops nothing, nor does "wie Sonntag" on a Sunday."""

    def weekday(hours: DayHours) -> int:
        return day.weekday() if hours.like_weekday is None else hours.like_weekday

    return not after.closed and weekday(after) != weekday(before)


def _within(
    session: Session,
    tenant_id: uuid.UUID,
    day: date,
    after: DayHours,
    before: DayHours,
    zone: str,
) -> bool:
    """Whether every service is open at most when it was before: closed, or
    shorter hours. Then the change holds at once (D34 point 4). The caller
    checks first that no "wie <Wochentag>" is dropped (#596)."""
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
