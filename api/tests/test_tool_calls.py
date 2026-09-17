"""HTTP-Hülle für /v1/calls/start und /end: Hülle, Auth, Idempotenz."""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from api.config import settings
from api.db import get_db
from api.main import app
from scripts.seed import seed

AUTH = {"Authorization": f"Bearer {settings.agent_api_token}"}


@pytest.fixture
def client(migrated_db_url):
    engine = create_engine(migrated_db_url)
    with Session(engine) as s:
        tenant_id = uuid.UUID(
            seed(s, tenant_name="Testbetrieb", timezone="Europe/Berlin").tenant_id
        )

    def override_get_db():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield TestClient(app), str(tenant_id)
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def test_anruf_startet_und_liefert_eine_call_id(client):
    http, tenant_id = client

    r = http.post(
        "/v1/calls/start",
        json={
            "tenant_id": tenant_id,
            "external_session_id": "ext-abc",
            "caller_id": "0721 5551234",
        },
        headers=AUTH,
    )

    assert r.status_code == 200
    antwort = r.json()
    assert antwort["ok"] is True
    uuid.UUID(antwort["data"]["call_id"])


def test_zweiter_start_derselben_session_liefert_dieselbe_call_id(client):
    http, tenant_id = client
    body = {
        "tenant_id": tenant_id,
        "external_session_id": "ext-retry",
        "caller_id": None,
    }

    erster = http.post("/v1/calls/start", json=body, headers=AUTH).json()
    zweiter = http.post("/v1/calls/start", json=body, headers=AUTH).json()

    assert zweiter["data"]["call_id"] == erster["data"]["call_id"]


def test_anruf_endet_mit_dauer(client):
    http, tenant_id = client
    call_id = http.post(
        "/v1/calls/start",
        json={"tenant_id": tenant_id, "external_session_id": "ext-end"},
        headers=AUTH,
    ).json()["data"]["call_id"]

    r = http.post(
        "/v1/calls/end",
        json={"call_id": call_id, "tenant_id": tenant_id, "outcome": "completed"},
        headers=AUTH,
    )

    assert r.status_code == 200
    antwort = r.json()
    assert antwort["ok"] is True
    assert antwort["data"]["outcome"] == "completed"
    assert antwort["data"]["duration_seconds"] >= 0


def test_ohne_token_401(client):
    http, tenant_id = client

    r = http.post(
        "/v1/calls/start",
        json={"tenant_id": tenant_id, "external_session_id": "ext-noauth"},
    )

    assert r.status_code == 401


def test_start_unbekannter_mandant_ist_not_found(client):
    http, _tenant_id = client

    r = http.post(
        "/v1/calls/start",
        json={"tenant_id": str(uuid.uuid4()), "external_session_id": "ext-x"},
        headers=AUTH,
    )

    assert r.json()["ok"] is False
    assert r.json()["error"]["code"] == "not_found"


def test_end_unbekannter_anruf_ist_not_found(client):
    http, tenant_id = client

    r = http.post(
        "/v1/calls/end",
        json={
            "call_id": str(uuid.uuid4()),
            "tenant_id": tenant_id,
            "outcome": "completed",
        },
        headers=AUTH,
    )

    assert r.json()["ok"] is False
    assert r.json()["error"]["code"] == "not_found"


def test_unbekanntes_ergebnis_ist_invalid_input(client):
    http, tenant_id = client
    call_id = http.post(
        "/v1/calls/start",
        json={"tenant_id": tenant_id, "external_session_id": "ext-bad-outcome"},
        headers=AUTH,
    ).json()["data"]["call_id"]

    r = http.post(
        "/v1/calls/end",
        json={"call_id": call_id, "tenant_id": tenant_id, "outcome": "kaese"},
        headers=AUTH,
    )

    assert r.json()["ok"] is False
    assert r.json()["error"]["code"] == "invalid_input"
