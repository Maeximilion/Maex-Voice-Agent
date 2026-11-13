"""How long before its time a table can be reserved, per weekday (T-3.13, #499)."""

import threading
import uuid
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from api.core.errors import Conflict, InvalidInput
from api.domain.confirm import confirm
from api.domain.reservations import check_slot, create_reservation
from api.domain.reservations import notice as notices
from api.domain.reservations.slots import (
    ASK_OTHER_DAY,
    ASK_OTHER_TIME,
    SAY_RESERVATIONS_OFF,
)
from api.domain.status import config as status_config
from api.models import (
    AuditLog,
    Call,
    Capacity,
    Reservation,
    ServiceConfig,
    SpecialDay,
)
from api.schemas.confirm import ConfirmRequest
from api.schemas.reservations import CreateReservationRequest
from scripts.seed import seed

BERLIN = ZoneInfo("Europe/Berlin")
# Seed: closed on Monday, seats 17:00 to 22:00 with 40 guests, grid of 30 minutes.
DIENSTAG = date(2026, 9, 15)
SONNTAG = date(2026, 9, 20)  # the Monday after it is closed
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
    assert slot.say is not None
    assert "Laut unseren Reservierungen ist zu der Zeit noch Platz" in slot.say
    assert slot.say is not None
    assert "so kurzfristig" in slot.say


def test_inside_the_notice_a_full_evening_is_named_as_full(session, tenant_id, call_id):
    set_notice(session, tenant_id, 60)
    book(session, tenant_id, call_id, 40)

    slot = check_slot(session, tenant_id, berlin(18, 30), 4, now=NOW)

    assert slot.available is False
    assert slot.say is not None
    assert "Laut unseren Reservierungen ist zu der Zeit leider alles belegt" in slot.say
    assert slot.say is not None
    assert "noch Platz" not in slot.say
    # Every later slot of the evening is full as well (#527).
    assert slot.say is not None
    assert slot.say.endswith(ASK_OTHER_DAY)


def test_the_seats_are_named_for_this_party(session, tenant_id, call_id):
    """Room for two is no room for six."""
    set_notice(session, tenant_id, 60)
    book(session, tenant_id, call_id, 36)

    two = check_slot(session, tenant_id, berlin(18, 30), 2, now=NOW)
    six = check_slot(session, tenant_id, berlin(18, 30), 6, now=NOW)
    assert two.say is not None
    assert six.say is not None
    assert "noch Platz" in two.say
    assert "belegt" in six.say


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


def test_a_notice_past_the_last_start_points_to_another_day(session, tenant_id):
    """#527: 240 minutes from 19:00 reach past the last start at 21:30. Asked for
    another time, the guest would hear the same refusal for every time that
    evening, until the ladder escalates."""
    set_notice(session, tenant_id, 240)

    for wish in (berlin(20), berlin(21, 30)):
        slot = check_slot(session, tenant_id, wish, 4, now=berlin(19))

        assert slot.available is False
        assert slot.alternatives == []
        assert slot.say is not None
        assert "noch Platz" in slot.say
        assert slot.say is not None
        assert slot.say.endswith(ASK_OTHER_DAY)
        assert slot.say is not None
        assert ASK_OTHER_TIME not in slot.say


def test_the_one_slot_left_after_the_notice_is_offered(session, tenant_id):
    set_notice(session, tenant_id, 135)

    slot = check_slot(session, tenant_id, berlin(20), 4, now=berlin(19))

    assert slot.alternatives == [berlin(21, 30)]
    assert slot.say is not None
    assert slot.say.endswith("Halb zehn ginge.")
    assert slot.say is not None
    assert ASK_OTHER_DAY not in slot.say


@pytest.mark.parametrize(
    ("minutes", "left", "ending"),
    [(150, [berlin(21, 30)], "Halb zehn ginge."), (151, [], ASK_OTHER_DAY)],
    ids=["on", "past"],
)
def test_a_notice_ending_on_the_last_start_leaves_that_start(
    session, tenant_id, minutes, left, ending
):
    set_notice(session, tenant_id, minutes)

    slot = check_slot(session, tenant_id, berlin(20), 4, now=berlin(19))

    assert slot.alternatives == left
    assert slot.say is not None
    assert slot.say.endswith(ending)


def test_before_a_closed_day_the_guest_is_not_sent_to_tomorrow(session, tenant_id):
    """Sunday evening, Monday closed: the sentence names no day it has not checked."""
    set_notice(session, tenant_id, 240, weekday=SUNDAY)

    slot = check_slot(
        session,
        tenant_id,
        datetime.combine(SONNTAG, time(20), tzinfo=BERLIN),
        4,
        now=datetime.combine(SONNTAG, time(19), tzinfo=BERLIN),
    )

    assert slot.alternatives == []
    assert slot.say is not None
    assert slot.say.endswith(ASK_OTHER_DAY)
    assert slot.say is not None
    assert "morgen" not in slot.say.lower()


def test_lunch_inside_the_notice_still_asks_for_another_time(session, tenant_id):
    """The notice hides the rest of lunch, not the evening: another day is wrong."""
    set_notice(session, tenant_id, 240)

    slot = check_slot(session, tenant_id, berlin(12, 30), 4, now=berlin(11))

    assert slot.alternatives == []
    assert slot.say is not None
    assert slot.say.endswith(ASK_OTHER_TIME)
    assert check_slot(session, tenant_id, berlin(18), 4, now=berlin(11)).available


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
    assert slot.say is not None
    assert "nichts mehr frei" not in slot.say
    assert slot.say is not None
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

    assert slot.say is not None
    assert "nichts mehr frei" in slot.say


def test_a_big_party_inside_the_notice_gets_no_callback(session, tenant_id):
    """The rule of the day says no booking: a callback would promise a table."""
    set_notice(session, tenant_id, 60)

    slot = check_slot(session, tenant_id, berlin(18, 30), 30, now=NOW)

    assert slot.available is False
    assert slot.team_callback is False
    # Nor another time: above the limit that would end with the team (#681).
    assert slot.alternatives == []


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

    assert slot.say is not None
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

    assert refused.value.say is not None
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

    assert refused.value.say is not None
    assert "0 bis 1440 Minuten" in refused.value.say
    session.rollback()
    assert notices.weekday_notices(session, tenant_id)[TUESDAY] == 0


@pytest.mark.parametrize("minutes", [0, notices.MAX_NOTICE_MINUTES])
def test_a_notice_may_stand_on_a_limit(session, tenant_id, minutes):
    set_notice(session, tenant_id, minutes)

    assert notices.weekday_notices(session, tenant_id)[TUESDAY] == minutes


def test_with_the_switch_off_no_table_is_offered(session, tenant_id):
    """T-3.10: no times to try, no callback that would promise one."""
    status_config.set_reservations(session, tenant_id, False)

    slot = check_slot(session, tenant_id, berlin(20), 4, now=NOW)

    assert slot.available is False
    assert slot.alternatives == []
    assert slot.team_callback is False
    assert slot.say == SAY_RESERVATIONS_OFF


def test_the_switch_holds_in_the_middle_of_a_call(engine, session, tenant_id):
    """The session of a call outlives a tap on another tablet."""
    assert check_slot(session, tenant_id, berlin(20), 4, now=NOW).available
    with Session(engine) as tablet:
        status_config.set_reservations(tablet, tenant_id, False)

    assert not check_slot(session, tenant_id, berlin(20), 4, now=NOW).available


def test_a_notice_set_in_the_middle_of_a_call_holds(engine, session, tenant_id):
    assert check_slot(session, tenant_id, berlin(18, 30), 4, now=NOW).available
    with Session(engine) as tablet:
        notices.set_notices(tablet, tenant_id, {TUESDAY: 60})

    assert not check_slot(session, tenant_id, berlin(18, 30), 4, now=NOW).available


def _request(tenant_id, call_id) -> CreateReservationRequest:
    return CreateReservationRequest(
        idempotency_key=uuid.uuid4().hex,
        call_id=call_id,
        tenant_id=tenant_id,
        guest_name="Weber",
        phone="+4972215551234",
        party_size=4,
        reserved_for=berlin(20),
    )


def test_a_draft_is_refused_with_the_switch_off(session, tenant_id, call_id):
    status_config.set_reservations(session, tenant_id, False)

    with pytest.raises(Conflict) as refused:
        create_reservation(session, _request(tenant_id, call_id), now=NOW)

    assert refused.value.say == SAY_RESERVATIONS_OFF


def test_the_switch_after_the_read_back_holds_at_the_yes(
    engine, session, tenant_id, call_id
):
    """The team switched reservations off between the read-back and the yes."""
    draft = create_reservation(session, _request(tenant_id, call_id), now=NOW)
    with Session(engine) as tablet:
        status_config.set_reservations(tablet, tenant_id, False)

    request = ConfirmRequest(
        idempotency_key="yes",
        call_id=call_id,
        tenant_id=tenant_id,
        entity="reservation",
        entity_id=draft.reservation_id,
    )
    with pytest.raises(Conflict) as refused:
        confirm(session, request, NOW)

    assert refused.value.say == SAY_RESERVATIONS_OFF
    session.rollback()
    assert session.get(Reservation, draft.reservation_id).status == "draft"


def test_a_table_is_booked_again_with_the_switch_on(session, tenant_id):
    status_config.set_reservations(session, tenant_id, False)
    status_config.set_reservations(session, tenant_id, True)

    assert check_slot(session, tenant_id, berlin(20), 4, now=NOW).available


def test_the_yes_waits_for_a_switch_that_is_being_set(
    engine, session, tenant_id, call_id
):
    """Codex #515: a plain read does not wait for the tablet's open transaction, and
    the table was booked after the tablet had switched reservations off."""
    draft = create_reservation(session, _request(tenant_id, call_id), now=NOW)
    session.commit()
    outcome: list[str] = []

    def yes() -> None:
        with Session(engine) as call:
            request = ConfirmRequest(
                idempotency_key="yes",
                call_id=call_id,
                tenant_id=tenant_id,
                entity="reservation",
                entity_id=draft.reservation_id,
            )
            try:
                confirm(call, request, NOW)
                outcome.append("booked")
            except Conflict:
                outcome.append("refused")

    with Session(engine) as tablet:
        config = tablet.scalars(
            select(ServiceConfig)
            .where(ServiceConfig.tenant_id == tenant_id)
            .with_for_update()
        ).one()
        config.reservations_enabled = False
        tablet.flush()
        thread = threading.Thread(target=yes)
        thread.start()
        thread.join(timeout=1)
        tablet.commit()
    thread.join(timeout=10)

    assert outcome == ["refused"]
