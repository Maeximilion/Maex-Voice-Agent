"""HTTP-Hülle für transfer_to_team: Hülle, Auth, Validierung, Latenz."""

import uuid
from datetime import UTC, datetime

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


def body(tenant_id: str, call_id: str, **overrides) -> dict:
    return {
        "call_id": call_id,
        "tenant_id": tenant_id,
        "reason": "human_requested",
        **overrides,
    }


def test_uebergabe_kommt_in_der_huelle_mit_durchwahl(client):
    http, tenant_id, call_id = client

    r = http.post(
        "/v1/tools/transfer_to_team", json=body(tenant_id, call_id), headers=AUTH
    )

    assert r.status_code == 200
    antwort = r.json()
    assert antwort["ok"] is True
    assert antwort["data"]["transfer_to"]
    assert antwort["data"]["available"] in (True, False)


def test_zweiter_aufruf_liefert_dieselbe_durchwahl(client):
    http, tenant_id, call_id = client
    erster = http.post(
        "/v1/tools/transfer_to_team", json=body(tenant_id, call_id), headers=AUTH
    ).json()

    zweiter = http.post(
        "/v1/tools/transfer_to_team",
        json=body(tenant_id, call_id, reason="complaint"),
        headers=AUTH,
    ).json()

    assert zweiter["data"]["transfer_to"] == erster["data"]["transfer_to"]


def test_ohne_token_401(client):
    http, tenant_id, call_id = client

    r = http.post("/v1/tools/transfer_to_team", json=body(tenant_id, call_id))

    assert r.status_code == 401


def test_unbekannter_anruf_ist_not_found_in_der_huelle(client):
    http, tenant_id, _call_id = client

    r = http.post(
        "/v1/tools/transfer_to_team",
        json=body(tenant_id, str(uuid.uuid4())),
        headers=AUTH,
    )

    assert r.status_code == 200
    assert r.json()["ok"] is False
    assert r.json()["error"]["code"] == "not_found"
    assert r.json()["say"]


def test_unbekannter_grund_ist_invalid_input(client):
    http, tenant_id, call_id = client

    r = http.post(
        "/v1/tools/transfer_to_team",
        json=body(tenant_id, call_id, reason="kaese"),
        headers=AUTH,
    )

    assert r.json()["ok"] is False
    assert r.json()["error"]["code"] == "invalid_input"
    assert "reason" in r.json()["error"]["message"]


def test_latenz_p95_unter_300_ms(client):
    http, tenant_id, call_id = client

    def call():
        http.post(
            "/v1/tools/transfer_to_team", json=body(tenant_id, call_id), headers=AUTH
        )

    p95 = p95_ms(call)

    assert p95 < 300, f"p95 {p95:.1f} ms über dem Budget"
