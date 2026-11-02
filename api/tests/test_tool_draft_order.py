"""HTTP-Hülle für draft_order: Hülle, Fehler als JSON, Idempotenz über HTTP, Latenz."""

import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from api.config import settings
from api.db import get_db
from api.domain.menu.importer import apply, parse
from api.domain.ordering import draft
from api.main import app
from api.models import Call, DeliveryZone, MenuItem
from api.tests.conftest import p95_ms
from api.tests.test_domain_draft_order import KARTE
from scripts.seed import seed

AUTH = {"Authorization": f"Bearer {settings.agent_api_token}"}
# Dienstag, Abholung offen. Das Tool nimmt die echte Uhr, der Test hält sie fest.
NOW = datetime(2026, 9, 15, 18, 0, tzinfo=ZoneInfo("Europe/Berlin"))


@pytest.fixture
def client(migrated_db_url, monkeypatch):
    monkeypatch.setattr(draft, "utcnow", lambda: NOW)
    engine = create_engine(migrated_db_url)
    with Session(engine) as s:
        tenant_id = uuid.UUID(
            seed(s, tenant_name="Testbetrieb", timezone="Europe/Berlin").tenant_id
        )
        apply(s, tenant_id, parse(KARTE), now=NOW)
        call = Call(
            tenant_id=tenant_id,
            external_session_id="ext",
            started_at=NOW,
            delete_after=NOW.date(),
        )
        s.add(call)
        s.commit()
        ids = dict(
            s.execute(
                select(MenuItem.number, MenuItem.id).where(
                    MenuItem.tenant_id == tenant_id
                )
            ).all()
        )
        call_id = call.id

    def override_get_db():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield TestClient(app), str(tenant_id), str(call_id), ids
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def _body(tenant_id, call_id, ids, **overrides) -> dict:
    return {
        "call_id": call_id,
        "tenant_id": tenant_id,
        "idempotency_key": uuid.uuid4().hex,
        "type": "pickup",
        "customer": {"name": "Müller", "phone": "+4972215551234"},
        "items": [{"menu_item_id": str(ids["23"]), "quantity": 2}],
        **overrides,
    }


def test_entwurf_in_der_huelle(client):
    http, tenant_id, call_id, ids = client
    res = http.post(
        "/v1/tools/draft_order", json=_body(tenant_id, call_id, ids), headers=AUTH
    )
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True and body["say"] is None
    assert body["data"]["total_cents"] == 1380
    assert body["data"]["readback"].startswith("Zweimal Nummer 23 Frühlingsrollen.")


def test_idempotenz_ueber_http(client):
    http, tenant_id, call_id, ids = client
    body = _body(tenant_id, call_id, ids)
    first = http.post("/v1/tools/draft_order", json=body, headers=AUTH).json()
    second = http.post("/v1/tools/draft_order", json=body, headers=AUTH).json()
    assert first == second


def test_pflichtwahl_fehlt_als_json_mit_say(client):
    http, tenant_id, call_id, ids = client
    body = _body(
        tenant_id, call_id, ids, items=[{"menu_item_id": str(ids["47"]), "quantity": 1}]
    )
    res = http.post("/v1/tools/draft_order", json=body, headers=AUTH).json()
    assert res["ok"] is False and res["error"]["code"] == "invalid_input"
    assert res["say"] == "Welche Auswahl bei Fleisch möchten Sie zu Ente knusprig?"


@pytest.mark.parametrize(
    "kaputt",
    [
        {"items": []},
        {"items": [{"menu_item_id": "x", "quantity": 1}]},
        {"idempotency_key": ""},
        {"type": "dinein"},
    ],
)
def test_ungueltiger_request(client, kaputt):
    http, tenant_id, call_id, ids = client
    res = http.post(
        "/v1/tools/draft_order",
        json=_body(tenant_id, call_id, ids, **kaputt),
        headers=AUTH,
    ).json()
    assert res["ok"] is False and res["error"]["code"] == "invalid_input"


def test_menge_ueber_grenze(client):
    http, tenant_id, call_id, ids = client
    items = [{"menu_item_id": str(ids["23"]), "quantity": 31}]
    res = http.post(
        "/v1/tools/draft_order",
        json=_body(tenant_id, call_id, ids, items=items),
        headers=AUTH,
    ).json()
    assert res["error"]["code"] == "invalid_input"


def test_ohne_token(client):
    http, tenant_id, call_id, ids = client
    res = http.post("/v1/tools/draft_order", json=_body(tenant_id, call_id, ids))
    assert res.status_code == 401


@pytest.mark.latency
def test_latenz(client):
    http, tenant_id, call_id, ids = client
    items = [
        {"menu_item_id": str(ids["23"]), "quantity": 2},
        {
            "menu_item_id": str(ids["47"]),
            "quantity": 1,
            "options": [{"group": "Fleisch", "name": "Huhn"}],
        },
    ]

    def call():
        body = _body(tenant_id, call_id, ids, items=items)
        assert http.post("/v1/tools/draft_order", json=body, headers=AUTH).json()["ok"]

    assert p95_ms(call) < 300


def _confirm_body(tenant_id, call_id, order_id) -> dict:
    return {
        "call_id": call_id,
        "tenant_id": tenant_id,
        "entity": "order",
        "entity_id": order_id,
        "idempotency_key": uuid.uuid4().hex,
    }


def test_entwurf_bestaetigen_ueber_http(client):
    """Seed laeuft im Modus shadow: bestaetigt, Code vergeben, Freigabe steht aus."""
    http, tenant_id, call_id, ids = client
    order_id = http.post(
        "/v1/tools/draft_order", json=_body(tenant_id, call_id, ids), headers=AUTH
    ).json()["data"]["order_id"]
    res = http.post(
        "/v1/tools/confirm",
        json=_confirm_body(tenant_id, call_id, order_id),
        headers=AUTH,
    ).json()
    assert res == {
        "ok": True,
        "data": {
            "status": "confirmed",
            "handover": "awaiting_approval",
            "pickup_code": "A1",
        },
        "say": None,
    }


@pytest.mark.latency
def test_latenz_confirm_bestellung(client):
    http, tenant_id, call_id, ids = client
    drafts = iter(
        http.post(
            "/v1/tools/draft_order", json=_body(tenant_id, call_id, ids), headers=AUTH
        ).json()["data"]["order_id"]
        for _ in range(60)
    )

    def call():
        body = _confirm_body(tenant_id, call_id, next(drafts))
        assert http.post("/v1/tools/confirm", json=body, headers=AUTH).json()["ok"]

    assert p95_ms(call) < 300


def _zone(tenant_id: str) -> None:
    with next(app.dependency_overrides[get_db]()) as session:
        session.add(
            DeliveryZone(
                tenant_id=uuid.UUID(tenant_id),
                name="Zone 1",
                match_type="postal_code",
                postal_codes=["12345"],
                fee_cents=250,
                min_order_cents=1000,
                eta_minutes=50,
            )
        )
        session.commit()


def _delivery(tenant_id, call_id, ids) -> dict:
    body = _body(tenant_id, call_id, ids)
    body["type"] = "delivery"
    body["customer"] = {
        **body["customer"],
        "address": {
            "street": "Lindenstraße",
            "house_number": "12",
            "postal_code": "12345",
            "city": "Musterdorf",
        },
    }
    return body


def test_lieferung_ueber_http(client):
    http, tenant_id, call_id, ids = client
    _zone(tenant_id)

    answer = http.post(
        "/v1/tools/draft_order", json=_delivery(tenant_id, call_id, ids), headers=AUTH
    ).json()

    assert answer["ok"] is True
    assert answer["data"]["delivery_fee_cents"] == 250
    assert answer["data"]["total_cents"] == answer["data"]["items_total_cents"] + 250


def test_mindestbestellwert_ueber_http(client):
    http, tenant_id, call_id, ids = client
    _zone(tenant_id)
    body = _delivery(tenant_id, call_id, ids)
    body["items"] = [{"menu_item_id": str(ids["23"]), "quantity": 1}]

    answer = http.post("/v1/tools/draft_order", json=body, headers=AUTH).json()

    assert answer["ok"] is False
    assert answer["error"]["code"] == "below_minimum"
    assert "3 Euro 10" in answer["say"]


def test_ausserhalb_der_zone_ueber_http(client):
    http, tenant_id, call_id, ids = client
    _zone(tenant_id)
    body = _delivery(tenant_id, call_id, ids)
    body["customer"]["address"]["postal_code"] = "54321"

    answer = http.post("/v1/tools/draft_order", json=body, headers=AUTH).json()

    assert answer["ok"] is False
    assert answer["error"]["code"] == "out_of_zone"


@pytest.mark.latency
def test_latenz_lieferung(client):
    http, tenant_id, call_id, ids = client
    _zone(tenant_id)

    def call():
        body = _delivery(tenant_id, call_id, ids)
        assert http.post("/v1/tools/draft_order", json=body, headers=AUTH).json()["ok"]

    measured = p95_ms(call)
    print(f"draft_order delivery p95 {measured:.1f} ms")
    assert measured < 300


@pytest.mark.latency
def test_latenz_confirm_lieferung(client):
    """The yes of a delivery: order, customer counters, default address, event."""
    http, tenant_id, call_id, ids = client
    _zone(tenant_id)
    drafts = iter(
        http.post(
            "/v1/tools/draft_order",
            json=_delivery(tenant_id, call_id, ids),
            headers=AUTH,
        ).json()["data"]["order_id"]
        for _ in range(60)
    )

    def call():
        body = _confirm_body(tenant_id, call_id, next(drafts))
        assert http.post("/v1/tools/confirm", json=body, headers=AUTH).json()["ok"]

    measured = p95_ms(call)
    print(f"confirm delivery p95 {measured:.1f} ms")
    assert measured < 300
