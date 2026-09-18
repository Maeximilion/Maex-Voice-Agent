"""HTTP-Hülle für confirm: Hülle, Validierung, Idempotenz über HTTP, Latenz."""

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
from api.tests.conftest import LATENZ_RUNDEN, p95_ms
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


def _draft(http: TestClient, tenant_id: str, call_id: str, **overrides) -> str:
    body = {
        "call_id": call_id,
        "tenant_id": tenant_id,
        "idempotency_key": uuid.uuid4().hex,
        "guest_name": "Müller",
        "phone": "+4972215551234",
        "party_size": 2,
        "reserved_for": _next_tuesday_1830_berlin().isoformat(),
        **overrides,
    }
    r = http.post("/v1/tools/create_reservation", json=body, headers=AUTH)
    return r.json()["data"]["reservation_id"]


def _confirm(http: TestClient, tenant_id: str, call_id: str, entity_id: str, **over):
    body = {
        "call_id": call_id,
        "tenant_id": tenant_id,
        "entity": "reservation",
        "entity_id": entity_id,
        "idempotency_key": uuid.uuid4().hex,
        **over,
    }
    return http.post("/v1/tools/confirm", json=body, headers=AUTH)


def test_antwort_folgt_der_huelle(client):
    http, tenant_id, call_id = client
    entity_id = _draft(http, tenant_id, call_id)

    r = _confirm(http, tenant_id, call_id, entity_id)

    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["say"] is None
    assert body["data"] == {
        "status": "confirmed",
        "handover": "queued",
        "pickup_code": None,
    }


def test_zweiter_aufruf_ueber_http_liefert_dieselbe_antwort(client):
    http, tenant_id, call_id = client
    entity_id = _draft(http, tenant_id, call_id)

    first = _confirm(http, tenant_id, call_id, entity_id).json()
    second = _confirm(http, tenant_id, call_id, entity_id).json()

    assert second == first


def test_unbekannte_reservierung_ist_not_found_mit_say(client):
    http, tenant_id, call_id = client
    r = _confirm(http, tenant_id, call_id, str(uuid.uuid4()))
    assert r.status_code == 200
    assert r.json()["ok"] is False
    assert r.json()["error"]["code"] == "not_found"
    assert r.json()["say"]


def test_unbekannte_entity_ist_invalid_input(client):
    http, tenant_id, call_id = client
    entity_id = _draft(http, tenant_id, call_id)
    r = _confirm(http, tenant_id, call_id, entity_id, entity="tisch")
    assert r.json()["error"]["code"] == "invalid_input"


def test_fehlender_schluessel_ist_invalid_input(client):
    http, tenant_id, call_id = client
    entity_id = _draft(http, tenant_id, call_id)
    body = {
        "call_id": call_id,
        "tenant_id": tenant_id,
        "entity": "reservation",
        "entity_id": entity_id,
    }
    r = http.post("/v1/tools/confirm", json=body, headers=AUTH)
    assert r.json()["error"]["code"] == "invalid_input"
    assert "idempotency_key" in r.json()["error"]["message"]


def test_ohne_token_401(client):
    http, _, _ = client
    r = http.post("/v1/tools/confirm", json={})
    assert r.status_code == 401


def test_latenz_p95_unter_300_ms(client):
    """Gemessen wird der Schreibpfad: je Aufruf ein frischer Entwurf, vorher angelegt.

    Genug Entwürfe für alle Messreihen, falls die erste über dem Budget liegt. Je
    Messreihe eine andere Woche, sonst reißt der Abend die Kapazität von 40 Gästen.
    """
    http, tenant_id, call_id = client
    drafts = iter(
        [
            _draft(
                http,
                tenant_id,
                call_id,
                party_size=1,
                reserved_for=(
                    _next_tuesday_1830_berlin() + timedelta(weeks=i // 20)
                ).isoformat(),
            )
            for i in range(20 * LATENZ_RUNDEN)
        ]
    )

    p95 = p95_ms(lambda: _confirm(http, tenant_id, call_id, next(drafts)), n=20)
    print(f"\nconfirm p95 = {p95:.1f} ms")
    assert p95 < 300, f"p95 {p95:.1f} ms über dem Budget aus docs/04 §1"
