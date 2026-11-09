"""The ops API of a stack for the operator dashboard (#571, docs/23 §8).

`GET /v1/ops/status` answers with counts, times and codes only: no guest data
reaches the dashboard without a reason, and this route has none to give.
"""

import uuid
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from api.config import Settings, settings
from api.core import time as clock
from api.db import get_db
from api.domain.status import overview
from api.main import app
from api.models import (
    AuditLog,
    BridgeHeartbeat,
    Call,
    ItemAllergen,
    MenuItem,
    ServiceConfig,
    Tenant,
)
from api.tests.conftest import p95_ms
from scripts.seed import seed

BERLIN = ZoneInfo("Europe/Berlin")
# Thursday 29.10.2026, 14:00 in Berlin: the business day began at 05:00.
NOW = datetime(2026, 10, 29, 14, 0, tzinfo=BERLIN)
OPS = "f" * 64
CALLER = "+4972215550199"


@pytest.fixture
def session(migrated_db_url):
    engine = create_engine(migrated_db_url)
    with Session(engine) as s:
        yield s
    engine.dispose()


@pytest.fixture
def tenant(session) -> Tenant:
    result = seed(session, tenant_name="Testbetrieb", timezone="Europe/Berlin")
    return session.get(Tenant, uuid.UUID(result.tenant_id))


@pytest.fixture(autouse=True)
def bridge_settings(monkeypatch):
    monkeypatch.setattr(settings, "kitchen_bridge_tenant_id", "")
    monkeypatch.setattr(settings, "alarm_bridge_silent_minutes", 5)


def _call(session, tenant, at, outcome="completed") -> None:
    session.add(
        Call(
            tenant_id=tenant.id,
            external_session_id=uuid.uuid4().hex,
            caller_id=CALLER,
            started_at=at,
            ended_at=at + timedelta(minutes=2),
            outcome=outcome,
            cost_cents=12,
            delete_after=date(2028, 10, 29),
        )
    )


def _item(session, tenant, number, allergens=(), register=None, active=True):
    item = MenuItem(
        tenant_id=tenant.id,
        number=number,
        name=f"Gericht {number}",
        category="Hauptgerichte",
        price_cents=990,
        active=active,
        register_price_cents=register,
    )
    session.add(item)
    session.flush()
    for code in allergens:
        session.add(
            ItemAllergen(
                menu_item_id=item.id,
                allergen_code=code,
                confirmed_by="Inhaber",
                confirmed_at=NOW,
            )
        )


def _audit(session, tenant, action, at, payload=None) -> None:
    session.add(
        AuditLog(
            tenant_id=tenant.id,
            actor="monitor",
            action=action,
            entity="monitor",
            payload=payload or {},
            at=at,
        )
    )


def _busy_day(session, tenant) -> None:
    _call(session, tenant, NOW - timedelta(hours=3))
    _call(session, tenant, NOW - timedelta(hours=1), outcome="transferred")
    # Yesterday's business day: in no count of today.
    _call(session, tenant, NOW - timedelta(days=1))
    _item(session, tenant, "1", allergens=("A", "G"))
    _item(session, tenant, "2", register=1090)
    _item(session, tenant, "3")
    # Inactive: offered to nobody, so neither counted nor missing anything.
    _item(session, tenant, "4", active=False)
    _audit(session, tenant, "backup.done", NOW - timedelta(hours=9))
    _audit(
        session,
        tenant,
        "monitor.alarm",
        NOW - timedelta(hours=2),
        {"kind": "bridge_silent", "key": "h1"},
    )
    _audit(
        session,
        tenant,
        "monitor.alarm",
        NOW - timedelta(hours=1),
        {"kind": "bridge_silent", "key": "h2"},
    )
    # The day report goes out under the same action, but is no alarm.
    _audit(
        session,
        tenant,
        "monitor.alarm",
        NOW - timedelta(hours=6),
        {"kind": "day_report", "key": "2026-10-28"},
    )
    # Older than a day: no longer open.
    _audit(
        session,
        tenant,
        "monitor.alarm",
        NOW - timedelta(hours=30),
        {"kind": "errors", "key": "h0"},
    )
    session.commit()


def test_the_status_of_a_busy_day(session, tenant):
    _busy_day(session, tenant)

    status = overview.status(session, tenant, NOW)

    assert status.tenant_name == "Testbetrieb"
    assert status.call_mode == "shadow"
    assert status.last_call_at == NOW - timedelta(hours=1)
    assert status.business_day == date(2026, 10, 29)
    assert status.today.calls == 2
    assert status.today.handed_over == 1
    assert status.today.cost_cents == 24
    assert status.menu == overview.MenuStatus(
        items=3, price_differences=1, without_allergen_information=2
    )
    assert status.backup_at == NOW - timedelta(hours=9)
    assert status.alarms == [
        overview.Alarm(kind="bridge_silent", last_at=NOW - timedelta(hours=1), count=2)
    ]
    assert status.print_bridge == overview.BridgeStatus(
        configured=False, last_poll_at=None, silent=False
    )


def test_a_new_stack_has_nothing_yet(session, tenant):
    status = overview.status(session, tenant, NOW)

    assert status.last_call_at is None
    assert status.today.calls == 0
    assert status.menu == overview.MenuStatus(0, 0, 0)
    assert status.backup_at is None
    assert status.alarms == []


def test_a_silent_bridge_while_pickup_is_open(session, tenant, monkeypatch):
    monkeypatch.setattr(settings, "kitchen_bridge_tenant_id", str(tenant.id))
    session.add(BridgeHeartbeat(tenant_id=tenant.id, seen_at=NOW - timedelta(hours=2)))
    session.commit()
    # 13:00: the seed holds pickup open from 11:30 to 14:00 on a Thursday.
    at_one = NOW - timedelta(hours=1)

    bridge = overview.status(session, tenant, at_one).print_bridge

    assert bridge.configured is True
    assert bridge.last_poll_at == NOW - timedelta(hours=2)
    assert bridge.silent is True
    # Its poll a minute ago: not silent.
    assert not overview.status(
        session, tenant, NOW - timedelta(hours=2, minutes=-1)
    ).print_bridge.silent


def test_the_emergency_stop_shows(session, tenant):
    session.get(ServiceConfig, tenant.id).call_mode = "paused"
    session.commit()

    assert overview.status(session, tenant, NOW).call_mode == "paused"


# --- Over HTTP ---------------------------------------------------------------


@pytest.fixture
def http(migrated_db_url, monkeypatch):
    engine = create_engine(migrated_db_url)

    def override_get_db():
        with Session(engine) as s:
            yield s

    app.dependency_overrides[get_db] = override_get_db
    monkeypatch.setattr(settings, "ops_token", OPS)
    monkeypatch.setattr(clock, "utcnow", lambda: NOW)
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


AUTH = {"Authorization": f"Bearer {OPS}"}


def test_http_status_carries_no_guest_data(http, session, tenant):
    _busy_day(session, tenant)

    response = http.get("/v1/ops/status", headers=AUTH)

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["tenant"] == {"key": None, "name": "Testbetrieb"}
    assert data["today"]["calls"] == 2
    assert data["today"]["handed_over_percent"] == 50
    # In UTC, as stored; the dashboard shows Berlin time.
    assert data["last_call_at"] == "2026-10-29T12:00:00+00:00"
    assert data["alarms"][0]["kind"] == "bridge_silent"
    assert CALLER not in response.text


def test_http_refuses_every_other_key(http, tenant, monkeypatch):
    assert http.get("/v1/ops/status").status_code == 401
    wrong = {"Authorization": f"Bearer {'e' * 64}"}
    assert http.get("/v1/ops/status", headers=wrong).status_code == 401
    agent = {"Authorization": f"Bearer {settings.agent_api_token}"}
    assert http.get("/v1/ops/status", headers=agent).status_code == 401

    # No token set: closed, also for "Bearer ".
    monkeypatch.setattr(settings, "ops_token", "")
    empty = {"Authorization": "Bearer "}
    assert http.get("/v1/ops/status", headers=empty).status_code == 401

    # A weak token outside dev and test opens nothing, should it ever be set.
    monkeypatch.setattr(settings, "ops_token", "short")
    monkeypatch.setattr(settings, "env", "prod")
    short = {"Authorization": "Bearer short"}
    assert http.get("/v1/ops/status", headers=short).status_code == 401


def test_http_a_stack_without_its_restaurant(http):
    response = http.get("/v1/ops/status", headers=AUTH)

    assert response.json()["ok"] is False
    assert response.json()["error"]["code"] == "not_found"


@pytest.mark.latency
def test_http_status_under_300_ms(http, session, tenant):
    _busy_day(session, tenant)
    ms = p95_ms(lambda: http.get("/v1/ops/status", headers=AUTH), rounds=20)
    assert ms < 300, f"p95 {ms:.0f} ms"


# --- Start check -------------------------------------------------------------

STRONG = {
    "env": "prod",
    "agent_api_token": "a" * 64,
    "kitchen_bridge_token": "b" * 64,
    "gui_proxy_secret": "c" * 64,
    "voice_media_token": "d" * 64,
}


def test_production_takes_a_strong_distinct_ops_token():
    assert Settings(**STRONG, ops_token=OPS).ops_token == OPS
    # Empty: no dashboard reads this stack yet, the route stays closed.
    assert Settings(**STRONG, ops_token="").ops_token == ""


@pytest.mark.parametrize(
    "value",
    ["short", "change_me_long_random", *(STRONG[k] for k in STRONG if k != "env")],
)
def test_production_refuses_a_weak_or_shared_ops_token(value):
    with pytest.raises(ValueError, match="OPS_TOKEN"):
        Settings(**STRONG, ops_token=value)
