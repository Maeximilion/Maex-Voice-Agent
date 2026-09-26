"""gui: "Gericht aus" am Tablet (T-4.8, docs/06 §3)."""

import uuid
from datetime import UTC

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from api.db import get_db
from api.main import app
from api.models import MenuItem
from api.tests.conftest import p95_ms
from api.tests.test_domain_draft_order import NOW, _tenant, item

HX = {"HX-Request": "true"}


@pytest.fixture(autouse=True)
def feste_uhr(monkeypatch):
    monkeypatch.setattr("api.core.time.utcnow", lambda: NOW.astimezone(UTC))


@pytest.fixture
def engine(migrated_db_url):
    engine = create_engine(migrated_db_url)
    yield engine
    engine.dispose()


@pytest.fixture
def db(engine):
    with Session(engine) as s:
        yield s


@pytest.fixture
def client(engine):
    def override_get_db():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.fixture
def tenant_id(db) -> uuid.UUID:
    return _tenant(db, "Testbetrieb")


def _sold_out(db, item_id) -> bool:
    db.expire_all()
    until = db.scalar(select(MenuItem.sold_out_until).where(MenuItem.id == item_id))
    return until is not None and until > NOW


def test_kachel_in_der_kopfzeile(client, tenant_id):
    html = client.get("/gui/").text
    assert 'hx-get="/gui/gericht-aus"' in html
    assert 'id="gericht-aus"' in html


def test_kasten_zeigt_aktive_gerichte(client, tenant_id):
    html = client.get("/gui/gericht-aus").text
    assert 'id="gericht-suche"' in html
    assert "Frühlingsrollen" in html and "Pho Bo" in html
    assert "Altes Gericht" not in html


def test_suchfeld_filtert(client, tenant_id):
    html = client.get("/gui/fragments/gericht-aus", params={"q": "pho"}).text
    assert "Pho Bo" in html
    assert "Frühlingsrollen" not in html
    assert (
        "Kein Gericht gefunden"
        in client.get("/gui/fragments/gericht-aus", params={"q": "Pizza"}).text
    )


def test_ein_tap_heute_aus_und_zurueck(client, db, tenant_id):
    rolls = item(db, tenant_id, "23")
    response = client.post(f"/gui/gericht-aus/{rolls}/aus", headers=HX, data={"q": ""})
    assert response.status_code == 200
    assert response.headers["HX-Trigger"] == "gerichte-geaendert"
    assert "heute aus" in response.text and "Wieder da" in response.text
    assert _sold_out(db, rolls)
    assert "1 heute aus" in client.get("/gui/fragments/kopfzeile").text

    back = client.post(f"/gui/gericht-aus/{rolls}/wieder-da", headers=HX)
    assert back.status_code == 200
    assert not _sold_out(db, rolls)
    assert "heute aus" not in client.get("/gui/fragments/kopfzeile").text


def test_tap_behaelt_die_suche(client, db, tenant_id):
    rolls = item(db, tenant_id, "23")
    html = client.post(
        f"/gui/gericht-aus/{rolls}/aus", headers=HX, data={"q": "rollen"}
    ).text
    assert "Frühlingsrollen" in html
    assert "Pho Bo" not in html


def test_ohne_htmx_kein_schalten(client, db, tenant_id):
    rolls = item(db, tenant_id, "23")
    assert client.post(f"/gui/gericht-aus/{rolls}/aus").status_code == 403
    assert not _sold_out(db, rolls)


@pytest.mark.parametrize("state", ["weg", "AUS", ""])
def test_unbekannter_schalter(client, db, tenant_id, state):
    rolls = item(db, tenant_id, "23")
    response = client.post(f"/gui/gericht-aus/{rolls}/{state}", headers=HX)
    assert response.status_code in (404, 405)


def test_unbekanntes_gericht_zeigt_die_liste(client, tenant_id):
    """Ein anderes Tablet oder ein Import war schneller: kein Fehler, frische Liste."""
    response = client.post(f"/gui/gericht-aus/{uuid.uuid4()}/aus", headers=HX)
    assert response.status_code == 200
    assert "Pho Bo" in response.text
    assert "nicht geklappt" not in response.text


def test_schalter_antwortet_schnell(client, db, tenant_id):
    rolls = item(db, tenant_id, "23")
    p95 = p95_ms(lambda: client.post(f"/gui/gericht-aus/{rolls}/aus", headers=HX))
    assert p95 < 300, p95
