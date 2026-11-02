"""How long before its time a table can be reserved, per weekday (T-3.13, #499)."""

import uuid
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from api.core.errors import Conflict, InvalidInput
from api.domain.reservations import check_slot, create_reservation
from api.domain.reservations import notice as notices
from api.models import AuditLog, Call, Capacity, Reservation, SpecialDay
from api.schemas.reservations import CreateReservationRequest
from scripts.seed import seed

BERLIN = ZoneInfo("Europe/Berlin")
# Seed: closed on Monday, seats 17:00 to 22:00 with 40 guests, grid of 30 minutes.
DIENSTAG = date(2026, 9, 15)
TUESDAY = 1
SUNDAY = 6
NOW = datetime(2026, 9, 15, 18, 0, tzinfo=BERLIN)


def berlin(hh: int, mm: int = 0) -> datetime:
    return datetime.combine(DIENSTAG, time(hh, mm), tzinfo=BERLIN)


@pytest.fixture
def engine(migrated_db_url):
    engine = create_engine(migrated_db_url)
    yield engine
    engine.dispose()


@pytest.fixture
def session(engine):
    with Session(engine) as s:
        yield s


@pytest.fixture
def tenant_id(session) -> uuid.UUID:
    return uuid.UUID(
        seed(session, tenant_name="Testbetrieb", timezone="Europe/Berlin").tenant_id
    )


@pytest.fixture
def call_id(session, tenant_id) -> uuid.UUID:
    call = Call(
        tenant_id=tenant_id,
        external_session_id=uuid.uuid4().hex,
        started_at=NOW,
        delete_after=DIENSTAG,
    )
    session.add(call)
    session.commit()
    return call.id


def book(session, tenant_id, call_id, guests: int) -> None:
    session.add(
        Reservation(
            tenant_id=tenant_id,
            call_id=call_id,
            status="confirmed",
            guest_name="Test",
            phone="+4900000000",
            party_size=guests,
            reserved_for=berlin(20),
            idempotency_key=uuid.uuid4().hex,
        )
    )
    session.commit()


def set_notice(session, tenant_id, minutes: int, weekday: int = TUESDAY) -> None:
    notices.set_notices(session, tenant_id, {weekday: minutes})


def test_without_a_notice_a_table_soon_is_booked_as_before(session, tenant_id):
    assert check_slot(session, tenant_id, berlin(18, 30), 4, now=NOW).available


def test_inside_the_notice_nothing_is_booked_and_the_seats_are_named(
    session, tenant_id
):
    set_notice(session, tenant_id, 60)

    slot = check_slot(session, tenant_id, berlin(18, 30), 4, now=NOW)

    assert slot.available is False
    assert slot.team_callback is False
    assert "Laut unseren Reservierungen ist zu der Zeit noch Platz" in slot.say
    assert "so kurzfristig" in slot.say


def test_inside_the_notice_a_full_evening_is_named_as_full(session, tenant_id, call_id):
    set_notice(session, tenant_id, 60)
    book(session, tenant_id, call_id, 40)

    slot = check_slot(session, tenant_id, berlin(18, 30), 4, now=NOW)

    assert slot.available is False
    assert "Laut unseren Reservierungen ist zu der Zeit leider alles belegt" in slot.say
    assert "noch Platz" not in slot.say


def test_the_seats_are_named_for_this_party(session, tenant_id, call_id):
    """Room for two is no room for six."""
    set_notice(session, tenant_id, 60)
    book(session, tenant_id, call_id, 36)

    assert (
        "noch Platz" in check_slot(session, tenant_id, berlin(18, 30), 2, now=NOW).say
    )
    assert "belegt" in check_slot(session, tenant_id, berlin(18, 30), 6, now=NOW).say


def test_a_table_right_on_the_notice_is_booked(session, tenant_id):
    set_notice(session, tenant_id, 60)

    assert check_slot(session, tenant_id, berlin(19), 4, now=NOW).available


def test_one_second_inside_the_notice_is_inside(session, tenant_id):
    set_notice(session, tenant_id, 60)
    late = NOW + timedelta(seconds=1)

    assert not check_slot(session, tenant_id, berlin(19), 4, now=late).available


def test_the_alternatives_lie_outside_the_notice(session, tenant_id):
    set_notice(session, tenant_id, 60)

    slot = check_slot(session, tenant_id, berlin(18, 30), 4, now=NOW)

    assert slot.alternatives
    assert all(at >= berlin(19) for at in slot.alternatives)


def test_a_wish_at_a_time_nobody_starts_offers_nothing_inside_the_notice(
    session, tenant_id
):
    """17:00 is no start, and the slots it offers instead keep the notice too."""
    set_notice(session, tenant_id, 120)

    slot = check_slot(session, tenant_id, berlin(17), 4, now=berlin(16))

    assert slot.available is False
    assert slot.alternatives
    assert all(at >= berlin(18) for at in slot.alternatives)


def test_a_notice_that_hides_every_slot_is_not_called_full(session, tenant_id):
    """Codex #513: before opening, with a day-long notice, nothing can be offered.
    The seats are free, so "nichts mehr frei" would be false."""
    set_notice(session, tenant_id, 1440)

    slot = check_slot(session, tenant_id, berlin(11), 4, now=berlin(9))

    assert slot.available is False
    assert slot.alternatives == []
    assert "nichts mehr frei" not in slot.say
    assert "so kurzfristig" in slot.say


def test_a_full_day_before_opening_is_still_called_full(session, tenant_id, call_id):
    set_notice(session, tenant_id, 30)
    book(session, tenant_id, call_id, 40)
    for start in (time(12), time(13)):
        session.add(
            Reservation(
                tenant_id=tenant_id,
                call_id=call_id,
                status="confirmed",
                guest_name="Test",
                phone="+4900000000",
                party_size=15,
                reserved_for=datetime.combine(DIENSTAG, start, tzinfo=BERLIN),
                idempotency_key=uuid.uuid4().hex,
            )
        )
    session.commit()

    slot = check_slot(session, tenant_id, berlin(11), 4, now=berlin(9))

    assert "nichts mehr frei" in slot.say


def test_a_big_party_inside_the_notice_gets_no_callback(session, tenant_id):
    """The rule of the day says no booking: a callback would promise a table."""
    set_notice(session, tenant_id, 60)

    slot = check_slot(session, tenant_id, berlin(18, 30), 30, now=NOW)

    assert slot.available is False
    assert slot.team_callback is False


def test_the_notice_of_another_weekday_does_not_count(session, tenant_id):
    set_notice(session, tenant_id, 60, weekday=SUNDAY)

    assert check_slot(session, tenant_id, berlin(18, 30), 4, now=NOW).available


def test_a_day_like_sunday_takes_the_notice_of_sunday(session, tenant_id):
    session.add(
        SpecialDay(
            tenant_id=tenant_id, date=DIENSTAG, like_weekday=SUNDAY, status="set"
        )
    )
    session.commit()
    set_notice(session, tenant_id, 60, weekday=SUNDAY)

    assert not check_slot(session, tenant_id, berlin(18, 30), 4, now=NOW).available


def test_a_closed_time_is_still_said_as_closed(session, tenant_id):
    """The notice is about a table that could be booked, not about the hours."""
    set_notice(session, tenant_id, 600)

    slot = check_slot(session, tenant_id, berlin(23), 4, now=NOW)

    assert "so kurzfristig" not in slot.say


def test_a_draft_inside_the_notice_is_refused(session, tenant_id, call_id):
    """Rule 3: the model may call it anyway, and nothing may come of it."""
    set_notice(session, tenant_id, 60)
    request = CreateReservationRequest(
        idempotency_key="k",
        call_id=call_id,
        tenant_id=tenant_id,
        guest_name="Weber",
        phone="+4972215551234",
        party_size=4,
        reserved_for=berlin(18, 30),
    )

    with pytest.raises(Conflict) as refused:
        create_reservation(session, request, now=NOW)

    assert "so kurzfristig" in refused.value.say
    session.rollback()
    assert session.scalars(select(Reservation)).all() == []


def test_the_notice_holds_for_every_window_of_the_weekday(session, tenant_id):
    set_notice(session, tenant_id, 45)

    rows = session.scalars(
        select(Capacity).where(
            Capacity.tenant_id == tenant_id, Capacity.weekday == TUESDAY
        )
    ).all()

    assert len(rows) == 2
    assert {row.notice_minutes for row in rows} == {45}


def test_the_notices_outlive_a_restart(engine, session, tenant_id):
    set_notice(session, tenant_id, 45)

    with Session(engine) as restarted:
        assert notices.weekday_notices(restarted, tenant_id)[TUESDAY] == 45


def test_a_closed_weekday_has_no_notice(session, tenant_id):
    """Monday has no seats: nothing to set, shown as closed."""
    assert notices.weekday_notices(session, tenant_id)[0] is None


def test_setting_a_closed_weekday_is_refused(session, tenant_id):
    with pytest.raises(InvalidInput):
        notices.set_notices(session, tenant_id, {0: 30})


def test_every_change_stands_in_the_audit(session, tenant_id):
    set_notice(session, tenant_id, 45)

    entries = session.scalars(
        select(AuditLog).where(AuditLog.action == notices.ACTION_NOTICE)
    ).all()

    assert [entry.payload for entry in entries] == [
        {"weekday": TUESDAY, "from": 0, "to": 45}
    ]


def test_the_same_notice_again_writes_nothing(session, tenant_id):
    set_notice(session, tenant_id, 45)
    set_notice(session, tenant_id, 45)

    entries = session.scalars(
        select(AuditLog).where(AuditLog.action == notices.ACTION_NOTICE)
    ).all()

    assert len(entries) == 1


@pytest.mark.parametrize("minutes", [-1, notices.MAX_NOTICE_MINUTES + 1])
def test_a_notice_outside_the_limits_changes_nothing(session, tenant_id, minutes):
    with pytest.raises(InvalidInput) as refused:
        notices.set_notices(session, tenant_id, {TUESDAY: 45, SUNDAY: minutes})

    assert "0 bis 1440 Minuten" in refused.value.say
    session.rollback()
    assert notices.weekday_notices(session, tenant_id)[TUESDAY] == 0


@pytest.mark.parametrize("minutes", [0, notices.MAX_NOTICE_MINUTES])
def test_a_notice_may_stand_on_a_limit(session, tenant_id, minutes):
    set_notice(session, tenant_id, minutes)

    assert notices.weekday_notices(session, tenant_id)[TUESDAY] == minutes
