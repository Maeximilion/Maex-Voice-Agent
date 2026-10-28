"""The daily push about orders nobody marked as typed into the register (T-11.6, L18)."""

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import Session

from api.config import settings
from api.domain.ordering.board import approve_order, mark_entered
from api.events.types import DAILY_REPORT
from api.jobs.daily_report import REGISTER_UNMARKED, report
from api.models import Order, OutboxEvent, Tenant
from api.tests.test_domain_confirm_order import _mode
from api.tests.test_domain_draft_order import NOW, _call, _tenant
from api.tests.test_domain_ordering_board import _order

DAY = timedelta(days=1)


@pytest.fixture(autouse=True)
def report_hour(monkeypatch):
    monkeypatch.setattr(settings, "register_report_hour", 10)


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


def _reports(session, tenant_id=None) -> list[OutboxEvent]:
    query = select(OutboxEvent).where(OutboxEvent.event_type == DAILY_REPORT)
    if tenant_id is not None:
        query = query.where(OutboxEvent.tenant_id == tenant_id)
    return list(session.scalars(query.order_by(OutboxEvent.created_at)))


def test_one_report_a_day_with_count_and_oldest_day(session, tenant_id, call_id):
    _order(session, tenant_id, call_id)  # today's: not counted yet
    _order(session, tenant_id, call_id, now=NOW - 3 * DAY)
    approved = _order(session, tenant_id, call_id, now=NOW - 2 * DAY)
    approve_order(session, tenant_id, approved)

    assert report(session, NOW) == 1
    assert report(session, NOW + timedelta(hours=1)) == 0
    [event] = _reports(session)
    assert event.tenant_id == tenant_id and event.status == "pending"
    # Counts and dates only: nothing the deletion job would have to strip.
    assert event.payload == {
        "report": REGISTER_UNMARKED,
        "business_day": "2026-09-15",
        "count": 2,
        "oldest_day": "2026-09-12",
    }

    # Still unmarked the next day: a new report, now with yesterday's order too.
    assert report(session, NOW + DAY) == 1
    assert _reports(session)[-1].payload["count"] == 3


@pytest.mark.parametrize(
    "local", [NOW.replace(hour=9, minute=59), (NOW + DAY).replace(hour=2)]
)
def test_no_report_before_the_hour(session, tenant_id, call_id, local):
    """02:00 still belongs to the business day before, past its hour already;
    its report waits for 10:00 of the next one instead of going out at night."""
    _order(session, tenant_id, call_id, now=NOW - 3 * DAY)
    assert report(session, local) == 0
    assert _reports(session) == []


def test_no_report_when_every_order_is_marked(session, tenant_id, call_id):
    order_id = _order(session, tenant_id, call_id, now=NOW - 2 * DAY)
    approve_order(session, tenant_id, order_id)
    mark_entered(session, tenant_id, order_id, 0, now=NOW - 2 * DAY)
    cancelled = _order(session, tenant_id, call_id, now=NOW - 2 * DAY)
    session.execute(
        update(Order).where(Order.id == cancelled).values(status="cancelled")
    )
    session.commit()

    assert report(session, NOW) == 0
    assert _reports(session) == []


def test_a_broken_tenant_does_not_stop_the_others(session, tenant_id, call_id, caplog):
    broken = _tenant(session, "Kaputt")
    _mode(session, broken, "primary")
    _order(session, broken, _call(session, broken), now=NOW - 2 * DAY)
    session.execute(
        update(Tenant).where(Tenant.id == broken).values(timezone="Nowhere/Nope")
    )
    session.commit()
    _order(session, tenant_id, call_id, now=NOW - 2 * DAY)

    assert report(session, NOW) == 1
    [event] = _reports(session)
    assert event.tenant_id == tenant_id
    assert "Daily report failed for a tenant" in caplog.text
