"""tool_call_log_middleware: jeder /v1/tools/*-Aufruf landet mit Dauer und
Ergebnis in calls.tool_calls (docs/03 §calls, docs/04 §Gemeinsame Regeln)."""

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
        yield TestClient(app), str(tenant_id), str(call_id), engine
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def test_erfolgreicher_tool_aufruf_wird_protokolliert(client):
    http, tenant_id, call_id, engine = client

    http.post(
        "/v1/tools/get_service_status",
        json={"call_id": call_id, "tenant_id": tenant_id},
        headers=AUTH,
    )

    with Session(engine) as s:
        call = s.get(Call, uuid.UUID(call_id))
        assert len(call.tool_calls) == 1
        entry = call.tool_calls[0]
        assert entry["name"] == "get_service_status"
        assert entry["ok"] is True
        assert entry["duration_ms"] >= 0


def test_fehlgeschlagener_tool_aufruf_traegt_den_fehlercode(client):
    http, tenant_id, call_id, engine = client

    http.post(
        "/v1/tools/check_slot",
        json={
            "call_id": call_id,
            "tenant_id": tenant_id,
            "party_size": 0,
            "reserved_for": "2026-09-15T18:00:00+02:00",
        },
        headers=AUTH,
    )

    with Session(engine) as s:
        call = s.get(Call, uuid.UUID(call_id))
        entry = call.tool_calls[0]
        assert entry["name"] == "check_slot"
        assert entry["ok"] is False
        assert entry["error_code"] == "invalid_input"


def test_fremde_tenant_id_schreibt_nicht_in_einen_fremden_anruf(client):
    """Eigene call_id, aber die tenant_id eines anderen Mandanten im Body: das
    Protokoll darf nicht in den Anruf eines fremden Mandanten schreiben, auch wenn
    die Fach-Antwort selbst schon `not_found` meldet (Codex-Review PR #99, P1)."""
    http, _tenant_id, call_id, engine = client
    fremder_mandant = str(uuid.uuid4())

    http.post(
        "/v1/tools/get_service_status",
        json={"call_id": call_id, "tenant_id": fremder_mandant},
        headers=AUTH,
    )

    with Session(engine) as s:
        call = s.get(Call, uuid.UUID(call_id))
        assert call.tool_calls == []


def test_unbekannte_call_id_im_body_scheitert_nicht_still(client):
    """Der Aufruf betrifft keinen bekannten Anruf: das Protokoll findet keine
    Zeile zum Aktualisieren und darf dabei still bleiben, nicht scheitern."""
    http, tenant_id, _call_id, _engine = client
    fremder_anruf = str(uuid.uuid4())

    r = http.post(
        "/v1/tools/get_service_status",
        json={"call_id": fremder_anruf, "tenant_id": tenant_id},
        headers=AUTH,
    )

    assert r.status_code == 200


def test_zwei_aufrufe_haengen_beide_an(client):
    http, tenant_id, call_id, engine = client

    http.post(
        "/v1/tools/get_service_status",
        json={"call_id": call_id, "tenant_id": tenant_id},
        headers=AUTH,
    )
    http.post(
        "/v1/tools/check_slot",
        json={
            "call_id": call_id,
            "tenant_id": tenant_id,
            "party_size": 2,
            "reserved_for": "2026-09-15T18:00:00+02:00",
        },
        headers=AUTH,
    )

    with Session(engine) as s:
        call = s.get(Call, uuid.UUID(call_id))
        names = [entry["name"] for entry in call.tool_calls]
        assert names == ["get_service_status", "check_slot"]


def test_ping_wird_nicht_protokolliert(client):
    http, _tenant_id, call_id, engine = client

    http.post("/v1/tools/ping", headers=AUTH)

    with Session(engine) as s:
        call = s.get(Call, uuid.UUID(call_id))
        assert call.tool_calls == []
