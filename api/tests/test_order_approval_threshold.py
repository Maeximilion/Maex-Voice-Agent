"""A large order waits for the team before a slip prints, whatever the mode (#252).

Maxi, 12.10.2026: above 150 euros the order waits for "Passt" on the tablet.
The amount is the restaurant's rule, `service_config.order_approval_above_cents`
(migration 007), never a number in the code. The guest's yes still confirms the
order; what waits is the kitchen ticket.
"""

import uuid

import pytest
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import Session

from api.domain.ordering import draft_order
from api.domain.ordering.board import approve_order
from api.models import AuditLog, MenuItem, Order, ServiceConfig
from api.tests.test_domain_confirm_order import _confirm, _events, _mode
from api.tests.test_domain_draft_order import NOW, _call, _tenant, item, request

# Dish 23 of the test menu costs 6,90 euros.
PRICE_CENTS = 690


@pytest.fixture
def session(migrated_db_url):
    engine = create_engine(migrated_db_url)
    with Session(engine) as s:
        yield s
    engine.dispose()


@pytest.fixture
def tenant_id(session) -> uuid.UUID:
    tid = _tenant(session, "Testbetrieb")
    _mode(session, tid, "primary")
    return tid


@pytest.fixture
def call_id(session, tenant_id) -> uuid.UUID:
    return _call(session, tenant_id)


def _limit(session, tenant_id, cents: int) -> None:
    session.execute(
        update(ServiceConfig)
        .where(ServiceConfig.tenant_id == tenant_id)
        .values(order_approval_above_cents=cents)
    )
    session.commit()


def _order(session, tenant_id, call_id, quantity: int = 2) -> uuid.UUID:
    items = [{"menu_item_id": item(session, tenant_id, "23"), "quantity": quantity}]
    # NOW: a moment the test restaurant is open, whatever the clock says.
    draft = draft_order(session, request(session, tenant_id, call_id, items), now=NOW)
    return draft.order_id


def _audit(session, order_id) -> dict:
    row = session.scalar(
        select(AuditLog).where(
            AuditLog.entity_id == order_id, AuditLog.action == "order.confirmed"
        )
    )
    assert row is not None
    return row.payload


def test_an_order_above_the_limit_waits_although_the_mode_sends_directly(
    session, tenant_id, call_id
):
    _limit(session, tenant_id, 2 * PRICE_CENTS - 1)
    order_id = _order(session, tenant_id, call_id)

    result = _confirm(session, tenant_id, call_id, order_id)

    # Confirmed for the guest, with a pickup code: only the ticket waits.
    assert result.status == "confirmed" and result.pickup_code == "A1"
    assert result.handover == "awaiting_approval"
    order = session.get(Order, order_id)
    session.refresh(order)
    assert order.status == "confirmed" and order.handover_state is None
    assert _events(session) == []
    assert _audit(session, order_id)["waits_for"] == "amount"


def test_an_order_of_exactly_the_limit_goes_to_the_kitchen(session, tenant_id, call_id):
    """Above the amount, not from it: 150,00 euros with a limit of 150 prints."""
    _limit(session, tenant_id, 2 * PRICE_CENTS)
    order_id = _order(session, tenant_id, call_id)

    result = _confirm(session, tenant_id, call_id, order_id)

    assert result.handover == "queued"
    assert len(_events(session)) == 1
    assert _audit(session, order_id)["waits_for"] is None


def test_the_limit_of_the_migration_holds_an_order_above_150_euros(
    session, tenant_id, call_id
):
    """No value set by the test: the default the restaurant starts with."""
    below = _order(session, tenant_id, call_id, quantity=21)  # 144,90 euros
    other_call = _call(session, tenant_id)
    above = _order(session, tenant_id, other_call, quantity=22)  # 151,80 euros

    assert _confirm(session, tenant_id, call_id, below).handover == "queued"
    assert _confirm(session, tenant_id, other_call, above).handover == (
        "awaiting_approval"
    )
    assert [e.payload["order_id"] for e in _events(session)] == [str(below)]


def test_a_limit_of_zero_holds_every_order(session, tenant_id, call_id):
    _limit(session, tenant_id, 0)
    order_id = _order(session, tenant_id, call_id, quantity=1)

    assert _confirm(session, tenant_id, call_id, order_id).handover == (
        "awaiting_approval"
    )
    assert _events(session) == []


def test_a_limit_of_zero_also_holds_an_order_that_costs_nothing(
    session, tenant_id, call_id
):
    """Zero means the team sees every order. A dish priced at zero makes an
    order no amount is above (Codex, PR #283)."""
    session.execute(
        update(MenuItem)
        .where(MenuItem.tenant_id == tenant_id, MenuItem.number == "23")
        .values(price_cents=0)
    )
    session.commit()
    _limit(session, tenant_id, 0)
    order_id = _order(session, tenant_id, call_id, quantity=1)
    assert session.get(Order, order_id).total_cents == 0

    assert _confirm(session, tenant_id, call_id, order_id).handover == (
        "awaiting_approval"
    )
    assert _events(session) == []


def test_passt_on_the_tablet_sends_the_held_order_to_the_kitchen(
    session, tenant_id, call_id
):
    _limit(session, tenant_id, 0)
    order_id = _order(session, tenant_id, call_id)
    _confirm(session, tenant_id, call_id, order_id)
    assert _events(session) == []

    approve_order(session, tenant_id, order_id)

    [event] = _events(session)
    assert event.payload["order_id"] == str(order_id)
    order = session.get(Order, order_id)
    session.refresh(order)
    assert order.status == "approved" and order.handover_state == "pending"


def test_a_raised_limit_does_not_change_the_answer_for_an_order_already_held(
    session, tenant_id, call_id
):
    """The decision stays with the order: a second confirm, after the team
    raised the limit, must not print a ticket nobody approved."""
    _limit(session, tenant_id, 0)
    order_id = _order(session, tenant_id, call_id)
    first = _confirm(session, tenant_id, call_id, order_id)
    _limit(session, tenant_id, 1_000_000)

    assert _confirm(session, tenant_id, call_id, order_id) == first
    assert _events(session) == []


@pytest.mark.parametrize("mode", ["overflow", "shadow", "paused"])
def test_outside_the_direct_mode_the_mode_is_the_reason(
    session, tenant_id, call_id, mode
):
    _mode(session, tenant_id, mode)
    _limit(session, tenant_id, 0)
    order_id = _order(session, tenant_id, call_id)

    assert _confirm(session, tenant_id, call_id, order_id).handover == (
        "awaiting_approval"
    )
    assert _audit(session, order_id)["waits_for"] == "mode"
