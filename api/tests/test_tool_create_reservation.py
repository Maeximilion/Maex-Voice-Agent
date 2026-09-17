"""HTTP-Hülle für create_reservation: Hülle, Validierung, Idempotenz über HTTP, Latenz."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from api.config import settings
from api.db import get_db
from api.main import app
from api.models import Call
from api.tests.conftest import p95_ms
from scripts.seed import seed

AUTH = {"Authorization": f"Bearer {settings.agent_api_token}"}


@pytest.fixture
def client(migrated_db_url):
    engine = create_engine(migrated_db_url)
    with Session(engine) as s:
        tenant_id = uuid.UUID(
            seed(s, tenant_name="Testbetrieb", timezone="Europe/Berlin").tenant_id
        )
        call = Call(
            tenant_id=tenant_id,
            external_session_id="ext",
            started_at=datetime.now(UTC),
            delete_after=datetime.now(UTC).date(),
        )
        s.add(call)
        s.commit()
        call_id = call.id

    def override_get_db():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield TestClient(app), str(tenant_id), str(call_id)
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def _next_tuesday_1830_berlin() -> datetime:
    today = datetime.now(UTC).date()
    days_ahead = (1 - today.weekday()) % 7 or 7
    return datetime.combine(
        today + timedelta(days=days_ahead), datetime.min.time(), tzinfo=UTC
    ).replace(hour=16, minute=30)


def _call(http: TestClient, tenant_id: str, call_id: str, **overrides):
    body = {
        "call_id": call_id,
        "tenant_id": tenant_id,
        "idempotency_key": uuid.uuid4().hex,
        "guest_name": "Müller",
        "phone": "+4972215551234",
        "party_size": 2,
        "reserved_for": _next_tuesday_1830_berlin().isoformat(),
        "note": "Kinderstuhl",
        **overrides,
    }
    return http.post("/v1/tools/create_reservation", json=body, headers=AUTH)


def test_antwort_folgt_der_huelle(client):
    http, tenant_id, call_id = client
    r = _call(http, tenant_id, call_id)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["say"] is None
    data = body["data"]
    assert set(data) == {
        "reservation_id",
        "status",
        "reserved_for",
        "party_size",
        "guest_name",
        "phone",
        "note",
        "readback",
    }
    assert data["status"] == "draft"
    assert data["readback"].endswith("Passt das so?")
    uuid.UUID(data["reservation_id"])


def test_gleicher_schluessel_ueber_http_liefert_dieselbe_antwort(client):
    http, tenant_id, call_id = client
    key = "http-key-1"
    first = _call(http, tenant_id, call_id, idempotency_key=key).json()
    second = _call(http, tenant_id, call_id, idempotency_key=key, party_size=7).json()
    assert second == first


def test_fehlender_schluessel_ist_invalid_input(client):
    http, tenant_id, call_id = client
    body = {
        "call_id": call_id,
        "tenant_id": tenant_id,
        "guest_name": "Müller",
        "phone": "+4972215551234",
        "party_size": 2,
        "reserved_for": _next_tuesday_1830_berlin().isoformat(),
    }
    r = http.post("/v1/tools/create_reservation", json=body, headers=AUTH)
    assert r.status_code == 200
    assert r.json()["error"]["code"] == "invalid_input"
    assert "idempotency_key" in r.json()["error"]["message"]


def test_party_size_null_ist_invalid_input(client):
    http, tenant_id, call_id = client
    r = _call(http, tenant_id, call_id, party_size=0)
    assert r.json()["error"]["code"] == "invalid_input"


def test_unbekannter_anruf_ist_not_found_mit_say(client):
    http, tenant_id, _ = client
    r = _call(http, tenant_id, str(uuid.uuid4()))
    assert r.json()["ok"] is False
    assert r.json()["error"]["code"] == "not_found"
    assert r.json()["say"]


def test_ohne_token_401(client):
    http, _tenant_id, _call_id = client
    r = http.post("/v1/tools/create_reservation", json={})
    assert r.status_code == 401


def test_latenz_p95_unter_300_ms(client):
    http, tenant_id, call_id = client
    p95 = p95_ms(lambda: _call(http, tenant_id, call_id, party_size=1), n=20)
    print(f"\ncreate_reservation p95 = {p95:.1f} ms")
    assert p95 < 300, f"p95 {p95:.1f} ms über dem Budget aus docs/04 §1"
