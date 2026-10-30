"""domain/status/holidays and jobs/holidays: the calendar, suggestions that change
no hours until the team answers, the answers, the pass over tenants (T-9.5)."""

import uuid
from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from api.core.errors import InvalidInput, NotFound
from api.domain.reservations.capacity import capacity_windows
from api.domain.status import get_service_status
from api.domain.status import holidays as days
from api.jobs.holidays import suggest_all
from api.models import AuditLog, Capacity, ServiceConfig, SpecialDay
from api.models.tenants import HOLIDAY_REGIONS
from scripts.seed import seed

BERLIN = ZoneInfo("Europe/Berlin")
# Seed: Monday closed, every other day 11:30-14:00 and 17:00-22:00.
ALL_SAINTS = date(2026, 11, 1)  # a Sunday
CORPUS_CHRISTI = date(2026, 6, 4)  # a Thursday
MONDAY = date(2026, 11, 2)


def berlin(day: date, hh: int, mm: int = 0) -> datetime:
    return datetime.combine(day, time(hh, mm), tzinfo=BERLIN)


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


def _region(session, tenant_id, region: str | None) -> None:
    session.get(ServiceConfig, tenant_id).holiday_region = region
    session.commit()


def _rows(session, tenant_id) -> list[SpecialDay]:
    return list(
        session.scalars(
            select(SpecialDay)
            .where(SpecialDay.tenant_id == tenant_id)
            .order_by(SpecialDay.date)
        )
    )


# --- the calendar ------------------------------------------------------------


@pytest.mark.parametrize(
    ("year", "easter"),
    [
        (2019, date(2019, 4, 21)),
        (2024, date(2024, 3, 31)),
        (2025, date(2025, 4, 20)),
        (2026, date(2026, 4, 5)),
        (2027, date(2027, 3, 28)),
        (2038, date(2038, 4, 25)),
    ],
)
def test_easter_sunday(year, easter):
    assert days.easter_sunday(year) == easter


def test_the_holidays_of_baden_wuerttemberg_2026():
    assert days.holidays("BW", 2026) == [
        (date(2026, 1, 1), "Neujahr"),
        (date(2026, 1, 6), "Heilige Drei Könige"),
        (date(2026, 4, 3), "Karfreitag"),
        (date(2026, 4, 6), "Ostermontag"),
        (date(2026, 5, 1), "Tag der Arbeit"),
        (date(2026, 5, 14), "Christi Himmelfahrt"),
        (date(2026, 5, 25), "Pfingstmontag"),
        (date(2026, 6, 4), "Fronleichnam"),
        (date(2026, 10, 3), "Tag der Deutschen Einheit"),
        (date(2026, 11, 1), "Allerheiligen"),
        (date(2026, 12, 25), "1. Weihnachtstag"),
        (date(2026, 12, 26), "2. Weihnachtstag"),
    ]


def test_state_days_go_only_to_their_states():
    def names(region: str) -> set[str]:
        return {name for _, name in days.holidays(region, 2026)}

    assert "Reformationstag" in names("NI") and "Reformationstag" not in names("BW")
    assert "Buß- und Bettag" in names("SN") and "Buß- und Bettag" not in names("BY")
    assert "Internationaler Frauentag" in names("BE")
    assert "Weltkindertag" in names("TH")
    assert "Mariä Himmelfahrt" in names("SL") and "Mariä Himmelfahrt" not in names("BY")
    # Nine days hold in every state; Hamburg has only Reformationstag on top.
    assert len(names("HH")) == 10


@pytest.mark.parametrize(
    ("year", "day"),
    [
        (2026, date(2026, 11, 18)),
        (2027, date(2027, 11, 17)),
        (2028, date(2028, 11, 22)),
    ],
)
def test_repentance_day_is_the_last_wednesday_before_november_23(year, day):
    assert (day, "Buß- und Bettag") in days.holidays("SN", year)


def test_every_region_of_the_column_has_a_calendar():
    for region in HOLIDAY_REGIONS:
        assert len(days.holidays(region, 2026)) >= 9


def test_an_unknown_region_is_refused():
    with pytest.raises(InvalidInput):
        days.holidays("XX", 2026)


# --- suggestions --------------------------------------------------------------


def test_suggest_writes_the_holidays_of_the_next_sixty_days(session, tenant_id):
    assert days.suggest(session, tenant_id, "BW", date(2026, 10, 1)) == 2
    session.commit()
    assert [
        (r.date, r.note, r.status, r.like_weekday) for r in _rows(session, tenant_id)
    ] == [
        (date(2026, 10, 3), "Tag der Deutschen Einheit", "suggested", days.SUNDAY),
        (ALL_SAINTS, "Allerheiligen", "suggested", days.SUNDAY),
    ]


def test_a_suggestion_changes_no_hours_until_the_team_answers(session, tenant_id):
    """Ostermontag falls on the seed's rest day: like Sunday would open it."""
    easter_monday = date(2026, 4, 6)
    days.suggest(session, tenant_id, "BW", date(2026, 3, 20))
    session.commit()
    assert easter_monday in [r.date for r in _rows(session, tenant_id)]
    status = get_service_status(session, tenant_id, now=berlin(easter_monday, 12))
    assert status.is_open is False


def test_suggest_twice_writes_once_and_keeps_an_answer(session, tenant_id):
    days.suggest(session, tenant_id, "BW", date(2026, 10, 1))
    session.commit()
    first = _rows(session, tenant_id)[0]
    days.answer(session, tenant_id, first.id, days.OPEN)
    assert days.suggest(session, tenant_id, "BW", date(2026, 10, 1)) == 0
    session.commit()
    assert [r.status for r in _rows(session, tenant_id)] == ["dismissed", "suggested"]


def test_a_date_the_team_already_set_is_not_suggested(session, tenant_id):
    session.add(SpecialDay(tenant_id=tenant_id, date=ALL_SAINTS, closed=True))
    session.commit()
    assert days.suggest(session, tenant_id, "BW", date(2026, 10, 20)) == 0


def test_the_horizon_reaches_into_the_next_year(session, tenant_id):
    days.suggest(session, tenant_id, "BW", date(2026, 12, 20))
    session.commit()
    assert [r.date for r in _rows(session, tenant_id)] == [
        date(2026, 12, 25),
        date(2026, 12, 26),
        date(2027, 1, 1),
        date(2027, 1, 6),
    ]


def test_open_suggestions_leave_out_past_and_answered_days(session, tenant_id):
    days.suggest(session, tenant_id, "BW", date(2026, 10, 1))
    session.commit()
    rows = _rows(session, tenant_id)
    days.answer(session, tenant_id, rows[1].id, days.CLOSED)
    assert days.open_suggestions(session, tenant_id, date(2026, 10, 4)) == []
    open_now = days.open_suggestions(session, tenant_id, date(2026, 10, 1))
    assert [(s.date, s.name, s.like_weekday) for s in open_now] == [
        (date(2026, 10, 3), "Tag der Deutschen Einheit", days.SUNDAY)
    ]


# --- answers --------------------------------------------------------------------


def _suggested(session, tenant_id, day: date) -> SpecialDay:
    row = SpecialDay(
        tenant_id=tenant_id,
        date=day,
        note="Feiertag",
        status="suggested",
        like_weekday=days.SUNDAY,
    )
    session.add(row)
    session.commit()
    return row


def test_like_sunday_opens_a_rest_day_with_sunday_hours(session, tenant_id):
    row = _suggested(session, tenant_id, MONDAY)
    assert (
        get_service_status(session, tenant_id, now=berlin(MONDAY, 12)).is_open is False
    )
    days.answer(session, tenant_id, row.id, days.LIKE)
    status = get_service_status(session, tenant_id, now=berlin(MONDAY, 12))
    assert status.is_open is True
    assert status.closes_at == berlin(MONDAY, 14).astimezone(UTC)


def test_like_monday_closes_a_thursday(session, tenant_id):
    row = _suggested(session, tenant_id, CORPUS_CHRISTI)
    row.like_weekday = 0
    session.commit()
    days.answer(session, tenant_id, row.id, days.LIKE)
    status = get_service_status(session, tenant_id, now=berlin(CORPUS_CHRISTI, 12))
    assert status.is_open is False


def test_monday_stays_monday_and_no_weekday_means_sunday(session, tenant_id):
    monday = _suggested(session, tenant_id, CORPUS_CHRISTI)
    monday.like_weekday = 0
    bare = _suggested(session, tenant_id, MONDAY)
    bare.like_weekday = None
    session.commit()
    shown = days.open_suggestions(session, tenant_id, date(2026, 6, 1))
    assert [s.like_weekday for s in shown] == [0, days.SUNDAY]
    days.answer(session, tenant_id, bare.id, days.LIKE)
    session.refresh(bare)
    assert (bare.status, bare.like_weekday) == ("set", days.SUNDAY)
    assert get_service_status(session, tenant_id, now=berlin(MONDAY, 12)).is_open


def test_closed_closes_the_day(session, tenant_id):
    row = _suggested(session, tenant_id, CORPUS_CHRISTI)
    days.answer(session, tenant_id, row.id, days.CLOSED)
    session.refresh(row)
    assert (row.status, row.closed, row.like_weekday) == ("set", True, None)
    status = get_service_status(session, tenant_id, now=berlin(CORPUS_CHRISTI, 12))
    assert status.is_open is False


def test_the_first_answer_holds_and_is_logged_once(session, tenant_id):
    row = _suggested(session, tenant_id, CORPUS_CHRISTI)
    days.answer(session, tenant_id, row.id, days.OPEN)
    days.answer(session, tenant_id, row.id, days.CLOSED)
    session.refresh(row)
    assert (row.status, row.closed) == ("dismissed", False)
    entries = session.scalars(
        select(AuditLog).where(AuditLog.action == days.ACTION_ANSWERED)
    ).all()
    assert [e.payload["answer"] for e in entries] == ["open"]
    assert entries[0].payload["date"] == "2026-06-04"


def test_an_answer_for_another_tenant_or_unknown_choice_is_refused(session, tenant_id):
    row = _suggested(session, tenant_id, CORPUS_CHRISTI)
    with pytest.raises(NotFound):
        days.answer(session, uuid.uuid4(), row.id, days.CLOSED)
    with pytest.raises(InvalidInput):
        days.answer(session, tenant_id, row.id, "maybe")
    session.refresh(row)
    assert row.status == "suggested"


# --- seats ----------------------------------------------------------------------


def test_seats_follow_the_weekday_a_day_is_like(session, tenant_id):
    session.add(
        Capacity(
            tenant_id=tenant_id,
            weekday=days.SUNDAY,
            slot_start=time(9),
            slot_end=time(10),
            max_guests=3,
            slot_minutes=30,
        )
    )
    row = _suggested(session, tenant_id, CORPUS_CHRISTI)

    def nine_oclock() -> list[int]:
        windows = capacity_windows(session, tenant_id, CORPUS_CHRISTI, BERLIN)
        return [w.max_guests for w in windows if w.start.hour == 9]

    # A suggestion leaves the Thursday's seats.
    assert nine_oclock() == []
    days.answer(session, tenant_id, row.id, days.LIKE)
    assert nine_oclock() == [3]


# --- the job ----------------------------------------------------------------------


def test_the_job_suggests_only_for_a_tenant_with_a_region(session, tenant_id):
    now = berlin(date(2026, 10, 1), 3).astimezone(UTC)
    assert suggest_all(session, now) == 0
    _region(session, tenant_id, "BW")
    assert suggest_all(session, now) == 2
    assert suggest_all(session, now) == 0


def test_the_job_takes_the_local_date(session, tenant_id):
    """At 22:30 UTC on 3 Aug it is 4 Aug in Berlin: sixty days reach 3 Oct
    from there, 2 Oct from the UTC date."""
    _region(session, tenant_id, "BW")
    suggest_all(session, datetime(2026, 8, 3, 22, 30, tzinfo=UTC))
    assert [r.date for r in _rows(session, tenant_id)] == [date(2026, 10, 3)]
