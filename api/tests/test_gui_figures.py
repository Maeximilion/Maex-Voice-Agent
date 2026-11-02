"""Key figures (T-8.4, docs/06 §4): the domain numbers and the Kennzahlen page."""

import uuid
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from api.core.time import business_day
from api.db import get_db
from api.domain.calls.figures import Figures, span_figures
from api.domain.ordering.labels import ACTION_CORRECTED
from api.main import app
from api.models import AuditLog, Call, Order, Tenant
from scripts.seed import seed

BERLIN = ZoneInfo("Europe/Berlin")


@pytest.fixture
def now(monkeypatch):
    """12:00 local of the running business day, fixed for setup and request."""
    noon = datetime.combine(
        business_day(tz_name="Europe/Berlin"), time(12, 0), tzinfo=BERLIN
    ).astimezone(UTC)
    monkeypatch.setattr("api.core.time.utcnow", lambda: noon)
    return noon


@pytest.fixture
def engine(migrated_db_url):
    engine = create_engine(migrated_db_url)
    yield engine
    engine.dispose()


@pytest.fixture
def db(engine):
    with Session(engine) as s:
        yield s


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


def _tenant(db, name: str) -> Tenant:
    tid = uuid.UUID(seed(db, tenant_name=name, timezone="Europe/Berlin").tenant_id)
    return db.get(Tenant, tid)


def _call(db, tenant, at, outcome, cost=None) -> Call:
    call = Call(
        tenant_id=tenant.id,
        external_session_id=uuid.uuid4().hex,
        started_at=at,
        outcome=outcome,
        cost_cents=cost,
        delete_after=at.date(),
    )
    db.add(call)
    db.flush()
    return call


def _order(db, tenant, call, at, status="confirmed", corrected=False) -> Order:
    order = Order(
        tenant_id=tenant.id,
        call_id=call.id,
        type="pickup",
        status=status,
        phone="+4917612345678",
        customer_name="Test",
        items_total_cents=1000,
        total_cents=1000,
        idempotency_key=uuid.uuid4().hex,
        created_at=at,
    )
    db.add(order)
    db.flush()
    if corrected:
        db.add(
            AuditLog(
                tenant_id=tenant.id,
                actor="gui:tablet",
                action=ACTION_CORRECTED,
                entity="order",
                entity_id=order.id,
                payload={},
            )
        )
    return order


def _week(db, tenant, now):
    """Four calls: one order kept, one order corrected, one team, one abandoned."""
    day = business_day(now, tenant.timezone)
    a = _call(db, tenant, now, "completed", cost=10)
    b = _call(db, tenant, now, "completed", cost=15)
    _call(db, tenant, now, "transferred", cost=5)
    _call(db, tenant, now, "abandoned")
    _order(db, tenant, a, now)
    _order(db, tenant, b, now, status="approved", corrected=True)
    # A draft is not an order taken; an order of the day before is out of the day.
    _order(db, tenant, a, now, status="draft")
    _order(db, tenant, a, now - timedelta(days=1))
    db.commit()
    return day


def test_four_values_over_a_day(db, now):
    tenant = _tenant(db, "Eins")
    day = _week(db, tenant, now)
    figures = span_figures(db, tenant, day, day)
    assert figures.calls == 4
    assert figures.orders == 2
    assert figures.accuracy_percent == 50
    assert figures.cost_cents == 30
    assert figures.unknown_cost_calls == 1
    assert figures.cost_per_order_cents == 15
    assert figures.handed_over_percent == 25
    assert figures.missed == 1


def test_a_cancelled_order_is_no_order_taken(db, now):
    """Nor a corrected one: a slip that was fixed and then cancelled says nothing
    about the accuracy of an order that was never served."""
    tenant = _tenant(db, "Eins")
    day = _week(db, tenant, now)
    before = span_figures(db, tenant, day, day)
    call = _call(db, tenant, now, "completed", cost=0)
    _order(db, tenant, call, now, status="cancelled", corrected=True)
    db.commit()

    figures = span_figures(db, tenant, day, day)
    assert (figures.orders, figures.corrected) == (before.orders, before.corrected)


def test_empty_day_has_no_ratios(db, now):
    tenant = _tenant(db, "Leer")
    day = business_day(now, tenant.timezone)
    figures = span_figures(db, tenant, day, day)
    assert figures == Figures(0, 0, 0, 0, 0, 0, 0)
    assert figures.accuracy_percent is None
    assert figures.cost_per_order_cents is None
    assert figures.handed_over_percent is None


def test_cost_per_order_rounds_to_whole_cents():
    assert Figures(1, 3, 0, 0, 0, 10, 0).cost_per_order_cents == 3
    assert Figures(1, 3, 0, 0, 0, 11, 0).cost_per_order_cents == 4


def test_other_tenant_is_not_counted(db, now):
    one = _tenant(db, "Eins")
    two = _tenant(db, "Zwei")
    day = _week(db, one, now)
    figures = span_figures(db, two, day, day)
    assert (figures.calls, figures.orders, figures.missed) == (0, 0, 0)


def test_page_shows_week_and_history(db, client, now):
    tenant = _tenant(db, "Eins")
    _week(db, tenant, now)
    response = client.get("/gui/kennzahlen")
    assert response.status_code == 200
    html = response.text
    assert "Letzte 7 Tage" in html
    assert "<b>67 %</b>" in html  # the order of yesterday is in the week
    assert "<b>0,10 €</b>" in html
    assert "<b>25 %</b>" in html
    assert "<b>1</b>" in html
    assert "Bei 1 Anrufen sind die Kosten nicht bekannt" in html
    assert html.count("<tr>") == 1 + 14


def test_history_marks_a_day_with_unpriced_calls(db, client, now):
    tenant = _tenant(db, "Eins")
    _week(db, tenant, now)  # today has one call without a cost
    html = client.get("/gui/kennzahlen").text
    assert html.count("€ *</td>") == 1
    assert "An diesem Tag sind bei einigen Anrufen" in html


def test_page_on_an_empty_day_shows_dashes(db, client, now):
    _tenant(db, "Leer")
    html = client.get("/gui/kennzahlen").text
    assert "<b>\N{EN DASH}</b>" in html
    assert "nicht bekannt" not in html


def test_page_shows_only_the_first_tenant(db, client, now):
    one = _tenant(db, "Eins")
    two = _tenant(db, "Zwei")
    _week(db, two, now)
    html = client.get("/gui/kennzahlen").text
    assert one.name in html
    assert "<b>67 %</b>" not in html


def test_page_without_tenant_is_503(client):
    assert client.get("/gui/kennzahlen").status_code == 503


def test_operations_view_links_to_the_page(db, client, now):
    _tenant(db, "Eins")
    assert 'href="/gui/kennzahlen"' in client.get("/gui").text
