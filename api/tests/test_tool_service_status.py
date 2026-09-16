"""HTTP-Hülle für get_service_status: Auth, Hülle, Fehler, Latenz."""

import uuid

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


def _call(client: TestClient, tenant_id: str, **extra):
    body = {"call_id": str(uuid.uuid4()), "tenant_id": tenant_id, **extra}
    return client.post("/v1/tools/get_service_status", json=body, headers=AUTH)


def test_antwort_folgt_der_huelle(client):
    http, tenant_id = client
    r = _call(http, tenant_id)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and "say" in body
    assert set(body["data"]) == {
        "is_open",
        "closes_at",
        "pickup_enabled",
        "delivery_enabled",
        "pickup_wait_minutes",
        "delivery_wait_minutes",
        "sold_out",
        "call_mode",
    }
    assert body["data"]["call_mode"] == "shadow"
    assert body["data"]["sold_out"] == []
    assert r.headers["X-Request-ID"]


def test_ohne_token_401(client):
    http, tenant_id = client
    r = http.post(
        "/v1/tools/get_service_status",
        json={"call_id": str(uuid.uuid4()), "tenant_id": tenant_id},
    )
    assert r.status_code == 401
    assert r.json()["ok"] is False


def test_fehlende_tenant_id_ist_invalid_input(client):
    http, _ = client
    r = http.post(
        "/v1/tools/get_service_status",
        json={"call_id": str(uuid.uuid4())},
        headers=AUTH,
    )
    assert r.status_code == 200
    assert r.json()["error"]["code"] == "invalid_input"
    assert "tenant_id" in r.json()["error"]["message"]


def test_unbekannter_mandant_ist_not_found_in_der_huelle(client):
    http, _ = client
    r = _call(http, str(uuid.uuid4()))
    assert r.status_code == 200
    assert r.json()["error"]["code"] == "not_found"


def test_latenz_p95_unter_300_ms(client):
    http, tenant_id = client
    p95 = p95_ms(lambda: _call(http, tenant_id), n=20)
    print(f"\nget_service_status p95 = {p95:.1f} ms")
    assert p95 < 300, f"p95 {p95:.1f} ms über dem Budget aus docs/04 §1"
