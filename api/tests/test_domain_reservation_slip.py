"""A short slip on the print bridge for every table the agent books (#863 C)."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select, update
from sqlalchemy.orm import Session

from api.config import settings
from api.db import get_db
from api.domain.confirm import confirm, confirm_tablet
from api.domain.ordering.handover import (
    LEASE_SECONDS,
    NOT_BOOKED,
    PICKUP_TIMEOUT_SECONDS,
    UNANSWERED,
    ack_ticket,
    claim_tickets,
    sweep,
)
from api.domain.ordering.selftest_slip import TEST_SLIP_NAME, queue_selftest_slip
from api.events.dispatcher import MAX_ATTEMPTS
from api.events.types import ORDER_CONFIRMED, ORDER_HANDOVER_FAILED
from api.main import app
from api.models import AuditLog, Call, Order, OutboxEvent, Reservation
from api.tests.test_domain_confirm import DIENSTAG, NOW, berlin, make_draft, request
from api.tests.test_domain_tablet_reservation import enter
from evals.judge import _duplicate_confirms
from printbridge.client import Server
from printbridge.escpos import render
from scripts.seed import seed

TOKEN = "bruecke-geheim"


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


def _claim(session, tenant_id, at, limit=5):
    """A pickup by a bridge that prints the reservation layout."""
    return claim_tickets(session, tenant_id, at, limit, reservation_slips=True)


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


def _cancel(session, reservation_id) -> None:
    session.execute(
        update(Reservation)
        .where(Reservation.id == reservation_id)
        .values(status="cancelled")
    )
    session.commit()


def test_a_table_the_agent_books_prints_a_short_slip(session, tenant_id):
    draft_id = _booked(session, tenant_id)

    [ticket] = _claim(session, tenant_id, NOW + timedelta(seconds=1))

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


def test_a_bridge_without_the_layout_never_gets_a_slip(session, tenant_id):
    """An older bridge would print the slip as an order of 0,00 EUR."""
    _booked(session, tenant_id)

    assert claim_tickets(session, tenant_id, NOW + timedelta(seconds=1)) == []
    [slip] = _slips(session)
    assert (slip.status, slip.attempts) == ("pending", 0)


def test_orders_go_out_before_reservation_slips(session, tenant_id):
    """A backlog of slips must not keep an order waiting until its card turns red."""
    _booked(session, tenant_id)
    queue_selftest_slip(session, tenant_id, NOW)

    [first] = _claim(session, tenant_id, NOW + timedelta(seconds=1), limit=1)
    [second] = _claim(session, tenant_id, NOW + timedelta(seconds=2), limit=1)

    assert first["ticket"]["customer_name"] == TEST_SLIP_NAME
    assert second["ticket"]["type"] == "reservation"


def test_the_slip_turns_no_card_and_raises_no_alarm(session, tenant_id, caplog):
    _booked(session, tenant_id)
    later = NOW + timedelta(seconds=PICKUP_TIMEOUT_SECONDS + 5)
    # Never picked up: the watchdog leaves it alone.
    assert sweep(session, later) == 0
    # Every pickup ends in a print error, the last one for good.
    for _ in range(MAX_ATTEMPTS):
        [ticket] = _claim(session, tenant_id, later)
        ack_ticket(session, tenant_id, uuid.UUID(ticket["id"]), False, later, "Papier")
        session.execute(update(OutboxEvent).values(next_attempt_at=later))
        session.commit()

    [slip] = _slips(session)
    assert slip.status == "failed"
    assert _alarms(session) == 0
    assert "Alarm" not in caplog.text
    assert session.scalar(select(func.count()).select_from(Order)) == 0
    assert not session.scalars(select(AuditLog).where(AuditLog.entity == "order")).all()


def test_a_printed_slip_is_done(session, tenant_id):
    _booked(session, tenant_id)
    at = NOW + timedelta(seconds=1)
    [ticket] = _claim(session, tenant_id, at)

    assert ack_ticket(session, tenant_id, uuid.UUID(ticket["id"]), True, at) == "sent"
    assert _alarms(session) == 0
    assert _claim(session, tenant_id, at + timedelta(minutes=5)) == []


def test_a_table_the_team_enters_prints_nothing(session, tenant_id):
    draft = enter(session, tenant_id, reserved_for=berlin(DIENSTAG, 19))
    confirm_tablet(session, tenant_id, draft.reservation_id, override=True, now=NOW)

    assert session.get(Reservation, draft.reservation_id).status == "confirmed"
    assert _slips(session) == []


def test_no_slip_without_a_print_bridge(session, tenant_id, monkeypatch):
    monkeypatch.setattr(settings, "kitchen_bridge_tenant_id", "")
    draft_id = _booked(session, tenant_id)

    assert session.get(Reservation, draft_id).status == "confirmed"
    assert _slips(session) == []


def test_a_self_tests_table_prints_nothing(session, tenant_id):
    call_id = _call(session, tenant_id)
    draft_id = make_draft(session, tenant_id, call_id)
    session.execute(
        update(Reservation).where(Reservation.id == draft_id).values(is_test=True)
    )
    session.commit()
    confirm(session, request(tenant_id, call_id, draft_id), now=NOW)

    assert session.get(Reservation, draft_id).status == "confirmed"
    assert _slips(session) == []


def test_a_cancelled_table_is_not_printed(session, tenant_id):
    draft_id = _booked(session, tenant_id)
    _cancel(session, draft_id)

    assert _claim(session, tenant_id, NOW + timedelta(seconds=1)) == []
    [slip] = _slips(session)
    assert (slip.status, slip.last_error) == ("sent", f"not printed: {NOT_BOOKED}")


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
    taken_back = ("sent", f"not printed: {NOT_BOOKED}")
    assert state[str(gone)] == taken_back
    assert state[str(past)] == taken_back
    assert state[str(ahead)] == ("pending", None)
    assert _alarms(session) == 0


def test_a_slip_picked_up_without_an_answer_may_lie_at_the_printer(session, tenant_id):
    draft_id = _booked(session, tenant_id)
    at = NOW + timedelta(seconds=1)
    assert len(_claim(session, tenant_id, at)) == 1
    _cancel(session, draft_id)

    sweep(session, at + timedelta(seconds=LEASE_SECONDS + 1))

    [slip] = _slips(session)
    assert (slip.status, slip.last_error) == ("sent", f"{UNANSWERED}{NOT_BOOKED}")


def test_a_slip_whose_pickups_got_no_answer_fails_without_an_alarm(session, tenant_id):
    _booked(session, tenant_id)
    at = NOW + timedelta(seconds=1)
    for _ in range(MAX_ATTEMPTS):
        assert len(_claim(session, tenant_id, at)) == 1
        at += timedelta(seconds=LEASE_SECONDS + 1)
    assert _claim(session, tenant_id, at) == []

    sweep(session, at + timedelta(seconds=PICKUP_TIMEOUT_SECONDS))

    [slip] = _slips(session)
    assert slip.status == "failed"
    assert _alarms(session) == 0


def test_the_eval_judge_counts_a_booked_table_once(session, tenant_id):
    draft_id = _booked(session, tenant_id)

    assert _duplicate_confirms(session, [], [session.get(Reservation, draft_id)]) == 0


@pytest.fixture
def http(migrated_db_url, monkeypatch, tenant_id):
    engine = create_engine(migrated_db_url)

    def override_get_db():
        with Session(engine) as s:
            yield s

    app.dependency_overrides[get_db] = override_get_db
    monkeypatch.setattr(settings, "kitchen_bridge_token", TOKEN)
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def test_only_a_bridge_that_names_the_layout_gets_the_slip(http, session, tenant_id):
    draft_id = _booked(session, tenant_id)
    # Over HTTP the server reads its own clock: the table must lie ahead of it.
    session.execute(
        update(Reservation)
        .where(Reservation.id == draft_id)
        .values(reserved_for=datetime.now(UTC) + timedelta(days=1))
    )
    session.commit()
    auth = {"Authorization": f"Bearer {TOKEN}"}
    claim = "/v1/kitchen/claim"
    old = {"tenant_id": str(tenant_id)}
    new = {**old, "layouts": ["reservation"]}

    assert http.post(claim, json=old, headers=auth).json()["data"]["tickets"] == []
    [ticket] = http.post(claim, json=new, headers=auth).json()["data"]["tickets"]
    assert ticket["ticket"]["order_id"] == str(draft_id)
    # The list is bounded: a bridge names a handful of layouts, never more.
    many = {**old, "layouts": ["x"] * 9}
    refused = http.post(claim, json=many, headers=auth).json()
    assert refused["ok"] is False
    assert refused["error"]["code"] == "invalid_input"


def test_the_bridge_names_the_layouts_it_prints(monkeypatch):
    sent: list[tuple[str, dict]] = []

    def post(path: str, body: dict) -> dict:
        sent.append((path, body))
        return {"tickets": []}

    bridge = Server("http://127.0.0.1:9", "token", str(uuid.uuid4()))
    monkeypatch.setattr(bridge, "_post", post)

    assert bridge.claim() == []
    assert sent == [("/v1/kitchen/claim", {"limit": 5, "layouts": ["reservation"]})]


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
