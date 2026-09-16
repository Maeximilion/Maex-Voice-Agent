"""HTTP-Hülle für check_slot: Hülle, Validierung, Latenz."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from api.config import settings
from api.db import get_db
from api.main import app
from api.tests.conftest import p95_ms
from scripts.seed import seed

AUTH = {"Authorization": f"Bearer {settings.agent_api_token}"}


@pytest.fixture
def client(migrated_db_url):
    engine = create_engine(migrated_db_url)
    with Session(engine) as s:
        tenant_id = seed(
            s, tenant_name="Testbetrieb", timezone="Europe/Berlin"
        ).tenant_id

    def override_get_db():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield TestClient(app), tenant_id
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def _next_tuesday_1830() -> datetime:
    today = datetime.now(UTC).date()
    days_ahead = (1 - today.weekday()) % 7 or 7
    return datetime.combine(
        today + timedelta(days=days_ahead), datetime.min.time(), tzinfo=UTC
    ).replace(hour=16, minute=30)


def _call(client: TestClient, tenant_id: str, **overrides):
    body = {
        "call_id": str(uuid.uuid4()),
        "tenant_id": tenant_id,
        "reserved_for": _next_tuesday_1830().isoformat(),
        "party_size": 4,
        **overrides,
    }
    return client.post("/v1/tools/check_slot", json=body, headers=AUTH)


def test_antwort_folgt_der_huelle(client):
    http, tenant_id = client
    r = _call(http, tenant_id)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and "say" in body
    assert set(body["data"]) == {"available", "alternatives"}
    assert body["data"]["available"] is True


def test_party_size_null_ist_invalid_input(client):
    http, tenant_id = client
    r = _call(http, tenant_id, party_size=0)
    assert r.status_code == 200
    assert r.json()["error"]["code"] == "invalid_input"
    assert "party_size" in r.json()["error"]["message"]


def test_zeit_ohne_zeitzone_ist_invalid_input(client):
    http, tenant_id = client
    r = _call(http, tenant_id, reserved_for="2027-01-05T18:30:00")
    assert r.json()["ok"] is False
    assert r.json()["error"]["code"] == "invalid_input"


def test_vergangenheit_ist_invalid_input_mit_say(client):
    http, tenant_id = client
    r = _call(http, tenant_id, reserved_for="2020-01-01T18:30:00Z")
    assert r.json()["error"]["code"] == "invalid_input"
    assert r.json()["say"] == "Dieser Zeitpunkt ist schon vorbei."


def test_latenz_p95_unter_300_ms(client):
    http, tenant_id = client
    p95 = p95_ms(lambda: _call(http, tenant_id), n=20)
    print(f"\ncheck_slot p95 = {p95:.1f} ms")
    assert p95 < 300, f"p95 {p95:.1f} ms über dem Budget aus docs/04 §1"
