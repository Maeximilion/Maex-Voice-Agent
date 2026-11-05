"""Reservations the team enters on the tablet (T-3.12, D30): a draft without a
call, confirmed by the team's tap, counted against the agent's seats (D28),
seated on a table or a combination of the floor plan."""

import uuid
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from api.core.errors import Conflict, InvalidInput, NotFound
from api.domain.confirm import confirm, confirm_tablet
from api.domain.reservations import (
    check_slot,
    create_reservation,
    create_tablet_reservation,
    list_today,
)
from api.domain.reservations.manual import WindowFull
from api.models import (
    AuditLog,
    Call,
    DiningTable,
    FloorArea,
    OutboxEvent,
    Reservation,
    ReservationTable,
    ServiceConfig,
    TableCombination,
)
from api.schemas.confirm import ConfirmRequest
from api.schemas.reservations import CreateReservationRequest, TabletReservationRequest
from api.tests.conftest import lift_party_limits
from scripts.seed import seed

BERLIN = ZoneInfo("Europe/Berlin")
# A Tuesday; the seed gives it 30 seats from 11:30 to 14:00 and 40 from 17:00 to 22:00.
DIENSTAG = date(2026, 9, 15)
NOW = datetime(2026, 9, 15, 8, 0, tzinfo=BERLIN)


def berlin(hh: int, mm: int = 0, day: date = DIENSTAG) -> datetime:
    return datetime.combine(day, time(hh, mm), tzinfo=BERLIN)


@pytest.fixture
def session(migrated_db_url):
    engine = create_engine(migrated_db_url)
    with Session(engine) as s:
        yield s
    engine.dispose()


@pytest.fixture
def tenant_id(session) -> uuid.UUID:
    tenant = uuid.UUID(
        seed(session, tenant_name="Testbetrieb", timezone="Europe/Berlin").tenant_id
    )
    lift_party_limits(session, tenant)
    return tenant


@pytest.fixture
def tables(session, tenant_id) -> dict[str, uuid.UUID]:
    """T1 and T2 combine, T3 stands alone."""
    area = FloorArea(tenant_id=tenant_id, name="Gastraum", width_cm=1000, depth_cm=700)
    session.add(area)
    session.flush()
    rows = {
        name: DiningTable(
            tenant_id=tenant_id,
            area_id=area.id,
            name=name,
            seats=4,
            width_cm=120,
            depth_cm=80,
            x_cm=200 + 200 * i,
            y_cm=200,
        )
        for i, name in enumerate(("T1", "T2", "T3"))
    }
    session.add_all(rows.values())
    session.flush()
    low, high = sorted([rows["T1"].id, rows["T2"].id])
    session.add(TableCombination(tenant_id=tenant_id, table_a_id=low, table_b_id=high))
    session.commit()
    return {name: row.id for name, row in rows.items()}


def enter(session, tenant_id, at=None, **overrides):
    body = {
        "tenant_id": tenant_id,
        "idempotency_key": uuid.uuid4().hex,
        "guest_name": "Freundin von Anna",
        "party_size": 4,
        "reserved_for": berlin(18, 30),
        **overrides,
    }
    return create_tablet_reservation(
        session, TabletReservationRequest(**body), now=at or NOW
    )


def booked(session, tenant_id, **overrides):
    """A confirmed table the team entered."""
    draft = enter(session, tenant_id, **overrides)
    confirm_tablet(session, tenant_id, draft.reservation_id, override=True, now=NOW)
    return draft


def agent_table(session, tenant_id, party_size, at):
    call = Call(
        tenant_id=tenant_id,
        external_session_id=uuid.uuid4().hex,
        started_at=NOW,
        delete_after=DIENSTAG,
    )
    session.add(call)
    session.commit()
    draft = create_reservation(
        session,
        CreateReservationRequest(
            call_id=call.id,
            tenant_id=tenant_id,
            idempotency_key=uuid.uuid4().hex,
            guest_name="Müller",
            phone="+4972215551234",
            party_size=party_size,
            reserved_for=at,
        ),
        now=NOW,
    )
    return call.id, draft.reservation_id


def test_the_team_enters_and_confirms_a_table_without_a_call(
    session, tenant_id, tables
):
    draft = enter(session, tenant_id, phone="07221 5551234", table_ids=[tables["T1"]])

    assert draft.status == "draft"
    assert draft.tables == ["T1"]
    assert draft.phone == "+4972215551234"
    assert not draft.needs_override
    assert (draft.booked_guests, draft.max_guests) == (0, 40)
    row = session.get(Reservation, draft.reservation_id)
    assert (row.source, row.call_id) == ("tablet", None)
    # A draft is no booking: the tablet's day list shows confirmed tables only.
    assert list_today(session, tenant_id, "Europe/Berlin", now=NOW) == []

    result = confirm_tablet(session, tenant_id, draft.reservation_id, now=NOW)

    assert result.status == "confirmed"
    assert session.get(Reservation, draft.reservation_id).status == "confirmed"
    event = session.scalars(select(OutboxEvent)).one()
    assert event.payload["call_id"] is None
    assert event.payload["source"] == "tablet"
    audit = session.scalars(select(AuditLog).order_by(AuditLog.id)).all()
    assert [(a.actor, a.action) for a in audit] == [
        ("gui:tablet", "reservation.draft_created"),
        ("gui:tablet", "reservation.confirmed"),
    ]
    assert audit[0].payload["tables"] == ["T1"]
    assert audit[1].payload == {"source": "tablet", "override": False}
    assert [
        r.reservation_id
        for r in list_today(session, tenant_id, "Europe/Berlin", now=NOW)
    ] == [draft.reservation_id]


def test_same_key_same_draft_and_a_second_tap_books_nothing_twice(session, tenant_id):
    key = uuid.uuid4().hex
    first = enter(session, tenant_id, idempotency_key=key)
    again = enter(session, tenant_id, idempotency_key=key, party_size=9)

    assert again.reservation_id == first.reservation_id
    assert again.party_size == 4
    confirm_tablet(session, tenant_id, first.reservation_id, now=NOW)
    confirm_tablet(session, tenant_id, first.reservation_id, now=NOW)
    assert len(session.scalars(select(OutboxEvent)).all()) == 1


def test_an_agents_key_is_not_replayed_as_a_tablet_draft(session, tenant_id):
    _, agent_id = agent_table(session, tenant_id, 2, berlin(19))
    key = session.get(Reservation, agent_id).idempotency_key

    with pytest.raises(Conflict):
        enter(session, tenant_id, idempotency_key=key)


def test_a_confirmed_tablet_table_counts_against_the_agents_seats(session, tenant_id):
    booked(session, tenant_id, party_size=38)

    slot = check_slot(session, tenant_id, berlin(19), 4, now=NOW)

    assert not slot.available


def test_a_tablet_draft_holds_no_seats(session, tenant_id):
    enter(session, tenant_id, party_size=40)

    assert check_slot(session, tenant_id, berlin(19), 4, now=NOW).available


def test_a_full_window_is_booked_only_on_the_teams_word(session, tenant_id):
    agent_call, agent_id = agent_table(session, tenant_id, 38, berlin(18))
    confirm(
        session,
        ConfirmRequest(
            call_id=agent_call,
            tenant_id=tenant_id,
            entity="reservation",
            entity_id=agent_id,
            idempotency_key=uuid.uuid4().hex,
        ),
        now=NOW,
    )
    draft = enter(session, tenant_id)
    assert draft.needs_override
    assert (draft.booked_guests, draft.max_guests) == (38, 40)

    with pytest.raises(WindowFull) as refused:
        confirm_tablet(session, tenant_id, draft.reservation_id, now=NOW)
    assert refused.value.say == (
        "Das Fenster 17:00 bis 22:00 Uhr hat 38 von 40 Plätzen belegt."
    )
    assert session.get(Reservation, draft.reservation_id).status == "draft"

    confirm_tablet(session, tenant_id, draft.reservation_id, override=True, now=NOW)
    assert session.get(Reservation, draft.reservation_id).status == "confirmed"
    last = session.scalars(select(AuditLog).order_by(AuditLog.id.desc())).first()
    assert last.payload == {"source": "tablet", "override": True}


def test_a_live_agent_draft_fills_the_window_for_the_tablet_too(session, tenant_id):
    agent_table(session, tenant_id, 38, berlin(18))

    draft = enter(session, tenant_id)

    assert draft.needs_override


def test_a_time_outside_every_window_needs_the_teams_word(session, tenant_id):
    draft = enter(session, tenant_id, reserved_for=berlin(15))

    assert draft.needs_override
    assert (draft.window_start, draft.max_guests) == (None, None)
    with pytest.raises(WindowFull) as refused:
        confirm_tablet(session, tenant_id, draft.reservation_id, now=NOW)
    assert refused.value.say == "Zu dieser Zeit gibt es kein Reservierungsfenster."


def test_a_table_held_in_the_same_window_is_refused(session, tenant_id, tables):
    booked(session, tenant_id, reserved_for=berlin(18), table_ids=[tables["T1"]])

    with pytest.raises(Conflict) as refused:
        enter(session, tenant_id, reserved_for=berlin(21), table_ids=[tables["T1"]])
    assert refused.value.say == "Tisch T1 ist um 21:00 Uhr schon vergeben."
    # The same table at lunch, the next window, and another table are free.
    assert enter(session, tenant_id, reserved_for=berlin(12), table_ids=[tables["T1"]])
    assert enter(session, tenant_id, reserved_for=berlin(21), table_ids=[tables["T3"]])


def test_a_combination_collides_with_either_of_its_tables(session, tenant_id, tables):
    booked(session, tenant_id, reserved_for=berlin(18), table_ids=[tables["T2"]])

    with pytest.raises(Conflict) as refused:
        enter(session, tenant_id, table_ids=[tables["T1"], tables["T2"]])
    assert refused.value.say == "Tisch T2 ist um 18:30 Uhr schon vergeben."


def test_two_drafts_on_one_table_the_second_confirmation_is_refused(
    session, tenant_id, tables
):
    first = enter(session, tenant_id, reserved_for=berlin(18), table_ids=[tables["T1"]])
    second = enter(
        session, tenant_id, reserved_for=berlin(20), table_ids=[tables["T1"]]
    )
    confirm_tablet(session, tenant_id, first.reservation_id, now=NOW)

    # Not even on the team's word: two parties cannot sit on one table.
    with pytest.raises(Conflict):
        confirm_tablet(
            session, tenant_id, second.reservation_id, override=True, now=NOW
        )
    assert session.get(Reservation, second.reservation_id).status == "draft"


def test_a_combination_of_the_plan_seats_one_party_on_two_tables(
    session, tenant_id, tables
):
    draft = enter(
        session, tenant_id, party_size=8, table_ids=[tables["T2"], tables["T1"]]
    )

    assert draft.tables == ["T1", "T2"]
    seated = session.scalars(
        select(ReservationTable.table_id).where(
            ReservationTable.reservation_id == draft.reservation_id
        )
    ).all()
    assert set(seated) == {tables["T1"], tables["T2"]}


@pytest.mark.parametrize(
    "pick, error, say",
    [
        (
            ("T1", "T3"),
            InvalidInput,
            "Diese beiden Tische sind im Raumplan nicht kombiniert.",
        ),
        (("T1", "T1"), InvalidInput, "Ein Tisch kann nur einmal gewählt werden."),
    ],
)
def test_the_seating_must_be_a_table_or_a_pair_of_the_plan(
    session, tenant_id, tables, pick, error, say
):
    with pytest.raises(error) as refused:
        enter(session, tenant_id, table_ids=[tables[name] for name in pick])
    assert refused.value.say == say
    assert session.scalars(select(Reservation)).all() == []


def test_a_removed_or_foreign_table_is_refused(session, tenant_id, tables):
    session.get(DiningTable, tables["T3"]).deleted_at = NOW
    session.commit()

    for table_id in (tables["T3"], uuid.uuid4()):
        with pytest.raises(NotFound):
            enter(session, tenant_id, table_ids=[table_id])


def test_a_table_removed_between_the_taps_stops_the_confirmation(
    session, tenant_id, tables
):
    draft = enter(session, tenant_id, table_ids=[tables["T3"]])
    session.get(DiningTable, tables["T3"]).deleted_at = NOW
    session.commit()

    with pytest.raises(Conflict) as refused:
        confirm_tablet(session, tenant_id, draft.reservation_id, now=NOW)
    assert refused.value.say == "Tisch T3 ist nicht mehr im Raumplan."


def test_the_tablet_and_the_agent_never_confirm_each_others_drafts(session, tenant_id):
    call, agent_id = agent_table(session, tenant_id, 2, berlin(19))
    tablet = enter(session, tenant_id)

    with pytest.raises(NotFound):
        confirm_tablet(session, tenant_id, agent_id, now=NOW)
    with pytest.raises(NotFound):
        confirm(
            session,
            ConfirmRequest(
                call_id=call,
                tenant_id=tenant_id,
                entity="reservation",
                entity_id=tablet.reservation_id,
                idempotency_key=uuid.uuid4().hex,
            ),
            now=NOW,
        )
    assert session.get(Reservation, agent_id).status == "draft"
    assert session.get(Reservation, tablet.reservation_id).status == "draft"


def test_a_cancelled_tablet_reservation_stays_cancelled(session, tenant_id):
    draft = enter(session, tenant_id)
    session.get(Reservation, draft.reservation_id).status = "cancelled"
    session.commit()

    with pytest.raises(Conflict):
        confirm_tablet(session, tenant_id, draft.reservation_id, now=NOW)


@pytest.mark.parametrize(
    "overrides",
    [
        {"reserved_for": berlin(7)},
        {"guest_name": "   "},
        {"phone": "keine Nummer"},
    ],
)
def test_what_the_team_must_get_right(session, tenant_id, overrides):
    with pytest.raises(InvalidInput):
        enter(session, tenant_id, **overrides)
    assert session.scalars(select(Reservation)).all() == []


def test_the_agents_limits_do_not_stop_the_team(session, tenant_id):
    """Party size, notice and the reservation switch are the agent's rules."""
    row = session.get(ServiceConfig, tenant_id)
    row.reservation_max_party_size = 6
    row.reservation_max_party_size_same_day = 6
    row.reservations_enabled = False
    session.commit()

    draft = enter(session, tenant_id, party_size=12, reserved_for=berlin(17))
    confirm_tablet(session, tenant_id, draft.reservation_id, now=NOW)

    assert session.get(Reservation, draft.reservation_id).status == "confirmed"
