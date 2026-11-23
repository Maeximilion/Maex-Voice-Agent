"""HTTP-Hülle für create_callback: Hülle, Auth, Validierung, Idempotenz, Latenz."""

import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from api.config import settings
from api.db import get_db
from api.main import app
from api.models import Call, Order, Reservation
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
        "phone": "0721 5551234",
        "reason": "human_requested",
        "summary": "Möchte mit einem Menschen sprechen.",
        **overrides,
    }


def test_rueckruf_kommt_in_der_huelle_mit_vorlesesatz(client):
    http, tenant_id, call_id = client

    r = http.post(
        "/v1/tools/create_callback", json=body(tenant_id, call_id), headers=AUTH
    )

    assert r.status_code == 200
    antwort = r.json()
    assert antwort["ok"] is True
    assert antwort["say"]
    assert antwort["data"]["status"] == "open"
    assert antwort["data"]["phone"] == "+497215551234"
    assert antwort["data"]["reason"] == "human_requested"
    uuid.UUID(antwort["data"]["callback_id"])


def test_zweiter_aufruf_liefert_denselben_rueckruf(client):
    http, tenant_id, call_id = client
    erster = http.post(
        "/v1/tools/create_callback", json=body(tenant_id, call_id), headers=AUTH
    ).json()

    zweiter = http.post(
        "/v1/tools/create_callback", json=body(tenant_id, call_id), headers=AUTH
    ).json()

    assert zweiter["data"]["callback_id"] == erster["data"]["callback_id"]


def test_ohne_token_401(client):
    http, tenant_id, call_id = client

    r = http.post("/v1/tools/create_callback", json=body(tenant_id, call_id))

    assert r.status_code == 401


def test_unbekannter_anruf_ist_not_found_in_der_huelle(client):
    http, tenant_id, _call_id = client

    r = http.post(
        "/v1/tools/create_callback",
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
        "/v1/tools/create_callback",
        json=body(tenant_id, call_id, reason="kaese"),
        headers=AUTH,
    )

    assert r.json()["ok"] is False
    assert r.json()["error"]["code"] == "invalid_input"
    assert "reason" in r.json()["error"]["message"]


def test_unbrauchbare_rufnummer_fragt_nach(client):
    http, tenant_id, call_id = client

    r = http.post(
        "/v1/tools/create_callback",
        json=body(tenant_id, call_id, phone="123"),
        headers=AUTH,
    )

    assert r.json()["ok"] is False
    assert r.json()["error"]["code"] == "invalid_input"
    assert r.json()["say"]


@pytest.mark.latency
def test_latenz_p95_unter_300_ms(client):
    http, tenant_id, call_id = client

    def call():
        http.post(
            "/v1/tools/create_callback", json=body(tenant_id, call_id), headers=AUTH
        )

    p95 = p95_ms(call)

    assert p95 < 300, f"p95 {p95:.1f} ms über dem Budget"


@pytest.mark.latency
def test_a_new_card_with_the_calls_bookings_stays_in_the_budget(
    client, migrated_db_url
):
    """#764: every new card reads the call's bookings. Each measured call is
    a new card, of a call with a confirmed table and a confirmed order."""
    http, tenant_id, _ = client
    engine = create_engine(migrated_db_url)
    calls = []
    with Session(engine) as s:
        now = datetime.now(UTC)
        for n in range(60):
            call = Call(
                tenant_id=uuid.UUID(tenant_id),
                external_session_id=f"ext-{n}",
                started_at=now,
                delete_after=now.date(),
            )
            s.add(call)
            s.flush()
            s.add_all(
                [
                    Reservation(
                        tenant_id=call.tenant_id,
                        call_id=call.id,
                        status="confirmed",
                        guest_name="Müller",
                        phone="+497215551234",
                        party_size=4,
                        reserved_for=now,
                        idempotency_key=uuid.uuid4().hex,
                    ),
                    Order(
                        tenant_id=call.tenant_id,
                        call_id=call.id,
                        type="pickup",
                        status="confirmed",
                        customer_name="Müller",
                        phone="+497215551234",
                        items_total_cents=1380,
                        total_cents=1380,
                        idempotency_key=uuid.uuid4().hex,
                    ),
                ]
            )
            calls.append(str(call.id))
        s.commit()
    engine.dispose()
    waiting = iter(calls)

    def call():
        r = http.post(
            "/v1/tools/create_callback",
            json=body(tenant_id, next(waiting)),
            headers=AUTH,
        )
        assert "Bereits gebucht:" in r.json()["data"]["summary"]

    p95 = p95_ms(call)

    assert p95 < 300, f"p95 {p95:.1f} ms over the budget"
