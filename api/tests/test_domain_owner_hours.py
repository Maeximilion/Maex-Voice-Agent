"""The owner's hours (#571, plan number 7): the weekly hours per weekday and
service, a date's hours set directly behind the admin PIN, and the confirmed
tables the hours no longer cover."""

import uuid
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, delete, func, select
from sqlalchemy.orm import Session

from api.core.errors import Conflict, FieldErrors, InvalidInput
from api.domain.reservations.spoken import WEEKDAYS
from api.domain.status import holidays, weekly
from api.domain.status import staff_changes as staff
from api.domain.status.hours import load_hours, windows_for_day
from api.domain.status.service import service_open_at
from api.domain.status.staff_changes import DayHours
from api.domain.status.weekly import Span
from api.models import (
    AuditLog,
    OpeningHours,
    OutboxEvent,
    Reservation,
    ServiceConfig,
    SpecialDay,
    StaffChange,
)
from scripts.seed import seed

BERLIN = ZoneInfo("Europe/Berlin")
# Seed: Monday closed, every other day 11:30-14:00 and 17:00-22:00.
MONDAY, TUESDAY, WEDNESDAY, SATURDAY, SUNDAY = 0, 1, 2, 5, 6
TODAY = date(2026, 11, 10)  # a Tuesday
NOW = datetime.combine(TODAY, time(10, 0), tzinfo=BERLIN)
SEEDED = [Span(time(11, 30), time(14)), Span(time(17), time(22))]


@pytest.fixture
def session(migrated_db_url):
    engine = create_engine(migrated_db_url)
    with Session(engine) as s:
        yield s
    engine.dispose()


@pytest.fixture
def tenant_id(session) -> uuid.UUID:
    return uuid.UUID(
        seed(session, tenant_name="Testbetrieb", timezone="Europe/Berlin").tenant_id
    )


def _seen(session, tenant_id, weekday) -> str:
    return weekly.token(weekly.week(session, tenant_id)[weekday])


def _set(session, tenant_id, weekday, spans, seen=None) -> bool:
    seen = _seen(session, tenant_id, weekday) if seen is None else seen
    # A fixed clock: which dates the night rule reads depends on it (#877).
    return weekly.set_weekday(session, tenant_id, weekday, spans, seen, now=NOW)


def _windows(session, tenant_id, day, service="pickup") -> list[tuple[str, str]]:
    data = load_hours(session, tenant_id, day, days=0)
    return [
        (a.strftime("%H:%M"), b.strftime("%H:%M"))
        for a, b in windows_for_day(data, day, service, BERLIN)
    ]


def _audits(session, action) -> list[AuditLog]:
    return list(
        session.scalars(
            select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.id)
        )
    )


def _open(session, tenant_id, at: datetime, service="pickup") -> bool:
    return service_open_at(session, tenant_id, at, BERLIN, service)


# --- the weekly hours ----------------------------------------------------------


def test_the_week_reads_every_weekday_and_service_from_the_database(session, tenant_id):
    week = weekly.week(session, tenant_id)
    assert sorted(week) == list(range(7))
    assert week[MONDAY] == {"dinein": [], "pickup": [], "delivery": []}
    assert week[TUESDAY]["pickup"] == SEEDED


def test_the_owner_changes_one_service_of_one_weekday(session, tenant_id):
    changed = _set(
        session, tenant_id, TUESDAY, {"pickup": [Span(time(12), time(15, 30))]}
    )
    assert changed is True
    assert _windows(session, tenant_id, TODAY) == [("12:00", "15:30")]
    # Nothing else moved: the other services of the day, the other days.
    assert _windows(session, tenant_id, TODAY, "dinein") == [
        ("11:30", "14:00"),
        ("17:00", "22:00"),
    ]
    assert weekly.week(session, tenant_id)[SATURDAY]["pickup"] == SEEDED
    (audit,) = _audits(session, "opening_hours.changed")
    assert audit.actor == "gui:owner" and audit.tenant_id == tenant_id
    payload = audit.payload or {}
    assert payload["weekday"] == TUESDAY
    assert payload["from"]["pickup"] == ["11:30-14:00", "17:00-22:00"]
    assert payload["to"]["pickup"] == ["12:00-15:30"]
    assert payload["to"]["dinein"] == payload["from"]["dinein"]


def test_the_same_hours_again_change_nothing_and_leave_no_audit_row(session, tenant_id):
    spans = {service: list(SEEDED) for service in ("dinein", "pickup", "delivery")}
    assert _set(session, tenant_id, TUESDAY, spans) is False
    assert _audits(session, "opening_hours.changed") == []


def test_hours_another_device_changed_since_are_not_overwritten(session, tenant_id):
    stale = _seen(session, tenant_id, TUESDAY)
    _set(session, tenant_id, TUESDAY, {"pickup": [Span(time(12), time(15))]})
    with pytest.raises(Conflict):
        _set(session, tenant_id, TUESDAY, {"pickup": []}, seen=stale)
    assert _windows(session, tenant_id, TODAY) == [("12:00", "15:00")]
    assert len(_audits(session, "opening_hours.changed")) == 1


def test_a_weekday_without_hours_is_closed(session, tenant_id):
    spans: dict[str, list[Span]] = {"dinein": [], "pickup": [], "delivery": []}
    assert _set(session, tenant_id, TUESDAY, spans) is True
    assert _windows(session, tenant_id, TODAY) == []
    assert not _open(session, tenant_id, NOW.replace(hour=12))
    # A closed day gets its hours back.
    assert _set(session, tenant_id, TUESDAY, {"pickup": list(SEEDED)}) is True
    assert _open(session, tenant_id, NOW.replace(hour=12))


def test_an_end_before_the_start_needs_the_owners_word(session, tenant_id):
    """Swapped times would open the night (#596), on every week to come."""
    with pytest.raises(InvalidInput) as refused:
        _set(session, tenant_id, TUESDAY, {"pickup": [Span(time(18), time(1))]})
    assert refused.value.say == staff.OVERNIGHT
    assert weekly.week(session, tenant_id)[TUESDAY]["pickup"] == SEEDED


def test_a_span_past_midnight_holds_into_the_next_day(session, tenant_id):
    night = Span(time(18), time(1), overnight=True)
    assert _set(session, tenant_id, TUESDAY, {"pickup": [night]}) is True
    wednesday = TODAY + timedelta(days=1)
    assert _open(session, tenant_id, datetime.combine(wednesday, time(0, 59), BERLIN))
    assert not _open(session, tenant_id, datetime.combine(wednesday, time(1), BERLIN))
    # Shown again, the box is ticked: an untouched save changes nothing.
    assert weekly.week(session, tenant_id)[TUESDAY]["pickup"] == [night]
    assert _set(session, tenant_id, TUESDAY, {"pickup": [night]}) is False


@pytest.mark.parametrize(
    ("saturday", "closes_utc"),
    [
        # The clock goes forward at 02:00: 03:00 comes an hour early.
        (date(2027, 3, 27), datetime(2027, 3, 28, 1, 0, tzinfo=UTC)),
        # The clock goes back at 03:00: the night is an hour longer.
        (date(2026, 10, 24), datetime(2026, 10, 25, 2, 0, tzinfo=UTC)),
    ],
)
def test_a_night_over_a_clock_change_ends_at_the_restaurants_clock(
    session, tenant_id, saturday, closes_utc
):
    night = Span(time(22), time(3), overnight=True)
    _set(session, tenant_id, SATURDAY, {"pickup": [night]})
    assert saturday.weekday() == SATURDAY
    assert _open(session, tenant_id, closes_utc - timedelta(minutes=1))
    assert not _open(session, tenant_id, closes_utc)


def test_a_special_day_beats_the_weekly_hours_of_its_date_only(session, tenant_id):
    _set(session, tenant_id, TUESDAY, {"pickup": [Span(time(10), time(23))]})
    later = TODAY + timedelta(days=7)
    staff.set_day_hours(session, tenant_id, later, DayHours(closed=True), NOW)
    noon = time(12)
    assert not _open(session, tenant_id, datetime.combine(later, noon, BERLIN))
    assert _open(session, tenant_id, datetime.combine(TODAY, noon, BERLIN))
    week_after = later + timedelta(days=7)
    assert _open(session, tenant_id, datetime.combine(week_after, noon, BERLIN))


@pytest.mark.parametrize(
    ("spans", "say"),
    [
        ([Span(time(12), time(12))], staff.NO_SPAN),
        ([Span(time(11), time(15)), Span(time(14), time(20))], weekly.OVERLAP),
        # A night that starts inside the evening span.
        (
            [Span(time(17), time(23)), Span(time(22), time(2), overnight=True)],
            weekly.OVERLAP,
        ),
        ([Span(time(h), time(h, 30)) for h in range(8, 13)], weekly.TOO_MANY),
    ],
)
def test_hours_that_cannot_hold_are_refused_and_nothing_is_written(
    session, tenant_id, spans, say
):
    with pytest.raises(InvalidInput) as refused:
        _set(session, tenant_id, TUESDAY, {"pickup": spans})
    assert refused.value.say == say
    assert weekly.week(session, tenant_id)[TUESDAY]["pickup"] == SEEDED
    assert _audits(session, "opening_hours.changed") == []


def test_spans_that_touch_are_refused_as_one_opening_split_in_two(session, tenant_id):
    """Every reader takes a span's end for a closing time: 11:00 to 14:00 and
    14:00 to 20:00 would close the kitchen at 14:00 for the agent."""
    spans = [Span(time(11), time(14)), Span(time(14), time(20))]
    with pytest.raises(InvalidInput) as refused:
        _set(session, tenant_id, TUESDAY, {"pickup": spans})
    assert refused.value.say == weekly.OVERLAP
    assert _set(session, tenant_id, TUESDAY, {"pickup": [Span(time(11), time(20))]})


def _night(closes: time) -> list[Span]:
    return [Span(time(18), closes, overnight=True)]


def _refused_night(session, tenant_id, weekday, spans, night, following) -> None:
    before = weekly.week(session, tenant_id)[weekday]
    with pytest.raises(InvalidInput) as refused:
        _set(session, tenant_id, weekday, spans)
    assert refused.value.say == weekly.NIGHT_OVERLAP.format(night=night, next=following)
    assert weekly.week(session, tenant_id)[weekday] == before


@pytest.mark.parametrize("service", ["dinein", "pickup"])
def test_a_night_that_reaches_the_next_weekdays_hours_is_refused(
    session, tenant_id, service
):
    """Wednesday opens 11:30 to 14:00. A Tuesday night to 13:00 is the window
    every reader finds first on Wednesday at 12:30 (`hours.open_window_at`
    reads the day before first), and its end would close Wednesday's lunch an
    hour early for the agent (review of #820)."""
    _refused_night(
        session, tenant_id, TUESDAY, {service: _night(time(13))}, "Dienstag", "Mittwoch"
    )
    assert _audits(session, "opening_hours.changed") == []
    # A night that ends as Wednesday opens touches it: refused like two spans
    # of one day. One minute earlier it holds.
    _refused_night(
        session,
        tenant_id,
        TUESDAY,
        {service: _night(time(11, 30))},
        "Dienstag",
        "Mittwoch",
    )
    assert _set(session, tenant_id, TUESDAY, {service: _night(time(11, 29))}) is True


def test_hours_that_start_inside_the_night_before_are_refused(session, tenant_id):
    """The same overlap from the other weekday's form."""
    assert _set(session, tenant_id, TUESDAY, {"pickup": _night(time(2))}) is True
    early = {"pickup": [Span(time(1), time(14))]}
    _refused_night(session, tenant_id, WEDNESDAY, early, "Dienstag", "Mittwoch")
    touching = {"pickup": [Span(time(2), time(14))]}
    _refused_night(session, tenant_id, WEDNESDAY, touching, "Dienstag", "Mittwoch")
    after = {"pickup": [Span(time(2, 1), time(14))]}
    assert _set(session, tenant_id, WEDNESDAY, after) is True


def test_a_sunday_night_is_checked_against_monday(session, tenant_id):
    """The week wraps: Sunday's night runs into Monday."""
    # Monday is closed in the seed: any Sunday night holds.
    assert _set(session, tenant_id, SUNDAY, {"pickup": _night(time(9))}) is True
    monday = {"pickup": [Span(time(8), time(12))]}
    _refused_night(session, tenant_id, MONDAY, monday, "Sonntag", "Montag")
    assert _set(session, tenant_id, SUNDAY, {"pickup": _night(time(3))}) is True
    assert _set(session, tenant_id, MONDAY, monday) is True
    _refused_night(
        session, tenant_id, SUNDAY, {"pickup": _night(time(8))}, "Sonntag", "Montag"
    )


def test_a_night_is_checked_against_its_own_service_only(session, tenant_id):
    """Hours are per service: delivery through the night says nothing about
    Wednesday's lunch in the house. Midnight itself is an end like any other."""
    assert _set(session, tenant_id, TUESDAY, {"delivery": _night(time(0))}) is True
    assert _set(session, tenant_id, WEDNESDAY, {"delivery": []}) is True
    assert _set(session, tenant_id, TUESDAY, {"delivery": _night(time(13))}) is True
    # Wednesday's delivery cannot come back inside that night.
    lunch = {"delivery": [Span(time(11, 30), time(14))]}
    _refused_night(session, tenant_id, WEDNESDAY, lunch, "Dienstag", "Mittwoch")


def test_a_stored_night_into_the_next_day_does_not_block_other_services(
    session, tenant_id
):
    """A service the owner did not change is not checked: rows from
    provisioning that overlap already stay until that service is edited."""
    _store(session, tenant_id, TUESDAY, "delivery", [(time(18), time(13))])
    shown = weekly.week(session, tenant_id)[TUESDAY]
    sent = {**shown, "pickup": [Span(time(12), time(15))]}
    assert _set(session, tenant_id, TUESDAY, sent) is True


def _store(session, tenant_id, weekday, service, spans) -> None:
    """Rows as provisioning or SQL may have left them, past the editor's rules."""
    session.execute(
        delete(OpeningHours).where(
            OpeningHours.tenant_id == tenant_id,
            OpeningHours.weekday == weekday,
            OpeningHours.service == service,
        )
    )
    session.add_all(
        OpeningHours(
            tenant_id=tenant_id,
            weekday=weekday,
            service=service,
            opens_at=opens,
            closes_at=closes,
        )
        for opens, closes in spans
    )
    session.commit()


def test_a_stored_second_is_no_change_the_owner_typed(session, tenant_id):
    """The form shows and sends minutes. An untouched save must not rewrite a
    row stored with seconds, nor claim a change."""
    odd = [(time(11, 30, 30), time(14)), (time(17), time(22))]
    _store(session, tenant_id, TUESDAY, "pickup", odd)
    untouched = {service: list(SEEDED) for service in ("dinein", "pickup", "delivery")}
    assert _set(session, tenant_id, TUESDAY, untouched) is False
    assert _audits(session, "opening_hours.changed") == []
    # Another service changed: the row with its second stays as it is.
    untouched["dinein"] = [Span(time(12), time(15))]
    assert _set(session, tenant_id, TUESDAY, untouched) is True
    stored = weekly.week(session, tenant_id)[TUESDAY]["pickup"]
    assert stored[0].opens == time(11, 30, 30)


@pytest.mark.parametrize(
    "odd",
    [
        # A day and a night without a break: an end equal to the start.
        [(time(0), time(0))],
        # More spans than the form takes.
        [(time(h), time(h, 30)) for h in range(8, 14)],
        # Two spans that touch.
        [(time(11), time(14)), (time(14), time(20))],
    ],
)
def test_rows_the_editor_would_refuse_do_not_block_the_other_services(
    session, tenant_id, odd
):
    _store(session, tenant_id, TUESDAY, "delivery", odd)
    shown = weekly.week(session, tenant_id)[TUESDAY]
    sent = {**shown, "dinein": [Span(time(12), time(15))]}
    assert _set(session, tenant_id, TUESDAY, sent) is True
    after = weekly.week(session, tenant_id)[TUESDAY]
    assert after["dinein"] == [Span(time(12), time(15))]
    assert after["delivery"] == shown["delivery"]
    # Changing that service itself is checked like any other.
    more = [*shown["delivery"], Span(time(22), time(23))]
    if len(more) > weekly.MAX_SPANS:
        with pytest.raises(InvalidInput):
            _set(session, tenant_id, TUESDAY, {"delivery": more})


def test_a_save_tapped_twice_is_no_conflict(session, tenant_id):
    seen = _seen(session, tenant_id, TUESDAY)
    spans = {"pickup": [Span(time(12), time(15))]}
    assert _set(session, tenant_id, TUESDAY, spans, seen=seen) is True
    # The second tap carries the old token and asks for what holds now.
    assert _set(session, tenant_id, TUESDAY, spans, seen=seen) is False
    assert len(_audits(session, "opening_hours.changed")) == 1


def test_a_stale_form_is_refused_as_stale_whatever_it_typed(session, tenant_id):
    """The token is checked before the spans: a typing error in a stale form
    must not come back as a form that then saves over the other device."""
    stale = _seen(session, tenant_id, TUESDAY)
    _set(session, tenant_id, TUESDAY, {"pickup": [Span(time(12), time(15))]})
    with pytest.raises(Conflict):
        _set(session, tenant_id, TUESDAY, {"pickup": [Span(time(9), time(9))]}, stale)


@pytest.mark.parametrize(
    ("weekday", "spans"),
    [(7, {"pickup": []}), (-1, {"pickup": []}), (TUESDAY, {"brunch": []})],
)
def test_an_unknown_weekday_or_service_is_refused(session, tenant_id, weekday, spans):
    with pytest.raises(InvalidInput):
        weekly.set_weekday(session, tenant_id, weekday, spans, "")
    assert session.scalar(select(func.count()).select_from(OpeningHours)) == 36


# --- a date's hours, set by the owner --------------------------------------------


def _special(session, tenant_id) -> list[SpecialDay]:
    return list(
        session.scalars(
            select(SpecialDay)
            .where(SpecialDay.tenant_id == tenant_id)
            .order_by(SpecialDay.date)
            .execution_options(populate_existing=True)
        )
    )


def test_the_owner_sets_longer_hours_for_a_later_date_at_once(session, tenant_id):
    """What the staff's request would wait for (D34 point 4) the owner decides."""
    later = TODAY + timedelta(days=4)
    hours = DayHours(opens=time(9), closes=time(23))
    assert staff.set_day_hours(session, tenant_id, later, hours, NOW) is True
    assert _windows(session, tenant_id, later) == [("09:00", "23:00")]
    # The owner's own change: no check card and no push.
    assert session.scalar(select(func.count()).select_from(StaffChange)) == 0
    assert session.scalar(select(func.count()).select_from(OutboxEvent)) == 0
    (audit,) = _audits(session, "special_day.set")
    payload = audit.payload or {}
    assert audit.actor == "gui:owner"
    assert payload["day"] == later.isoformat()
    assert payload["from"]["opens"] is None
    assert payload["to"]["opens"] == "09:00"


def test_the_owner_returns_a_date_to_the_weekly_hours(session, tenant_id):
    later = TODAY + timedelta(days=4)
    staff.set_day_hours(session, tenant_id, later, DayHours(closed=True), NOW)
    assert _windows(session, tenant_id, later) == []
    assert staff.set_day_hours(session, tenant_id, later, DayHours(), NOW) is True
    assert _special(session, tenant_id) == []
    assert _windows(session, tenant_id, later) == [
        ("11:30", "14:00"),
        ("17:00", "22:00"),
    ]


def test_what_holds_already_changes_nothing(session, tenant_id):
    later = TODAY + timedelta(days=4)
    assert staff.set_day_hours(session, tenant_id, later, DayHours(), NOW) is False
    staff.set_day_hours(session, tenant_id, later, DayHours(closed=True), NOW)
    again = staff.set_day_hours(session, tenant_id, later, DayHours(closed=True), NOW)
    assert again is False
    assert len(_audits(session, "special_day.set")) == 1


def test_a_holidays_name_stays_when_the_owner_changes_its_hours(session, tenant_id):
    later = TODAY + timedelta(days=4)
    session.add(
        SpecialDay(tenant_id=tenant_id, date=later, like_weekday=6, note="Feiertag")
    )
    session.commit()
    hours = DayHours(opens=time(12), closes=time(18))
    staff.set_day_hours(session, tenant_id, later, hours, NOW)
    (row,) = _special(session, tenant_id)
    assert (row.note, row.like_weekday, row.opens_at) == ("Feiertag", None, time(12))


@pytest.mark.parametrize(
    ("day", "hours", "overnight", "say"),
    [
        (TODAY - timedelta(days=1), DayHours(closed=True), False, staff.PAST_DAY),
        (TODAY, DayHours(opens=time(12)), False, staff.NO_SPAN),
        (TODAY, DayHours(opens=time(12), closes=time(12)), True, staff.NO_SPAN),
        (TODAY, DayHours(opens=time(18), closes=time(2)), False, staff.OVERNIGHT),
    ],
)
def test_a_date_the_owner_cannot_set_is_refused(
    session, tenant_id, day, hours, overnight, say
):
    with pytest.raises(InvalidInput) as refused:
        staff.set_day_hours(session, tenant_id, day, hours, NOW, overnight=overnight)
    assert refused.value.say == say
    assert _special(session, tenant_id) == []


def test_a_night_on_a_date_holds_with_the_owners_word(session, tenant_id):
    hours = DayHours(opens=time(18), closes=time(2))
    staff.set_day_hours(session, tenant_id, TODAY, hours, NOW, overnight=True)
    tomorrow = TODAY + timedelta(days=1)
    assert _open(session, tenant_id, datetime.combine(tomorrow, time(1, 59), BERLIN))
    assert not _open(session, tenant_id, datetime.combine(tomorrow, time(2), BERLIN))


def test_a_date_with_an_open_card_is_decided_on_the_card_first(session, tenant_id):
    """Two answers to one staff change would leave the card saying what no
    longer holds."""
    later = TODAY + timedelta(days=4)
    asked = DayHours(opens=time(9), closes=time(23))
    card = staff.change_day_hours(session, tenant_id, later, asked, NOW)
    assert card is not None and not card.held
    with pytest.raises(Conflict) as refused:
        staff.set_day_hours(session, tenant_id, later, DayHours(closed=True), NOW)
    assert refused.value.say == staff.CARD_OPEN
    assert _special(session, tenant_id) == []
    assert _audits(session, "special_day.set") == []


def test_the_coming_special_days_by_date_without_suggestions(session, tenant_id):
    for offset, status in ((-1, "set"), (9, "set"), (2, "set"), (5, "suggested")):
        session.add(
            SpecialDay(
                tenant_id=tenant_id,
                date=TODAY + timedelta(days=offset),
                closed=status == "set",
                like_weekday=None if status == "set" else 6,
                status=status,
            )
        )
    session.commit()
    coming = staff.coming_days(session, tenant_id, TODAY)
    assert [day for day, _ in coming] == [
        TODAY + timedelta(days=2),
        TODAY + timedelta(days=9),
    ]
    assert all(hours.closed for _, hours in coming)


# --- confirmed tables the hours no longer cover -------------------------------------


def _book(
    session,
    tenant_id,
    day,
    at,
    name,
    status="confirmed",
    is_test=False,
    off_hours=False,
):
    session.add(
        Reservation(
            tenant_id=tenant_id,
            source="tablet",
            status=status,
            guest_name=name,
            phone="+491701234567",
            party_size=2,
            reserved_for=datetime.combine(day, at, tzinfo=BERLIN),
            idempotency_key=str(uuid.uuid4()),
            is_test=is_test,
            off_hours=off_hours,
        )
    )
    session.commit()


def _outside(session, tenant_id) -> list[str]:
    return [b.guest_name for b in weekly.tables_outside_hours(session, tenant_id, NOW)]


def test_tables_the_shorter_week_leaves_without_hours_are_listed(session, tenant_id):
    next_week = TODAY + timedelta(days=7)
    _book(session, tenant_id, TODAY, time(21), "Heute spät")
    _book(session, tenant_id, next_week, time(21), "Nächste Woche spät")
    _book(session, tenant_id, next_week, time(19), "Bleibt")
    _book(session, tenant_id, next_week, time(21, 30), "Storniert", status="cancelled")
    _book(session, tenant_id, next_week, time(21, 30), "Selbsttest", is_test=True)
    _book(session, tenant_id, TODAY, time(9), "Vorbei")
    _book(session, tenant_id, next_week, time(21, 15), "Gelöscht")
    gone = session.scalars(
        select(Reservation).where(Reservation.guest_name == "Gelöscht")
    ).one()
    gone.deleted_at = NOW
    session.commit()
    assert _outside(session, tenant_id) == []

    shorter = [Span(time(11, 30), time(14)), Span(time(17), time(20))]
    _set(session, tenant_id, TUESDAY, {"dinein": shorter})
    assert _outside(session, tenant_id) == ["Heute spät", "Nächste Woche spät"]
    (first, _) = weekly.tables_outside_hours(session, tenant_id, NOW)
    # The restaurant's clock, not UTC.
    assert first.at.utcoffset() == timedelta(hours=1) and first.at.hour == 21


def test_a_table_booked_outside_the_hours_on_purpose_is_not_listed(session, tenant_id):
    """The team entered it on purpose ("Trotzdem eintragen", #819): no call."""
    next_week = TODAY + timedelta(days=7)
    _book(session, tenant_id, next_week, time(15), "Bewusst", off_hours=True)
    _book(session, tenant_id, next_week, time(15, 30), "Zu prüfen")

    assert _outside(session, tenant_id) == ["Zu prüfen"]


def test_a_date_with_hours_of_its_own_keeps_its_tables(session, tenant_id):
    next_week = TODAY + timedelta(days=7)
    _book(session, tenant_id, next_week, time(21), "Sondertag hält")
    _book(session, tenant_id, next_week + timedelta(days=7), time(21), "Ohne Zeiten")
    _set(session, tenant_id, TUESDAY, {"dinein": [Span(time(17), time(20))]})
    hours = DayHours(opens=time(17), closes=time(23))
    staff.set_day_hours(session, tenant_id, next_week, hours, NOW)
    assert _outside(session, tenant_id) == ["Ohne Zeiten"]


def test_a_table_in_the_night_of_the_day_before_is_kept(session, tenant_id):
    """A span past midnight covers the table at 00:30 of the next date."""
    wednesday = TODAY + timedelta(days=1)
    _book(session, tenant_id, wednesday, time(0, 30), "Nachts")
    assert _outside(session, tenant_id) == ["Nachts"]
    night = Span(time(17), time(1), overnight=True)
    _set(session, tenant_id, TUESDAY, {"dinein": [night]})
    assert _outside(session, tenant_id) == []


# --- a holiday returned to the weekly hours -----------------------------------------

CHRISTMAS = date(2026, 12, 25)


def _answered_holiday(session, tenant_id) -> None:
    """The holiday job's row after the team's answer "wie Sonntag"."""
    session.add(
        SpecialDay(
            tenant_id=tenant_id,
            date=CHRISTMAS,
            like_weekday=6,
            note="1. Weihnachtstag",
            status="set",
        )
    )
    session.commit()


def test_a_holiday_returned_to_the_weekly_hours_is_not_suggested_again(
    session, tenant_id
):
    """Deleted, the row would let the holiday job suggest the date again, and
    one tap on the board would undo the owner's decision without a card."""
    _answered_holiday(session, tenant_id)
    assert staff.set_day_hours(session, tenant_id, CHRISTMAS, DayHours(), NOW) is True
    (row,) = _special(session, tenant_id)
    assert (row.status, row.note, row.closed) == (
        "dismissed",
        "1. Weihnachtstag",
        False,
    )
    # The weekly hours hold (a Friday), and nothing is listed as a special day.
    assert _windows(session, tenant_id, CHRISTMAS) == [
        ("11:30", "14:00"),
        ("17:00", "22:00"),
    ]
    assert staff.coming_days(session, tenant_id, TODAY) == []
    holidays.suggest(session, tenant_id, "BW", TODAY)
    session.commit()
    statuses = [r.status for r in _special(session, tenant_id) if r.date == CHRISTMAS]
    assert statuses == ["dismissed"]


def test_a_holiday_with_an_older_answer_is_returned_without_a_second_one(
    session, tenant_id
):
    """One suggestion or dismissal per date (`uq_special_days_suggestion`)."""
    _answered_holiday(session, tenant_id)
    staff.set_day_hours(session, tenant_id, CHRISTMAS, DayHours(), NOW)
    # The owner closes the date after all, then opens it again.
    staff.set_day_hours(session, tenant_id, CHRISTMAS, DayHours(closed=True), NOW)
    assert staff.set_day_hours(session, tenant_id, CHRISTMAS, DayHours(), NOW) is True
    rows = [(r.status, r.closed) for r in _special(session, tenant_id)]
    assert rows == [("dismissed", False)]


def test_a_date_without_a_holidays_name_is_simply_removed(session, tenant_id):
    staff.set_day_hours(session, tenant_id, CHRISTMAS, DayHours(closed=True), NOW)
    staff.set_day_hours(session, tenant_id, CHRISTMAS, DayHours(), NOW)
    assert _special(session, tenant_id) == []


def test_a_holiday_closed_before_the_job_named_it_is_answered_too(session, tenant_id):
    """The owner closes 25.12. before any suggestion exists (no name on the
    row), then returns it to the weekly hours: the date is a holiday of the
    restaurant's state, so the answer is kept and the job does not ask again."""
    config = session.get_one(ServiceConfig, tenant_id)
    config.holiday_region = "BW"
    session.commit()
    staff.set_day_hours(session, tenant_id, CHRISTMAS, DayHours(closed=True), NOW)
    staff.set_day_hours(session, tenant_id, CHRISTMAS, DayHours(), NOW)
    (row,) = _special(session, tenant_id)
    assert (row.status, row.note) == ("dismissed", "1. Weihnachtstag")
    holidays.suggest(session, tenant_id, "BW", TODAY)
    session.commit()
    statuses = [r.status for r in _special(session, tenant_id) if r.date == CHRISTMAS]
    assert statuses == ["dismissed"]
    # A date that is no holiday there is removed without a trace.
    plain = CHRISTMAS - timedelta(days=4)
    staff.set_day_hours(session, tenant_id, plain, DayHours(closed=True), NOW)
    staff.set_day_hours(session, tenant_id, plain, DayHours(), NOW)
    assert [r.date for r in _special(session, tenant_id) if r.date == plain] == []


LUNCH_AND_EVENING = [("11:30", "14:00"), ("17:00", "22:00")]


def _christmas(session, tenant_id) -> list[tuple[str, bool]]:
    rows = _special(session, tenant_id)
    return [(r.status, r.closed) for r in rows if r.date == CHRISTMAS]


def _suggest(session, tenant_id) -> None:
    config = session.get_one(ServiceConfig, tenant_id)
    config.holiday_region = "BW"
    holidays.suggest(session, tenant_id, "BW", TODAY)
    session.commit()


def _open_suggestion(session, tenant_id) -> list[uuid.UUID]:
    suggestions = holidays.open_suggestions(session, tenant_id, TODAY)
    return [s.id for s in suggestions if s.date == CHRISTMAS]


def test_a_suggested_holiday_returned_to_the_weekly_hours_is_answered(
    session, tenant_id
):
    """The job suggested 25.12., the owner closes the date and then returns it
    to the weekly hours: that answers the suggestion too. Left open, "closed"
    tapped on its card would close the date again (review of #820)."""
    _suggest(session, tenant_id)
    (card,) = _open_suggestion(session, tenant_id)
    staff.set_day_hours(session, tenant_id, CHRISTMAS, DayHours(closed=True), NOW)
    # The owner's row counts and answers the suggestion (#764).
    assert sorted(_christmas(session, tenant_id)) == [
        ("dismissed", False),
        ("set", True),
    ]
    assert _open_suggestion(session, tenant_id) == []
    assert staff.set_day_hours(session, tenant_id, CHRISTMAS, DayHours(), NOW) is True
    assert _christmas(session, tenant_id) == [("dismissed", False)]
    assert _open_suggestion(session, tenant_id) == []
    # A tablet that still shows the card: its answer changes nothing.
    holidays.answer(session, tenant_id, card, holidays.CLOSED)
    assert _christmas(session, tenant_id) == [("dismissed", False)]
    assert _windows(session, tenant_id, CHRISTMAS) == LUNCH_AND_EVENING
    _suggest(session, tenant_id)
    assert _open_suggestion(session, tenant_id) == []


def test_a_holiday_the_owner_closed_first_gets_no_suggestion_later(session, tenant_id):
    """The other order: the owner's row is there when the job runs, so the
    job writes none, and the date returned to the weekly hours stays answered."""
    staff.set_day_hours(session, tenant_id, CHRISTMAS, DayHours(closed=True), NOW)
    _suggest(session, tenant_id)
    assert _christmas(session, tenant_id) == [("set", True)]
    assert staff.set_day_hours(session, tenant_id, CHRISTMAS, DayHours(), NOW) is True
    _suggest(session, tenant_id)
    assert _christmas(session, tenant_id) == [("dismissed", False)]
    assert _open_suggestion(session, tenant_id) == []
    assert _windows(session, tenant_id, CHRISTMAS) == LUNCH_AND_EVENING


def test_a_suggested_holiday_the_staff_reopen_is_answered_too(session, tenant_id):
    """The staff's way through the same write: closing holds at once, taking
    it back returns the date to the weekly hours and answers the suggestion."""
    _suggest(session, tenant_id)
    staff.change_day_hours(session, tenant_id, CHRISTMAS, DayHours(closed=True), NOW)
    staff.change_day_hours(session, tenant_id, CHRISTMAS, DayHours(), NOW)
    assert _christmas(session, tenant_id) == [("dismissed", False)]
    assert _open_suggestion(session, tenant_id) == []


def test_rows_that_differ_only_by_seconds_are_no_change(session, tenant_id):
    """Stored rows sort by their seconds, typed ones cannot: the comparison
    must not depend on that order."""
    odd = [(time(11, 30, 30), time(12)), (time(11, 30, 10), time(14))]
    _store(session, tenant_id, TUESDAY, "delivery", odd)
    shown = weekly.week(session, tenant_id)[TUESDAY]
    typed = [Span(time(11, 30), time(12)), Span(time(11, 30), time(14))]
    assert _set(session, tenant_id, TUESDAY, {**shown, "delivery": typed}) is False


def test_an_end_at_midnight_without_the_box_is_named_as_such(session, tenant_id):
    """18:00 to 00:00 ends after midnight; the refusal says why (#819)."""
    with pytest.raises(InvalidInput) as refused:
        _set(session, tenant_id, TUESDAY, {"pickup": [Span(time(18), time(0))]})
    assert refused.value.say == staff.AT_MIDNIGHT
    assert "00:00" in staff.AT_MIDNIGHT and "Ende nach Mitternacht" in (
        staff.AT_MIDNIGHT
    )
    # The rule itself is unchanged: with the box the span holds.
    night = Span(time(18), time(0), overnight=True)
    assert _set(session, tenant_id, TUESDAY, {"pickup": [night]}) is True


def test_a_date_ending_at_midnight_without_the_box_is_named_as_such(session, tenant_id):
    hours = DayHours(opens=time(18), closes=time(0))
    with pytest.raises(InvalidInput) as refused:
        staff.set_day_hours(session, tenant_id, TODAY, hours, NOW)
    assert refused.value.say == staff.AT_MIDNIGHT
    assert staff.set_day_hours(session, tenant_id, TODAY, hours, NOW, overnight=True)


def test_every_fault_of_a_weekday_is_named_at_once_by_its_span(session, tenant_id):
    """All faults of one save, each by the service and span it is about
    (#821, #829-3); nothing is written while one stands."""
    spans = {
        "pickup": [Span(time(18), time(1))],
        "delivery": [Span(time(11), time(15)), Span(time(14), time(20))],
        "dinein": [Span(time(h), time(h, 30)) for h in range(8, 13)],
    }
    with pytest.raises(FieldErrors) as refused:
        _set(session, tenant_id, TUESDAY, spans)
    assert refused.value.faults == {
        "pickup 18:00-01:00": staff.OVERNIGHT,
        "delivery 14:00-20:00": weekly.OVERLAP,
        "dinein": weekly.TOO_MANY,
    }
    # The first fault in the order of the services, for a reader of one line.
    assert refused.value.say == weekly.TOO_MANY
    assert weekly.week(session, tenant_id)[TUESDAY]["pickup"] == SEEDED


def test_a_night_into_the_next_weekday_is_named_by_its_service(session, tenant_id):
    night = Span(time(18), time(13), overnight=True)
    with pytest.raises(FieldErrors) as refused:
        _set(session, tenant_id, TUESDAY, {"pickup": [night]})
    assert list(refused.value.faults) == ["pickup"]
    assert "Dienstag" in refused.value.faults["pickup"]


# --- the night rule across the dates with hours of their own (#877) -------------

SATURDAY_14 = date(2026, 11, 14)


def _dated(day: date) -> str:
    return f"{WEEKDAYS[day.weekday()]}, {day:%d.%m.}"


def _set_at_now(session, tenant_id, weekday, spans) -> bool:
    seen = _seen(session, tenant_id, weekday)
    return weekly.set_weekday(session, tenant_id, weekday, spans, seen, now=NOW)


def _refused_dated(session, tenant_id, weekday, spans, night: date) -> None:
    before = weekly.week(session, tenant_id)[weekday]
    with pytest.raises(FieldErrors) as refused:
        _set_at_now(session, tenant_id, weekday, spans)
    assert refused.value.say == weekly.NIGHT_OVERLAP.format(
        night=_dated(night), next=_dated(night + timedelta(days=1))
    )
    assert weekly.week(session, tenant_id)[weekday] == before


def _date_rows(session, tenant_id, rows) -> None:
    """Dates as an older writer may have left them, past the night rule."""
    session.add_all(
        SpecialDay(tenant_id=tenant_id, date=day, opens_at=opens, closes_at=closes)
        for day, opens, closes in rows
    )
    session.commit()


def test_a_weekday_that_starts_inside_a_dates_night_is_refused(session, tenant_id):
    """Saturday 14.11 runs to 03:00 on hours of its own. Weekly Sunday from
    02:00 would be read inside that night on Sunday 15.11, closing at 03:00:
    the weekly writer reads the dates beside the weekday too."""
    night = DayHours(opens=time(18), closes=time(3))
    assert staff.set_day_hours(
        session, tenant_id, SATURDAY_14, night, NOW, overnight=True
    )
    early = {"pickup": [Span(time(2), time(10))]}
    _refused_dated(session, tenant_id, SUNDAY, early, SATURDAY_14)
    later = [Span(time(3, 1), time(10))]
    assert _set_at_now(session, tenant_id, SUNDAY, {"pickup": later})
    # The rows the check built to read the new hours were never written.
    assert weekly.week(session, tenant_id)[SUNDAY]["pickup"] == later


def test_a_weekly_night_into_a_dates_own_hours_is_refused(session, tenant_id):
    """The other way: Sunday 15.11 opens at 10:00 on hours of its own, so a
    weekly Saturday night to 11:00 reaches into it, though not into the
    weekly Sunday's 11:30."""
    sunday = SATURDAY_14 + timedelta(days=1)
    morning = DayHours(opens=time(10), closes=time(14))
    assert staff.set_day_hours(session, tenant_id, sunday, morning, NOW)
    _refused_dated(
        session,
        tenant_id,
        SATURDAY,
        {"pickup": [Span(time(18), time(11), overnight=True)]},
        SATURDAY_14,
    )


def test_a_date_like_a_weekday_is_checked_with_that_weekdays_new_hours(
    session, tenant_id
):
    """Wednesday 18.11 is "wie Sonntag" and Tuesday 17.11 runs to 03:00: a
    weekly Sunday from 02:00 would start inside that night on the 18th."""
    tuesday = date(2026, 11, 17)
    night = DayHours(opens=time(18), closes=time(3))
    assert staff.set_day_hours(session, tenant_id, tuesday, night, NOW, overnight=True)
    # A holiday answered "wie Sonntag" (`holidays.answer`) writes such a row.
    session.add(
        SpecialDay(
            tenant_id=tenant_id, date=tuesday + timedelta(days=1), like_weekday=SUNDAY
        )
    )
    session.commit()
    _refused_dated(
        session, tenant_id, SUNDAY, {"pickup": [Span(time(2), time(10))]}, tuesday
    )


def test_an_old_overlap_of_two_dates_blocks_no_other_weekday(session, tenant_id):
    """Rows from before the rule: Saturday 14.11 to 03:00, Sunday 15.11 from
    02:00. Tuesday's weekly hours are no part of it and save; a past night
    (Sunday 8.11 into Monday 9.11, before today) blocks no Monday save either."""
    sunday = SATURDAY_14 + timedelta(days=1)
    _date_rows(
        session,
        tenant_id,
        [
            (SATURDAY_14, time(18), time(3)),
            (sunday, time(2), time(10)),
            (date(2026, 11, 8), time(18), time(3)),
        ],
    )
    assert _set_at_now(
        session, tenant_id, TUESDAY, {"pickup": [Span(time(12), time(15))]}
    )
    assert _set_at_now(session, tenant_id, MONDAY, {"pickup": [Span(time(1), time(9))]})


def test_a_date_change_may_ease_an_old_overlap_but_never_deepen_it(session, tenant_id):
    """#877: an overlap the date had before no longer blocks a change that
    leaves less of it (a shorter night, a later start); one that reaches
    further in is refused. Before, only "closed" passed."""
    sunday = SATURDAY_14 + timedelta(days=1)
    _date_rows(
        session,
        tenant_id,
        [(SATURDAY_14, time(18), time(3)), (sunday, time(2), time(10))],
    )
    later = DayHours(opens=time(2, 30), closes=time(10))
    assert staff.set_day_hours(session, tenant_id, sunday, later, NOW)
    shorter = DayHours(opens=time(18), closes=time(2, 45))
    assert staff.set_day_hours(
        session, tenant_id, SATURDAY_14, shorter, NOW, overnight=True
    )
    longer = DayHours(opens=time(18), closes=time(4))
    with pytest.raises(InvalidInput) as refused:
        staff.set_day_hours(
            session, tenant_id, SATURDAY_14, longer, NOW, overnight=True
        )
    assert refused.value.say == weekly.NIGHT_OVERLAP.format(
        night="Samstag", next="Sonntag"
    )
    assert staff.day_hours(session, tenant_id, SATURDAY_14) == shorter


def test_a_change_may_not_let_a_night_cut_a_span_it_held(session, tenant_id):
    """Deep review of #877: the depth of an overlap alone is no measure. A span
    wholly inside the night is read inside it and closes no earlier; shortening
    the night so that it ends inside that span, or stretching the span past the
    night's end, would close it at the night's end for the agent."""
    sunday = SATURDAY_14 + timedelta(days=1)
    _date_rows(
        session,
        tenant_id,
        [(SATURDAY_14, time(18), time(3)), (sunday, time(1), time(2, 30))],
    )
    shorter = DayHours(opens=time(18), closes=time(2))
    with pytest.raises(InvalidInput):
        staff.set_day_hours(
            session, tenant_id, SATURDAY_14, shorter, NOW, overnight=True
        )
    longer = DayHours(opens=time(1), closes=time(10))
    with pytest.raises(InvalidInput):
        staff.set_day_hours(session, tenant_id, sunday, longer, NOW)
    # Still wholly inside the night: nothing is read wrong, the change holds.
    inside = DayHours(opens=time(0, 30), closes=time(2, 45))
    assert staff.set_day_hours(session, tenant_id, sunday, inside, NOW)


def test_a_night_that_is_over_blocks_no_weekly_save(session, tenant_id):
    """Review of #877: Saturday 14.11 runs to 03:00. At Sunday 02:30 a weekly
    Sunday from 02:00 is refused, the night still runs; at 15:00 that night is
    over, nobody reads it, and the date is past changing: the save holds."""
    night = DayHours(opens=time(18), closes=time(3))
    assert staff.set_day_hours(
        session, tenant_id, SATURDAY_14, night, NOW, overnight=True
    )
    sunday = SATURDAY_14 + timedelta(days=1)
    early = {"pickup": [Span(time(2), time(10))]}
    for hour, refused in ((time(2, 30), True), (time(15), False)):
        at = datetime.combine(sunday, hour, tzinfo=BERLIN)
        seen = _seen(session, tenant_id, SUNDAY)
        if refused:
            with pytest.raises(FieldErrors):
                weekly.set_weekday(session, tenant_id, SUNDAY, early, seen, now=at)
        else:
            assert weekly.set_weekday(session, tenant_id, SUNDAY, early, seen, now=at)
