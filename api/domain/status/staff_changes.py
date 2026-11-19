"""Staff changes and their check cards (#570 part b, D34 point 4, docs/23 §6).

The staff, signed in without the owner's PIN, change the team number and the
hours of a given date. Both are temporary and reviewed:

- A team number set by the staff holds to the end of the business day
  (`service_config.team_phone_today`), then the standard holds again, unless
  the owner keeps it. Read only through `team_phone_at`. The business day
  ends at 05:00 (`core/time.py`), not at closing: calls still reach the team
  after closing, and a closed date has no closing (#861, docs/20 §5).
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
(`domain/operator.py`): the card then says it was the operator, not the team,
and the push names the operator's account (#811). Only the operator sets the standard number directly
(`set_standard_team_phone`), with a push and without a card; the owner makes a
number the standard by keeping a card.

Holiday answers (T-9.5) stay as they are (Maxi, 09.11.2026): the system
suggested the date, the staff only confirm it.
"""

import hashlib
import uuid
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import Text, cast, func, literal, select, update
from sqlalchemy.dialects.postgresql import aggregate_order_by
from sqlalchemy.orm import Session

from api.config import settings
from api.core import time as clock
from api.core.errors import Conflict, InvalidInput, NotFound
from api.core.time import business_day_bounds_utc
from api.domain.access.sessions import audit
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
    OpeningHours,
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
# A new standard moved the "before" of today's open card: its last change.
ACTION_BEFORE = "staff_change.team_phone_before"
DAY_HOURS = "day_hours"
OPEN, KEPT, UNDONE = "open", "kept", "undone"
SET = "set"
DISMISSED = "dismissed"
SUGGESTED = "suggested"
NO_SPAN = "Bitte Beginn und Ende angeben, das Ende nicht gleich dem Beginn."
OVERNIGHT = (
    "Das Ende liegt vor dem Beginn. Geht der Tag über Mitternacht, "
    "bitte „Ende nach Mitternacht“ ankreuzen."
)
# The same refusal for an end at 00:00, which reads as the end of the day and
# is the start of the next one (#819): the rule is unchanged, the reason named.
AT_MIDNIGHT = (
    "Ein Ende um 00:00 Uhr liegt schon am nächsten Tag. Bitte „Ende nach "
    "Mitternacht“ ankreuzen."
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


def sheet_numbers() -> list[str]:
    """The numbers a team number may be: TEAM_PHONE and TRANSFER_TARGETS,
    those the dialplan transfers to, compared exactly as written there
    (docs/20 §5, T-10.13); a team number elsewhere would never ring. Only
    those written in E.164: a typed number is normalised, so a listed
    national one (`0721...`) could be offered and never accepted (#596).
    TEAM_PHONE is E.164 by the start check (`config.py`, #861), so the
    standard number is always among them."""
    listed = [settings.team_phone, *settings.transfer_targets.split(",")]
    numbers = dict.fromkeys(n.strip() for n in listed if n.strip())
    return [n for n in numbers if E164.match(n)]


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
        # To the business day's end (05:00), not to closing (#861): transfers,
        # outage legs and callbacks ring the team after closing too, a closed
        # date has no closing, and the card is the business day's.
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
            **_who(actor),
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
    its day: the staff chose it for today. No card of its own: the owner gets
    the push. Today's open card now starts from the new standard, so undo
    returns to it, a tablet showing the old card is refused (`decide`), the
    card names the operator, and the push asks the owner to look at it; when
    today's number is the new standard, nothing is left to decide and the
    card closes as kept, with its row as `decide` writes it. False when it is
    the standard already. Raises InvalidInput. Commits."""
    number = _dialable(raw)
    config, zone = _locked(session, tenant_id)
    before = config.team_phone
    if number == before:
        session.commit()
        return False
    config.team_phone = number
    today = clock.business_day(now, zone)
    card = _open_card(session, tenant_id, TEAM_PHONE, today)
    holding: dict[str, str] = {}
    if card is not None:
        moved = card.before.get("number")
        card.before = {"number": number}
        if card.after == card.before:
            config.team_phone_today = config.team_phone_today_until = None
            # The staff's number is the standard now: kept, not undone.
            _close(card, KEPT, now)
            _audit(
                session,
                tenant_id,
                actor,
                f"staff_change.{KEPT}",
                card,
                moved,
                number,
                kind=card.kind,
            )
        else:
            _audit(session, tenant_id, actor, ACTION_BEFORE, card, moved, number)
            holding = {"today": card.after["number"]}
    audit(
        session,
        tenant_id,
        actor,
        ACTION_STANDARD,
        tenant_id,
        {"from": before, "to": number},
        entity="service_config",
    )
    enqueue(
        session,
        tenant_id=tenant_id,
        event_type=TEAM_PHONE_CHANGED,
        payload={
            "day": today.isoformat(),
            "from": before,
            "to": number,
            "standard": True,
            **holding,
            **_who(actor),
        },
    )
    session.commit()
    return True


def changed_by(
    session: Session, tenant_id: uuid.UUID, cards: list[StaffChange]
) -> dict[uuid.UUID, str]:
    """Who made the last change of each team number card, the team or the
    operator (#811 question 1), read from the cards' audit rows: the card
    itself keeps only what changed."""
    phones = [card for card in cards if card.kind == TEAM_PHONE]
    if not phones:
        return {}
    rows = session.execute(
        select(AuditLog.entity_id, AuditLog.actor)
        .where(
            # The tenant and the time first: `ix_audit_log_tenant_at`. Both
            # times are a transaction's start, and a change begun just before
            # the card's own transaction may still write to it: a margin.
            AuditLog.tenant_id == tenant_id,
            AuditLog.at >= min(card.created_at for card in phones) - timedelta(hours=1),
            AuditLog.action.in_((ACTION_TEAM_PHONE, ACTION_BEFORE)),
            AuditLog.entity_id.in_([card.id for card in phones]),
        )
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
    the night. New hours whose night reaches into the next date's, or that
    start inside the night before, are refused too (`_night_checked`).
    Raises InvalidInput. Commits."""
    hours = _checked(hours, overnight)
    now = now or clock.utcnow()
    _config, zone = _locked(session, tenant_id)
    today = clock.business_day(now, zone)
    if day < today:
        session.rollback()
        raise InvalidInput("a past day", say=PAST_DAY)
    row = _set_row(session, tenant_id, day, lock=True)
    holding = DayHours.of_row(row)
    card = _open_card(session, tenant_id, DAY_HOURS, day)
    origin = holding if card is None else DayHours.of(card.before)
    if hours.note is None:
        hours = DayHours(hours.closed, hours.opens, hours.closes, None, holding.note)
        if replace(hours, note=origin.note) == origin:
            # Back to the origin's hours is the origin, its holiday name too:
            # after held weekly hours the row holds no name to take (#802).
            hours = origin
    if hours == holding and (card is None or card.after == hours.as_dict()):
        session.commit()
        return None
    if card is None:
        card = _new_card(session, tenant_id, DAY_HOURS, day, holding.as_dict())
    # What the card showed until now: what held, or the request that waited.
    shown = DayHours.of(card.after)
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
    if hours != holding:
        _night_checked(session, tenant_id, day, hours, zone)
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


def holiday_answered(session: Session, tenant_id: uuid.UUID, row: SpecialDay) -> None:
    """The team's holiday answer set the date's row (`holidays.answer`) while
    a staff card for it waits: the answer is what held before the staff's
    request now. The card starts from it, so the owner's undo returns to it
    and later times are checked against its weekday's seats, call mode and
    call limit (#802); a request without a name takes the holiday's, as one
    sent now would. The caller holds the restaurant's lock. Flushes."""
    card = _open_card(session, tenant_id, DAY_HOURS, row.date)
    if card is None:
        return
    card.before = DayHours.of_row(row).as_dict()
    if card.after["note"] is None:
        card.after = {**card.after, "note": row.note}
    session.flush()


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
    _night_checked(session, tenant_id, day, hours, zone)
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
    asked = KEPT if keep else UNDONE
    if (
        seen is not None
        and card.status not in (OPEN, asked)
        and seen != card.updated_at
    ):
        # Closed since the tablet showed it, the other way (a new standard
        # kept it, review of #837): saying "done" would hide that nothing was.
        session.rollback()
        raise Conflict(f"Staff change {card_id} changed since it was shown")
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
    elif not keep or not card.held:
        row = _set_row(session, tenant_id, card.day, lock=True)
        hours = DayHours.of(card.after if keep else card.before)
        if hours != DayHours.of_row(row):
            # The hours move, and a date beside may have changed since (#862).
            # Read from the row: a card whose last request waits can sit over
            # an earlier change that held.
            _night_checked(session, tenant_id, card.day, hours, zone)
        _write(session, tenant_id, card.day, hours, row)
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
    now: datetime | None = None,
) -> list[BookingToCall]:
    """The confirmed tables of the business day that `hours` would leave
    without a dine-in window: all of them when closed, the ones outside the
    times when shortened. The team calls these guests itself (#710, Maxi
    2026-11-13): nothing is cancelled and no guest is messaged. By time.
    A table the team booked outside the hours on purpose (`off_hours`, #819)
    is listed only when a window held it before the change: one the hours
    never held is no news to its guest. Only tables from `now` on: a closure
    held at 15:00 has no call to make to the lunch guests (#862), as the
    board's own list (`weekly.tables_outside_hours`)."""
    now = now or clock.utcnow()
    tz = ZoneInfo(zone)
    # After the change: the date with its new hours, its neighbours as set. A
    # table counts as kept when any window still holds it, also one of the day
    # before or after (open_window_at checks the previous day too).
    after = _hours_with(session, tenant_id, day, hours)

    def dinein(held: DayHours) -> list[tuple[datetime, datetime]]:
        special = {} if held.normal else {day: held.row(tenant_id, day)}
        return windows_for_day(HoursData(after.regular, special), day, "dinein", tz)

    # The date's span: its business day plus the windows that held before the
    # change, which may start before or end after the cutoff (02:00 to 04:00,
    # 22:00 to 10:00). The query bounds come from this span, never a constant.
    held_before = dinein(before or DayHours())
    span = [business_day_bounds_utc(day, zone), *held_before]
    rows = session.scalars(
        select(Reservation)
        .where(
            Reservation.tenant_id == tenant_id,
            Reservation.status == "confirmed",
            Reservation.deleted_at.is_(None),
            Reservation.reserved_for >= max(now, min(a for a, _ in span)),
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
        and (not r.off_hours or any(a <= r.reserved_for < b for a, b in held_before))
    ]


def cards_change_token(session: Session, tenant_id: uuid.UUID, today: date) -> str:
    """A fingerprint of the open cards and the tables listed below them for
    the event stream (gui/sse.py), hashed in the database: no card rows with
    their JSONB per tablet tick."""
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
    # The guest lists on the cards and below them hang on the confirmed tables
    # (#710), also with no card open (#764), and on the hours as they stand:
    # the weekly ones and every date's (#819). The hours by their content: a
    # save replaces its rows within one transaction.
    tables = session.execute(
        select(func.count(), func.max(Reservation.updated_at)).where(
            Reservation.tenant_id == tenant_id,
            Reservation.status == "confirmed",
            Reservation.deleted_at.is_(None),
            Reservation.reserved_for >= business_day_bounds_utc(today)[0],
        )
    ).one()
    weekly = func.concat(
        OpeningHours.weekday,
        OpeningHours.service,
        cast(OpeningHours.opens_at, Text),
        cast(OpeningHours.closes_at, Text),
    )
    dated = func.concat(
        cast(SpecialDay.date, Text),
        cast(SpecialDay.closed, Text),
        cast(SpecialDay.opens_at, Text),
        cast(SpecialDay.closes_at, Text),
        SpecialDay.like_weekday,
    )
    hours = (
        session.scalar(
            select(_digest(weekly)).where(OpeningHours.tenant_id == tenant_id)
        ),
        session.scalar(
            select(_digest(dated)).where(
                SpecialDay.tenant_id == tenant_id, SpecialDay.status == SET
            )
        ),
    )
    behind = repr((tuple(tables), hours)).encode()
    return str(digest)[:16] + hashlib.md5(behind).hexdigest()[:8]


def _digest(each: Any) -> Any:
    """The md5 of every row's text in one order, computed in the database."""
    return func.md5(
        func.coalesce(func.string_agg(each, aggregate_order_by(literal(";"), each)), "")
    )


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
        raise InvalidInput(
            "hours past midnight not confirmed", say=past_midnight(hours.closes)
        )
    return hours


def _hours_with(
    session: Session, tenant_id: uuid.UUID, day: date, hours: DayHours
) -> HoursData:
    """The hours of `day` and the dates beside it, with `day` holding `hours`."""
    held = load_hours(session, tenant_id, day, days=1)
    if hours.normal:
        held.special.pop(day, None)
    else:
        held.special[day] = hours.row(tenant_id, day)
    return held


def _night_checked(
    session: Session, tenant_id: uuid.UUID, day: date, hours: DayHours, zone: str
) -> None:
    """A date's night ends before the next date's first span of a service
    starts, and the night before the date ends before its spans start: every
    reader takes the night first, so a night that reached into the next span
    would be the window found there and its end the closing time (#862). The
    weekly writer's rule (`weekly._night_faults`), with the dates around `day`
    as they hold. The caller holds the restaurant's lock. Raises InvalidInput."""
    # Imported here: weekly imports this module.
    from api.domain.status.weekly import Span, _night_faults

    tz = ZoneInfo(zone)
    held = _hours_with(session, tenant_id, day, hours)

    def spans(on: date) -> dict[str, list[Span]]:
        return {
            service: [
                Span(opens.time(), closes.time())
                for opens, closes in windows_for_day(held, on, service, tz)
            ]
            for service in SERVICES
        }

    around = {
        (day - timedelta(days=1)).weekday(): spans(day - timedelta(days=1)),
        (day + timedelta(days=1)).weekday(): spans(day + timedelta(days=1)),
    }
    own = spans(day)
    for service in SERVICES:
        for say in _night_faults(around, day.weekday(), service, own[service]).values():
            session.rollback()
            raise InvalidInput("a night reaches into the next span", say=say)


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


def _who(actor: str) -> dict[str, str]:
    """Who a push names: the team, or the operator with its account, as the
    operator's access pushes do (`domain/operator.py` `_tell_owner`)."""
    by = _by(actor)
    return {"by": by, "actor": actor} if by == BY_OPERATOR else {"by": by}


def past_midnight(closes: time) -> str:
    """Why an end before the start is refused without the owner's word."""
    return AT_MIDNIGHT if closes == time(0) else OVERNIGHT


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
    # Hours set by hand answer the date's holiday card too (#764): left open,
    # it says the weekly hours hold, and a tap on it is dropped beside this row.
    session.execute(
        update(SpecialDay)
        .where(
            SpecialDay.tenant_id == tenant_id,
            SpecialDay.date == day,
            SpecialDay.status == SUGGESTED,
        )
        .values(status=DISMISSED)
    )
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
