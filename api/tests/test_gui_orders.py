"""gui: Spalte "Neue Bestellungen" mit Passt, Nochmal senden und Korrektur (T-4.7, docs/06 §3)."""

import json
import re
import uuid
from datetime import UTC

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import Session

from api.db import get_db
from api.domain.ordering import draft_order
from api.events.types import ORDER_CONFIRMED
from api.main import app
from api.models import Order, OrderItem, OutboxEvent
from api.tests.conftest import p95_ms
from api.tests.test_domain_confirm_order import _confirm, _mode
from api.tests.test_domain_draft_order import NOW, _call, _tenant, item, request

HX = {"HX-Request": "true"}


@pytest.fixture(autouse=True)
def feste_uhr(monkeypatch):
    """Die Spalte zeigt den Betriebstag von "jetzt": jetzt ist der Testabend."""
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


def _order(db, tenant_id, mode="overflow", note=None) -> uuid.UUID:
    _mode(db, tenant_id, mode)
    call_id = _call(db, tenant_id)
    items = [{"menu_item_id": item(db, tenant_id, "23"), "quantity": 2, "note": note}]
    order_id = draft_order(db, request(db, tenant_id, call_id, items), now=NOW).order_id
    _confirm(db, tenant_id, call_id, order_id)
    return order_id


def _state(html: str) -> str:
    match = re.search(r'name="state" value="([^"]*)"', html)
    assert match, html
    return match.group(1).replace("&#34;", '"').replace("&quot;", '"')


def _fresh(db, order_id) -> Order:
    order = db.get(Order, order_id)
    db.refresh(order)
    return order


# --- Spalte ----------------------------------------------------------------------


def test_seite_zeigt_wartende_bestellung_mit_freigabe(client, db, tenant_id):
    order_id = _order(db, tenant_id, note="ohne Zwiebeln")
    html = client.get("/gui").text

    assert f'data-id="{order_id}"' in html
    assert ">A1<" in html  # Abholcode gross
    assert "Frühlingsrollen" in html and "ohne Zwiebeln" in html
    assert "13,80 €" in html
    assert "Noch nicht in der Küche" in html
    assert "Passt, ab in die Küche" in html
    assert 'id="korrektur"' in html


def test_leere_spalte(client, tenant_id):
    html = client.get("/gui/fragments/bestellungen").text
    assert "Noch keine Bestellungen heute." in html


def test_passt_gibt_frei_und_nimmt_die_karte_weg(client, db, tenant_id):
    order_id = _order(db, tenant_id)
    assert client.post(f"/gui/bestellungen/{order_id}/passt").status_code == 403

    response = client.post(f"/gui/bestellungen/{order_id}/passt", headers=HX)
    assert response.status_code == 200
    assert f'data-id="{order_id}"' not in response.text
    assert _fresh(db, order_id).status == "approved"
    events = db.scalars(
        select(OutboxEvent).where(OutboxEvent.event_type == ORDER_CONFIRMED)
    ).all()
    assert len(events) == 1

    # Zweites Tablet mit alter Seite: kein Fehler, frische Spalte.
    again = client.post(f"/gui/bestellungen/{order_id}/passt", headers=HX)
    assert again.status_code == 200 and "Das hat nicht geklappt" not in again.text
    unknown = client.post(f"/gui/bestellungen/{uuid.uuid4()}/passt", headers=HX)
    assert unknown.status_code == 200


def test_rote_karte_hat_nochmal_senden_statt_passt(client, db, tenant_id):
    order_id = _order(db, tenant_id, mode="primary")
    db.execute(
        update(Order).where(Order.id == order_id).values(handover_state="failed")
    )
    db.commit()
    html = client.get("/gui/fragments/bestellungen").text
    assert "Küche nicht erreicht" in html
    assert "Nochmal senden" in html
    assert f"/gui/bestellungen/{order_id}/passt" not in html

    response = client.post(f"/gui/bestellungen/{order_id}/nochmal-senden", headers=HX)
    assert response.status_code == 200
    assert _fresh(db, order_id).handover_state == "pending"


# --- Korrektur -------------------------------------------------------------------


def test_korrektur_menge_tauschen_und_speichern(client, db, tenant_id):
    order_id = _order(db, tenant_id)
    base = f"/gui/bestellungen/{order_id}/korrigieren"
    html = client.get(base).text
    assert "Bestellung korrigieren" in html
    # Ohne Aenderung kein Grund waehlbar, und das steht als Text da.
    assert "Noch nichts geändert." in html
    # Adresse gibt es bei Abholung nicht.
    assert "falsche Adresse" not in html

    html = client.post(
        f"{base}/vorschau", data={"state": _state(html), "op": "plus:r:0"}, headers=HX
    ).text
    assert "3&times;" in html and "20,70 €" in html

    html = client.post(
        f"{base}/vorschau",
        data={"state": _state(html), "op": "nummer", "nummer": "13"},
        headers=HX,
    ).text
    assert "Pho Bo" in html and "32,60 €" in html

    unknown = client.post(
        f"{base}/vorschau",
        data={"state": _state(html), "op": "nummer", "nummer": "99"},
        headers=HX,
    )
    assert "Nummer 99 gibt es nicht auf der Karte." in unknown.text

    saved = client.post(
        base, data={"state": _state(html), "reason": "wrong_quantity"}, headers=HX
    )
    assert saved.status_code == 200
    assert saved.headers["HX-Trigger"] == "bestellungen-geaendert"
    assert _fresh(db, order_id).total_cents == 3260
    card = client.get("/gui/fragments/bestellungen").text
    assert "Korrigiert: falsche Menge" in card and "Pho Bo" in card


def test_korrektur_pflichtauswahl_per_tap(client, db, tenant_id):
    order_id = _order(db, tenant_id)
    base = f"/gui/bestellungen/{order_id}/korrigieren"
    html = client.get(base).text
    html = client.post(
        f"{base}/vorschau",
        data={"state": _state(html), "op": "nummer", "nummer": "47"},
        headers=HX,
    ).text
    assert "Fehlt noch: Fleisch" in html
    # Fleisch ist die erste Gruppe (Ente, Huhn), Huhn die zweite Option.
    html = client.post(
        f"{base}/vorschau",
        data={"state": _state(html), "op": "opt:a:0:0:1"},
        headers=HX,
    ).text
    assert "Gewählt: Huhn" in html
    assert "Fehlt noch" not in html
    assert json.loads(_state(html))["a"][0]["o"] == [["Fleisch", "Huhn"]]


def test_hinweis_zeile_faellt_nicht_mit_minus_weg(client, db, tenant_id):
    order_id = _order(db, tenant_id, note="WICHTIG: Keine Erdnüsse. Grund: Allergie")
    base = f"/gui/bestellungen/{order_id}/korrigieren"
    html = client.get(base).text
    for _ in range(3):
        html = client.post(
            f"{base}/vorschau",
            data={"state": _state(html), "op": "minus:r:0"},
            headers=HX,
        ).text
    # Minus stoppt bei 1; Entfernen geht nur mit Rueckfrage.
    assert json.loads(_state(html))["r"][0]["q"] == 1
    assert "Der Hinweis fällt mit weg" in html


def test_veralteter_stand_und_kaputter_stand(client, db, tenant_id):
    order_id = _order(db, tenant_id)
    base = f"/gui/bestellungen/{order_id}/korrigieren"
    html = client.get(base).text
    state = _state(html)
    db.execute(
        update(OrderItem).where(OrderItem.order_id == order_id).values(quantity=5)
    )
    db.commit()

    stale = client.post(base, data={"state": state, "reason": "other"}, headers=HX)
    assert stale.status_code == 409
    assert "inzwischen geändert" in stale.text
    broken = client.post(
        f"{base}/vorschau", data={"state": "{kaputt", "op": "plus:r:0"}, headers=HX
    )
    assert broken.status_code == 409
    assert "durcheinander" in broken.text
    assert _fresh(db, order_id).total_cents == 1380


def test_spalte_antwortet_schnell(client, db, tenant_id):
    for _ in range(10):
        _order(db, tenant_id)
    p95 = p95_ms(lambda: client.get("/gui/fragments/bestellungen"))
    assert p95 < 300, p95
