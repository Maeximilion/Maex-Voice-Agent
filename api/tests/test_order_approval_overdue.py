"""An order that waits too long for "Passt" turns red on the tablet (#252).

Maxi, 13.10.2026: after 5 minutes. The number is the restaurant's,
`service_config.order_approval_overdue_minutes` (migration 008). An order
nobody approves is cooked by nobody, and until now its card looked the same
after an hour as after a second.
"""

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, update
from sqlalchemy.orm import Session

from api.core.time import to_local, utcnow
from api.db import get_db
from api.domain.ordering.board import (
    approve_order,
    list_new,
    new_orders_change_token,
)
from api.main import app
from api.models import AuditLog, Order, ServiceConfig
from api.tests.test_domain_confirm_order import _confirm, _draft, _mode
from api.tests.test_domain_draft_order import NOW, _call, _tenant

TZ = "Europe/Berlin"
GUI = Path(__file__).resolve().parents[1] / "gui"
STATIC, TEMPLATES = GUI / "static", GUI / "templates"
# The moment the guest said yes, in every test that does not move it.
YES = datetime(2026, 9, 15, 17, 0, tzinfo=UTC)


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
    """The agent takes calls, and every order waits for the team (overflow)."""
    tid = _tenant(session, "Testbetrieb")
    _mode(session, tid, "overflow")
    return tid


def _order(session, tenant_id, *, yes: datetime = YES) -> uuid.UUID:
    """A confirmed order of its own call whose guest said yes at `yes`."""
    call_id = _call(session, tenant_id)
    order_id = _draft(session, tenant_id, call_id, now=NOW)
    _confirm(session, tenant_id, call_id, order_id)
    session.execute(
        update(AuditLog)
        .where(AuditLog.entity_id == order_id, AuditLog.action == "order.confirmed")
        .values(at=yes)
    )
    session.commit()
    return order_id


def _minutes(session, tenant_id, minutes: int) -> None:
    session.execute(
        update(ServiceConfig)
        .where(ServiceConfig.tenant_id == tenant_id)
        .values(order_approval_overdue_minutes=minutes)
    )
    session.commit()


def _card(session, tenant_id, order_id, after: timedelta):
    cards = list_new(session, tenant_id, TZ, YES + after)
    return next(c for c in cards if c.order_id == order_id)


def test_a_waiting_order_is_overdue_from_the_fifth_minute(session, tenant_id):
    order_id = _order(session, tenant_id)

    before = _card(session, tenant_id, order_id, timedelta(minutes=4, seconds=59))
    at = _card(session, tenant_id, order_id, timedelta(minutes=5))

    assert before.overdue is False and at.overdue is True
    assert at.waiting_since == YES


def test_the_wait_counts_from_the_guests_yes_not_from_the_draft(session, tenant_id):
    """A long call: the draft is half an hour old when the guest confirms. The
    team has had one minute to look, not thirty-one."""
    order_id = _order(session, tenant_id)
    session.execute(
        update(Order)
        .where(Order.id == order_id)
        .values(created_at=YES - timedelta(minutes=30))
    )
    session.commit()

    assert _card(session, tenant_id, order_id, timedelta(minutes=1)).overdue is False


def test_the_minutes_are_the_restaurants(session, tenant_id):
    order_id = _order(session, tenant_id)
    _minutes(session, tenant_id, 1)

    assert _card(session, tenant_id, order_id, timedelta(minutes=1)).overdue is True

    _minutes(session, tenant_id, 30)

    assert _card(session, tenant_id, order_id, timedelta(minutes=29)).overdue is False


def test_an_order_above_the_limit_in_the_direct_mode_turns_overdue_too(
    session, tenant_id
):
    _mode(session, tenant_id, "primary")
    session.execute(
        update(ServiceConfig)
        .where(ServiceConfig.tenant_id == tenant_id)
        .values(order_approval_above_cents=0)
    )
    session.commit()
    order_id = _order(session, tenant_id)

    assert _card(session, tenant_id, order_id, timedelta(minutes=6)).overdue is True


def test_an_order_the_kitchen_has_is_never_overdue(session, tenant_id):
    _mode(session, tenant_id, "primary")
    sent = _order(session, tenant_id)
    _mode(session, tenant_id, "overflow")
    approved = _order(session, tenant_id)
    approve_order(session, tenant_id, approved)
    failed = _order(session, tenant_id)
    session.execute(
        update(Order).where(Order.id == failed).values(handover_state="failed")
    )
    session.commit()

    cards = {c.order_id: c for c in list_new(session, tenant_id, TZ, YES)}
    late = {
        c.order_id: c
        for c in list_new(session, tenant_id, TZ, YES + timedelta(hours=3))
    }

    assert sent in cards and failed in cards
    assert all(c.overdue is False for c in late.values())
    assert late[failed].waiting_since is None


def test_overdue_cards_come_after_the_red_ones_and_before_the_rest(session, tenant_id):
    fresh = _order(session, tenant_id, yes=YES + timedelta(minutes=9))
    overdue = _order(session, tenant_id, yes=YES)
    failed = _order(session, tenant_id, yes=YES + timedelta(minutes=9))
    session.execute(
        update(Order).where(Order.id == failed).values(handover_state="failed")
    )
    # By age alone the fresh one would come first: its draft is the oldest.
    for order_id, minutes in ((fresh, 3), (overdue, 2), (failed, 1)):
        session.execute(
            update(Order)
            .where(Order.id == order_id)
            .values(created_at=YES - timedelta(minutes=minutes))
        )
    session.commit()

    cards = list_new(session, tenant_id, TZ, YES + timedelta(minutes=10))

    assert [c.order_id for c in cards] == [failed, overdue, fresh]


def test_the_tablet_is_told_when_an_order_becomes_overdue(session, tenant_id):
    """Nothing in the database changes at that moment, only the clock: the
    change token of the column has to move with it, or no tablet redraws."""
    _order(session, tenant_id)

    early = new_orders_change_token(session, tenant_id, TZ, YES + timedelta(minutes=4))
    still = new_orders_change_token(
        session, tenant_id, TZ, YES + timedelta(minutes=4, seconds=30)
    )
    late = new_orders_change_token(session, tenant_id, TZ, YES + timedelta(minutes=5))

    assert early == still and late != early


def test_an_order_without_its_audit_entry_counts_from_its_creation(session, tenant_id):
    """Rule 5 for the tablet: an order that waits is never left yellow for
    ever because a row is missing."""
    order_id = _order(session, tenant_id)
    session.execute(
        update(AuditLog)
        .where(AuditLog.entity_id == order_id, AuditLog.action == "order.confirmed")
        .values(action="order.confirmed.lost")
    )
    session.execute(
        update(Order)
        .where(Order.id == order_id)
        .values(created_at=YES - timedelta(minutes=2))
    )
    session.commit()

    card = _card(session, tenant_id, order_id, timedelta(minutes=3))

    assert card.overdue is True and card.waiting_since == YES - timedelta(minutes=2)


# -- the card on the tablet -----------------------------------------------------


@pytest.fixture
def client(engine):
    def override_get_db():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_the_card_of_an_overdue_order_is_red_and_says_since_when(
    client, session, tenant_id
):
    waited = utcnow() - timedelta(minutes=10)
    _order(session, tenant_id, yes=waited)

    body = client.get("/gui/fragments/bestellungen").text

    assert 'class="card order overdue"' in body and 'data-overdue="1"' in body
    since = to_local(waited, TZ).strftime("%H:%M")
    assert f"Wartet seit {since} auf Freigabe" in body
    assert "Noch nicht an der Kasse" not in body
    # Still the same button: the way out is "Passt".
    assert "Passt, Zettel an die Kasse" in body


def test_the_card_of_an_order_that_just_came_in_stays_yellow(
    client, session, tenant_id
):
    order_id = _order(session, tenant_id, yes=utcnow())
    # Created today too: an order of an earlier business day nobody typed into
    # the register is red for that alone (T-11.6).
    session.execute(
        update(Order).where(Order.id == order_id).values(created_at=utcnow())
    )
    session.commit()

    body = client.get("/gui/fragments/bestellungen").text

    assert "awaiting" in body and "overdue" not in body
    assert "Noch nicht an der Kasse" in body and "Wartet seit" not in body


def test_the_script_of_the_tablet_rings_for_a_card_that_turned_red():
    """Nobody tapped "Passt", so nobody is looking at the tablet: without a
    sound the red card is as silent as the yellow one was. Not run here, the
    suite has no browser; this keeps the hook the template writes in the script."""
    script = (STATIC / "app.js").read_text(encoding="utf-8")
    template = (TEMPLATES / "fragments" / "bestellungen.html").read_text(
        encoding="utf-8"
    )

    assert 'data-overdue="1"' in template
    assert "[data-overdue]" in script and "turnedRed" in script
