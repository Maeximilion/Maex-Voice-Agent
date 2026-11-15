"""Probes, area D (#754 staff part, #757). Red = finding confirmed."""

from datetime import datetime, time

from sqlalchemy import select

from api.domain.status import staff_changes as staff
from api.domain.status.staff_changes import DayHours
from api.models import SpecialDay
from api.tests.test_domain_staff_changes import (  # noqa: F401
    BERLIN,
    NOW,
    SATURDAY,
    TUESDAY,
    _table,
    dialable,
    session,
    tenant_id,
)


def test_754_close_then_reopen_today_keeps_like_weekday(session, tenant_id):  # noqa: F811
    """#710 fix: times today on a 'wie Sonntag' date wait for the owner. Closing
    first (held, closing drops nothing) and then sending times compares with
    the closed row, not with the card's origin: the times hold at once and
    Sunday's seats, call mode and call limit are gone without the owner."""
    session.add(
        SpecialDay(tenant_id=tenant_id, date=TUESDAY, like_weekday=6, status="set")
    )
    session.commit()
    closed = staff.change_day_hours(
        session, tenant_id, TUESDAY, DayHours(closed=True), NOW
    )
    assert closed is not None and closed.held
    card = staff.change_day_hours(
        session, tenant_id, TUESDAY, DayHours(opens=time(12), closes=time(13)), NOW
    )
    row = session.scalars(select(SpecialDay).where(SpecialDay.date == TUESDAY)).one()
    session.refresh(row)
    # Expected as in test_shorter_times_today_on_a_day_like_another_weekday_wait_too:
    assert card is not None and not card.held, "times held at once"
    assert row.like_weekday is None and row.closed  # still closed, waiting


def test_757_today_closure_lists_tables_already_past(session, tenant_id):  # noqa: F811
    """Staff close today at 15:00 (kitchen down): the 12:00 lunch table has
    eaten and gone, still 'confirmed' (no other status exists), and is listed
    under 'Diese Gaeste bitte selbst anrufen'."""
    _table(session, tenant_id, TUESDAY, time(12), "LunchDone")
    _table(session, tenant_id, TUESDAY, time(19), "Evening")
    at_three = datetime.combine(TUESDAY, time(15), tzinfo=BERLIN)
    card = staff.change_day_hours(
        session, tenant_id, TUESDAY, DayHours(closed=True), at_three
    )
    assert card is not None and card.held
    found = staff.bookings_outside_hours(
        session,
        tenant_id,
        TUESDAY,
        DayHours(closed=True),
        "Europe/Berlin",
        DayHours.of(card.before),
    )
    assert [b.guest_name for b in found] == ["Evening"]


def test_757_shortening_lists_an_override_table_it_never_touched(session, tenant_id):  # noqa: F811
    """A special day 17:00-22:00; the team booked a 15:30 table on its own
    word (tablet override, manual.py). Staff shorten to 17:00-21:00: the
    15:30 table was outside the hours before and after, the change does not
    touch it, but it is listed with the 21:30 table."""
    before = DayHours(opens=time(17), closes=time(22))
    session.add(
        SpecialDay(
            tenant_id=tenant_id,
            date=SATURDAY,
            opens_at=time(17),
            closes_at=time(22),
            status="set",
        )
    )
    session.commit()
    _table(session, tenant_id, SATURDAY, time(15, 30), "Override")
    _table(session, tenant_id, SATURDAY, time(21, 30), "Late")
    found = staff.bookings_outside_hours(
        session,
        tenant_id,
        SATURDAY,
        DayHours(opens=time(17), closes=time(21)),
        "Europe/Berlin",
        before,
    )
    assert [b.guest_name for b in found] == ["Late"]
