"""A short slip on the print bridge for every table the agent books (#863 C)."""

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import create_engine, func, select, update
from sqlalchemy.orm import Session

from api.config import settings
from api.domain.confirm import confirm, confirm_tablet
from api.domain.ordering.handover import (
    LEASE_SECONDS,
    NOT_BOOKED,
    PICKUP_TIMEOUT_SECONDS,
    ack_ticket,
    claim_tickets,
    sweep,
)
from api.events.dispatcher import MAX_ATTEMPTS
from api.events.types import ORDER_CONFIRMED, ORDER_HANDOVER_FAILED
from api.models import AuditLog, Call, Order, OutboxEvent, Reservation
from api.tests.test_domain_confirm import DIENSTAG, NOW, berlin, make_draft, request
from api.tests.test_domain_tablet_reservation import enter
from printbridge.escpos import render
from scripts.seed import seed


@pytest.fixture
def session(migrated_db_url):
    engine = create_engine(migrated_db_url)
    with Session(engine) as s:
        yield s
    engine.dispose()


@pytest.fixture
def tenant_id(session, monkeypatch) -> uuid.UUID:
    tid = uuid.UUID(
        seed(session, tenant_name="Testbetrieb", timezone="Europe/Berlin").tenant_id
    )
    monkeypatch.setattr(settings, "kitchen_bridge_tenant_id", str(tid))
    return tid


def _call(session, tenant_id) -> uuid.UUID:
    call = Call(
        tenant_id=tenant_id,
        external_session_id=uuid.uuid4().hex,
        started_at=NOW,
        delete_after=DIENSTAG,
    )
    session.add(call)
    session.commit()
    return call.id


def _booked(session, tenant_id, **overrides) -> uuid.UUID:
    call_id = _call(session, tenant_id)
    draft_id = make_draft(session, tenant_id, call_id, **overrides)
    confirm(session, request(tenant_id, call_id, draft_id), now=NOW)
    # The queue stamps the database's clock; the test runs on NOW.
    session.execute(
        update(OutboxEvent)
        .where(OutboxEvent.event_type == ORDER_CONFIRMED)
        .values(next_attempt_at=NOW)
    )
    session.commit()
    return draft_id


def _slips(session) -> list[OutboxEvent]:
    return list(
        session.scalars(
            select(OutboxEvent)
            .where(OutboxEvent.event_type == ORDER_CONFIRMED)
            .execution_options(populate_existing=True)
        )
    )


def _alarms(session) -> int:
    return session.scalar(
        select(func.count())
        .select_from(OutboxEvent)
        .where(OutboxEvent.event_type == ORDER_HANDOVER_FAILED)
    )


def test_a_table_the_agent_books_prints_a_short_slip(session, tenant_id):
    draft_id = _booked(session, tenant_id)

    [ticket] = claim_tickets(session, tenant_id, NOW + timedelta(seconds=1))

    slip = ticket["ticket"]
    assert slip["type"] == "reservation"
    assert slip["order_id"] == str(draft_id)
    assert slip["customer_name"] == "Müller"
    assert slip["party_size"] == 4
    assert slip["reserved_day"] == "Dienstag 15.09."
    assert slip["reserved_time"] == "18:30"
    assert slip["note"] == "Kinderstuhl"
    # What the bridge reads and nothing more: no phone number on the way.
    assert "phone" not in slip and "reservation_id" not in slip
    # The cold path still gets its own event for n8n.
    types = sorted(e.event_type for e in session.scalars(select(OutboxEvent)))
    assert types == [ORDER_CONFIRMED, "reservation.confirmed"]


def test_the_slip_turns_no_card_and_raises_no_alarm(session, tenant_id):
    _booked(session, tenant_id)
    later = NOW + timedelta(seconds=PICKUP_TIMEOUT_SECONDS + 5)
    # Never picked up: the watchdog leaves it alone.
    assert sweep(session, later) == 0
    [ticket] = claim_tickets(session, tenant_id, later)
    ack_ticket(session, tenant_id, uuid.UUID(ticket["id"]), False, later, "Papier")

    assert _alarms(session) == 0
    assert session.scalar(select(func.count()).select_from(Order)) == 0
    assert not session.scalars(select(AuditLog).where(AuditLog.entity == "order")).all()

    [ticket] = claim_tickets(session, tenant_id, later + timedelta(minutes=5))
    assert (
        ack_ticket(
            session,
            tenant_id,
            uuid.UUID(ticket["id"]),
            True,
            later + timedelta(minutes=5),
        )
        == "sent"
    )
    assert _alarms(session) == 0


def test_a_table_the_team_enters_prints_nothing(session, tenant_id):
    draft = enter(session, tenant_id, reserved_for=berlin(DIENSTAG, 19))
    confirm_tablet(session, tenant_id, draft.reservation_id, override=True, now=NOW)

    assert session.get(Reservation, draft.reservation_id).status == "confirmed"
    assert _slips(session) == []


def test_no_slip_without_a_print_bridge(session, tenant_id, monkeypatch):
    monkeypatch.setattr(settings, "kitchen_bridge_tenant_id", "")
    _booked(session, tenant_id)

    assert _slips(session) == []


def test_a_self_tests_table_prints_nothing(session, tenant_id):
    call_id = _call(session, tenant_id)
    draft_id = make_draft(session, tenant_id, call_id)
    session.execute(
        update(Reservation).where(Reservation.id == draft_id).values(is_test=True)
    )
    session.commit()
    confirm(session, request(tenant_id, call_id, draft_id), now=NOW)

    assert _slips(session) == []


def test_a_cancelled_table_is_not_printed(session, tenant_id):
    draft_id = _booked(session, tenant_id)
    session.execute(
        update(Reservation).where(Reservation.id == draft_id).values(status="cancelled")
    )
    session.commit()

    assert claim_tickets(session, tenant_id, NOW + timedelta(seconds=1)) == []
    [slip] = _slips(session)
    assert (slip.status, slip.last_error) == ("sent", NOT_BOOKED)


def test_a_slip_waiting_for_a_silent_bridge_ends_with_its_table(session, tenant_id):
    """Deleted or past, the watchdog takes the slip back: it does not stay
    queued with the guest's name, and a later poll prints nothing old."""
    gone = _booked(session, tenant_id)
    past = _booked(session, tenant_id, reserved_for=berlin(DIENSTAG, 12))
    ahead = _booked(session, tenant_id, reserved_for=berlin(DIENSTAG, 20))
    session.execute(
        update(Reservation).where(Reservation.id == gone).values(deleted_at=NOW)
    )
    session.commit()

    sweep(session, berlin(DIENSTAG, 12, 30))

    state = {
        s.payload["reservation_id"]: (s.status, s.last_error) for s in _slips(session)
    }
    assert state[str(gone)] == ("sent", NOT_BOOKED)
    assert state[str(past)] == ("sent", NOT_BOOKED)
    assert state[str(ahead)] == ("pending", None)
    assert _alarms(session) == 0


def test_a_slip_whose_pickups_got_no_answer_fails_without_an_alarm(session, tenant_id):
    _booked(session, tenant_id)
    at = NOW + timedelta(seconds=1)
    for _ in range(MAX_ATTEMPTS):
        assert len(claim_tickets(session, tenant_id, at)) == 1
        at += timedelta(seconds=LEASE_SECONDS + 1)
    assert claim_tickets(session, tenant_id, at) == []

    sweep(session, at + timedelta(seconds=PICKUP_TIMEOUT_SECONDS))

    [slip] = _slips(session)
    assert slip.status == "failed"
    assert _alarms(session) == 0


def test_the_reservation_slip_layout():
    text = render(
        {
            "order_id": str(uuid.uuid4()),
            "type": "reservation",
            "customer_name": "Müller",
            "party_size": 4,
            "reserved_day": "Dienstag 15.09.",
            "reserved_time": "18:30",
            "note": "Kinderstuhl",
            "revision": 0,
            "items": [],
            "print_time": "08:01",
        }
    ).decode("cp858")

    assert "RESERVIERUNG" in text
    assert "Dienstag 15.09. 18:30" in text
    assert "4 Personen" in text
    assert "Name: Müller" in text
    assert "! Kinderstuhl" in text
    assert "Gedruckt 08:01" in text
    # No order: nothing for the register, no sum, no pickup banner.
    assert "Summe" not in text and "NICHT IN KASSE" not in text
    assert "ABHOLUNG" not in text
