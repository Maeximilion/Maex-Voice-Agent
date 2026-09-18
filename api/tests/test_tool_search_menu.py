"""HTTP-Hülle für search_menu und get_item_details: Hülle, Validierung, Latenz (docs/04 §1)."""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from api.config import settings
from api.db import get_db
from api.main import app
from api.tests.conftest import p95_ms
from api.tests.test_domain_menu_search import build_menu
from scripts.seed import seed

AUTH = {"Authorization": f"Bearer {settings.agent_api_token}"}


@pytest.fixture
def client(migrated_db_url):
    engine = create_engine(migrated_db_url)
    with Session(engine) as s:
        tenant_id = uuid.UUID(
            seed(s, tenant_name="Testbetrieb", timezone="Europe/Berlin").tenant_id
        )
        items = build_menu(s, tenant_id)
        ids = {number: item.id for number, item in items.items()}

    def override_get_db():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield TestClient(app), str(tenant_id), ids
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def _search(client: TestClient, tenant_id: str, **overrides):
    body = {
        "call_id": str(uuid.uuid4()),
        "tenant_id": tenant_id,
        "query": "einmal die Nummer dreiundzwanzig",
        **overrides,
    }
    return client.post("/v1/tools/search_menu", json=body, headers=AUTH)


def _details(client: TestClient, tenant_id: str, menu_item_id, **overrides):
    body = {
        "call_id": str(uuid.uuid4()),
        "tenant_id": tenant_id,
        "menu_item_id": str(menu_item_id),
        **overrides,
    }
    return client.post("/v1/tools/get_item_details", json=body, headers=AUTH)


def test_antwort_folgt_der_huelle(client):
    http, tenant_id, ids = client
    r = _search(http, tenant_id)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["say"] is None
    assert set(body["data"]) == {"match_type", "results"}
    hit = body["data"]["results"][0]
    assert set(hit) == {
        "menu_item_id",
        "number",
        "name",
        "price_cents",
        "sold_out",
        "option_groups",
    }
    assert hit["menu_item_id"] == str(ids["23"])


def test_ohne_treffer_kommt_not_found_in_der_huelle(client):
    http, tenant_id, _ = client
    r = _search(http, tenant_id, query="Pizza Salami")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "not_found"


def test_ohne_token_kein_zugriff(client):
    http, tenant_id, _ = client
    r = http.post(
        "/v1/tools/search_menu",
        json={"call_id": str(uuid.uuid4()), "tenant_id": tenant_id, "query": "23"},
    )
    assert r.status_code == 401


def test_leere_query_ist_invalid_input(client):
    http, tenant_id, _ = client
    r = _search(http, tenant_id, query="")
    assert r.json()["ok"] is False
    assert r.json()["error"]["code"] == "invalid_input"


def test_mehr_als_drei_vorschlaege_werden_abgelehnt(client):
    http, tenant_id, _ = client
    r = _search(http, tenant_id, max_results=9)
    assert r.json()["error"]["code"] == "invalid_input"
    assert "max_results" in r.json()["error"]["message"]


def test_details_liefern_allergene_und_say(client):
    http, tenant_id, ids = client
    r = _details(http, tenant_id, ids["40"])
    body = r.json()
    assert body["ok"] is True
    assert body["data"]["allergens"] == {
        "known": False,
        "codes": [],
        "confirmed_at": None,
    }
    assert body["say"] == "Das lasse ich Ihnen vom Team bestätigen."


def test_details_zu_unbekannter_id_sind_not_found(client):
    http, tenant_id, _ = client
    r = _details(http, tenant_id, uuid.uuid4())
    assert r.json()["error"]["code"] == "not_found"


def test_latenz_search_menu_p95_unter_300_ms(client):
    http, tenant_id, _ = client
    p95 = p95_ms(lambda: _search(http, tenant_id, query="Frühlingsrollen"), n=20)
    print(f"\nsearch_menu p95 = {p95:.1f} ms")
    assert p95 < 300, f"p95 {p95:.1f} ms über dem Budget aus docs/04 §1"


def test_latenz_get_item_details_p95_unter_300_ms(client):
    http, tenant_id, ids = client
    p95 = p95_ms(lambda: _details(http, tenant_id, ids["23"]), n=20)
    print(f"\nget_item_details p95 = {p95:.1f} ms")
    assert p95 < 300, f"p95 {p95:.1f} ms über dem Budget aus docs/04 §1"
