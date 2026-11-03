"""gui: Spalte "Neue Bestellungen" mit Passt, Nochmal senden und Korrektur (T-4.7, docs/06 §3)."""

import json
import re
import uuid
from datetime import UTC, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import Session

from api.db import get_db
from api.domain.ordering import draft_order
from api.domain.ordering.correction import RowEdit
from api.events.types import ORDER_CONFIRMED
from api.main import app
from api.models import Address, Customer, ItemOption, Order, OrderItem, OutboxEvent
from api.tests.conftest import p95_ms
from api.tests.test_domain_confirm_order import _confirm, _mode
from api.tests.test_domain_draft_order import NOW, _call, _tenant, item, request
from api.tests.test_domain_ordering_correction import _fix, _rows

HX = {"HX-Request": "true", "X-CSRF-Token": "testtoken"}


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
        client = TestClient(app)
        client.cookies.set("csrf_token", "testtoken")
        yield client
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.fixture
def tenant_id(db) -> uuid.UUID:
    return _tenant(db, "Testbetrieb")


def _order(db, tenant_id, mode="overflow", note=None, order_note=None) -> uuid.UUID:
    _mode(db, tenant_id, mode)
    call_id = _call(db, tenant_id)
    items = [{"menu_item_id": item(db, tenant_id, "23"), "quantity": 2, "note": note}]
    req = request(db, tenant_id, call_id, items, note=order_note)
    order_id = draft_order(db, req, now=NOW).order_id
    _confirm(db, tenant_id, call_id, order_id)
    return order_id


def _delivery(db, tenant_id, order_id, floor_note=None, with_address=True, fee=250):
    """Turn a stored order into a delivery, as T-6.5 will write it."""
    order = db.get(Order, order_id)
    if with_address:
        customer = Customer(tenant_id=tenant_id, phone="+4972215551234", name="Gast")
        db.add(customer)
        db.flush()
        address = Address(
            tenant_id=tenant_id,
            customer_id=customer.id,
            street="Hauptstraße",
            house_number="12a",
            postal_code="76133",
            city="Karlsruhe",
            floor_note=floor_note,
        )
        db.add(address)
        db.flush()
        order.address_id = address.id
    order.type = "delivery"
    order.pickup_code = None
    order.delivery_fee_cents = fee
    order.total_cents = order.items_total_cents + fee
    db.commit()


def _state(html: str) -> str:
    match = re.search(r'name="state" value="([^"]*)"', html)
    assert match, html
    return match.group(1).replace("&#34;", '"').replace("&quot;", '"')


def _mark_url(html: str, order_id) -> str:
    """The "In Kasse eingegeben" target of the card, with the state it shows."""
    match = re.search(rf"/gui/bestellungen/{order_id}/in-kasse/[0-9a-f]{{32}}", html)
    assert match, html
    return match.group(0)


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
    assert "Noch nicht an der Kasse" in html
    assert "Passt, Zettel an die Kasse" in html
    assert 'id="korrektur"' in html


def test_leere_spalte(client, tenant_id):
    html = client.get("/gui/fragments/bestellungen").text
    assert "Noch keine Bestellungen heute." in html


def test_passt_gibt_frei_und_die_karte_wartet_auf_die_kasse(client, db, tenant_id):
    order_id = _order(db, tenant_id)
    assert client.post(f"/gui/bestellungen/{order_id}/passt").status_code == 403

    response = client.post(f"/gui/bestellungen/{order_id}/passt", headers=HX)
    assert response.status_code == 200
    # Approved, not yet typed into the register (T-11.6): the card stays, its
    # button is the mark now.
    assert f'data-id="{order_id}"' in response.text
    assert f"/gui/bestellungen/{order_id}/passt" not in response.text
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
    assert "Zettel nicht gedruckt" in html
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
        data={"state": _state(html), "op": "number", "number": "13"},
        headers=HX,
    ).text
    assert "Pho Bo" in html and "32,60 €" in html

    unknown = client.post(
        f"{base}/vorschau",
        data={"state": _state(html), "op": "number", "number": "99"},
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
        data={"state": _state(html), "op": "number", "number": "47"},
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


def test_korrektur_two_extras_of_one_optional_group_per_tap(client, db, tenant_id):
    """Repo audit of 27.10.2026, finding 8: a tap on an optional choice adds
    or removes that one choice; only a required group keeps one."""
    db.add(
        ItemOption(
            menu_item_id=item(db, tenant_id, "47"),
            group_name="Sauce",
            option_name="Chili",
            price_delta_cents=30,
        )
    )
    db.commit()
    order_id = _order(db, tenant_id)
    base = f"/gui/bestellungen/{order_id}/korrigieren"

    def tap(html: str, op: str) -> str:
        return client.post(
            f"{base}/vorschau", data={"state": _state(html), "op": op}, headers=HX
        ).text

    html = client.get(base).text
    html = client.post(
        f"{base}/vorschau",
        data={"state": _state(html), "op": "number", "number": "47"},
        headers=HX,
    ).text
    # Group 0 is Fleisch (Ente, Huhn), group 1 is Sauce with both choices.
    for op in ("opt:a:0:0:0", "opt:a:0:1:0", "opt:a:0:1:1"):
        html = tap(html, op)
    chosen = {tuple(c) for c in json.loads(_state(html))["a"][0]["o"]}
    assert chosen == {("Fleisch", "Ente"), ("Sauce", "Erdnuss"), ("Sauce", "Chili")}

    html = tap(html, "opt:a:0:1:0")  # the first sauce off again, the other stays

    after = [tuple(c) for c in json.loads(_state(html))["a"][0]["o"]]
    assert ("Fleisch", "Ente") in after
    assert len([c for c in after if c[0] == "Sauce"]) == 1


def test_korrektur_same_number_counts_nothing_and_keeps_the_choice(
    client, db, tenant_id
):
    """Tauschen with the line's own number is how the team reaches its choices.

    Nothing is changed by that alone, but the buttons have to stay: an extra
    on a line without options can be added no other way.
    """
    db.add(
        ItemOption(
            menu_item_id=item(db, tenant_id, "23"),
            group_name="Dip",
            option_name="Chili",
            price_delta_cents=100,
        )
    )
    db.commit()
    order_id = _order(db, tenant_id)
    base = f"/gui/bestellungen/{order_id}/korrigieren"

    def tap(html: str, op: str, number: str = "") -> str:
        data = {"state": _state(html), "op": op, "number": number}
        return client.post(f"{base}/vorschau", data=data, headers=HX).text

    html = tap(client.get(base).text, "swap:0")
    html = tap(html, "number", "23")
    assert "Noch nichts geändert." in html
    assert "getauscht" not in html
    assert "Chili" in html

    html = tap(html, "opt:r:0:0:0")
    assert "Gewählt: Chili" in html
    assert "getauscht" in html
    assert "Noch nichts geändert." not in html

    html = tap(html, "opt:r:0:0:0")  # off again: back at the stored row
    assert "Noch nichts geändert." in html

    html = tap(html, "opt:r:0:0:0")
    saved = client.post(
        base, data={"state": _state(html), "reason": "other"}, headers=HX
    )
    assert saved.status_code == 200
    # 2 x (6,90 + 1,00)
    assert _fresh(db, order_id).total_cents == 1580


def test_card_shows_the_note_of_the_whole_order(client, db, tenant_id):
    """D24: an allergy not tied to a dish stands on the card above the dishes."""
    _order(db, tenant_id, order_note="WICHTIG: Keine Sesam. Grund: Allergie")
    html = client.get("/gui").text
    assert "Für die ganze Bestellung: WICHTIG: Keine Sesam. Grund: Allergie" in html
    assert html.index("ganze Bestellung") < html.index("Frühlingsrollen")


def test_card_without_a_note_of_the_whole_order_has_no_line_for_it(
    client, db, tenant_id
):
    _order(db, tenant_id)
    assert "ganze Bestellung" not in client.get("/gui").text


def test_delivery_card_shows_address_floor_and_fee(client, db, tenant_id):
    order_id = _order(db, tenant_id)
    _delivery(db, tenant_id, order_id, floor_note="3. Stock links")
    html = client.get("/gui").text
    assert "Lieferung" in html
    assert "Hauptstraße 12a, 76133 Karlsruhe" in html
    assert "3. Stock links" in html
    assert "Lieferpauschale 2,50 €" in html
    assert "Abholcode" not in html


def test_delivery_card_without_floor_note_has_no_floor_line(client, db, tenant_id):
    order_id = _order(db, tenant_id)
    _delivery(db, tenant_id, order_id)
    html = client.get("/gui").text
    assert "Hauptstraße 12a, 76133 Karlsruhe" in html
    assert "Stockwerk" not in html


def test_delivery_card_without_address_still_renders(client, db, tenant_id):
    order_id = _order(db, tenant_id)
    _delivery(db, tenant_id, order_id, with_address=False, fee=0)
    html = client.get("/gui").text
    assert f'data-id="{order_id}"' in html
    assert "Adresse fehlt" in html
    assert "Lieferpauschale" not in html


def test_pickup_card_has_no_address_or_fee_line(client, db, tenant_id):
    _order(db, tenant_id)
    html = client.get("/gui").text
    assert (
        "Lieferpauschale" not in html
        and "Adresse fehlt" not in html
        and "Hauptstra" not in html
    )


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


@pytest.mark.latency
def test_spalte_antwortet_schnell(client, db, tenant_id):
    for _ in range(10):
        _order(db, tenant_id)
    p95 = p95_ms(lambda: client.get("/gui/fragments/bestellungen"))
    assert p95 < 300, p95


@pytest.mark.parametrize("swapping", ['"0"', "5", "-1", "true"])
def test_kaputter_tauschen_index_meldet_statt_500(client, db, tenant_id, swapping):
    order_id = _order(db, tenant_id)
    base = f"/gui/bestellungen/{order_id}/korrigieren"
    state = json.loads(_state(client.get(base).text))
    raw = json.dumps(state)[:-1] + f', "t": {swapping}}}'
    raw = raw.replace('"t": null, ', "")
    response = client.post(
        f"{base}/vorschau",
        data={"state": raw, "op": "number", "number": "13"},
        headers=HX,
    )
    assert response.status_code == 409
    assert "durcheinander" in response.text


def test_stand_ohne_zeilen_meldet_statt_500(client, db, tenant_id):
    order_id = _order(db, tenant_id)
    base = f"/gui/bestellungen/{order_id}/korrigieren"
    state = json.loads(_state(client.get(base).text))
    state["r"] = []
    response = client.post(
        f"{base}/vorschau",
        data={"state": json.dumps(state), "op": "number", "number": "13"},
        headers=HX,
    )
    assert response.status_code == 409
    assert "durcheinander" in response.text


# --- In Kasse eingegeben (T-11.6) -------------------------------------------------


def test_after_passt_the_card_asks_for_the_register_mark(client, db, tenant_id):
    order_id = _order(db, tenant_id)
    html = client.get("/gui/fragments/bestellungen").text
    # Waiting for "Passt": nothing to type in yet, so no mark button.
    assert "In Kasse eingegeben</button>" not in html

    html = client.post(f"/gui/bestellungen/{order_id}/passt", headers=HX).text
    assert f'data-id="{order_id}"' in html
    assert "Noch nicht in der Kasse eingegeben" in html
    url = _mark_url(html, order_id)

    assert client.post(url).status_code == 403
    response = client.post(url, headers=HX)
    assert response.status_code == 200
    assert f'data-id="{order_id}"' not in response.text
    assert _fresh(db, order_id).register_entered_at is not None
    # A second tablet with the old page: no error, the fresh column.
    again = client.post(url, headers=HX)
    assert again.status_code == 200 and "Das hat nicht geklappt" not in again.text


def test_mark_with_the_card_from_before_a_correction_says_so(client, db, tenant_id):
    order_id = _order(db, tenant_id)
    html = client.post(f"/gui/bestellungen/{order_id}/passt", headers=HX).text
    stale = _mark_url(html, order_id)
    [row] = _rows(db, order_id)
    _fix(db, tenant_id, order_id, rows=(RowEdit(row.id, 3),))

    html = client.post(stale, headers=HX).text
    assert "inzwischen korrigiert" in html
    assert _mark_url(html, order_id) != stale
    assert _fresh(db, order_id).register_entered_at is None


# --- Storniert (docs/06 §1 rule 6, docs/09 §1 L18) ---------------------------------


def _cancel_button(html: str, order_id) -> tuple[str, str]:
    """The opening tag of the card's button "Bestellung stornieren" and the URL it posts to."""
    match = re.search(
        rf'<button[^>]*hx-post="(/gui/bestellungen/{order_id}/storniert/[0-9a-f]{{32}})"'
        r"[^>]*>Bestellung stornieren</button>",
        html,
    )
    assert match, html
    return match.group(0), match.group(1)


def test_storniert_asks_first_and_the_card_leaves(client, db, tenant_id):
    order_id = _order(db, tenant_id)
    html = client.get("/gui/fragments/bestellungen").text
    button, url = _cancel_button(html, order_id)
    assert 'hx-confirm="Bestellung A1 stornieren?' in button

    assert client.post(url).status_code == 403
    assert _fresh(db, order_id).status == "confirmed"
    response = client.post(url, headers=HX)
    assert response.status_code == 200
    assert f'data-id="{order_id}"' not in response.text
    # The card is gone: the text travels in an event, not in the fragment the
    # refresh right after would replace.
    assert json.loads(response.headers["HX-Trigger"]) == {
        "order-notice": "Bestellung A1: Storniert"
    }
    assert "Storniert" not in response.text
    assert _fresh(db, order_id).status == "cancelled"
    # A second tablet with the old page: no error, the fresh column.
    again = client.post(url, headers=HX)
    assert again.status_code == 200 and "Das hat nicht geklappt" not in again.text


def test_storniert_is_on_every_card_after_the_yes(client, db, tenant_id):
    order_id = _order(db, tenant_id)
    html = client.post(f"/gui/bestellungen/{order_id}/passt", headers=HX).text
    _, url = _cancel_button(html, order_id)
    assert client.post(_mark_url(html, order_id), headers=HX).status_code == 200
    db.execute(
        update(Order).where(Order.id == order_id).values(handover_state="failed")
    )
    db.commit()

    # Marked, and red all the same: the card stands, and so does its button.
    html = client.get("/gui/fragments/bestellungen").text
    assert _cancel_button(html, order_id)[1] == url


def test_storniert_with_the_card_from_before_a_correction_says_so(
    client, db, tenant_id
):
    order_id = _order(db, tenant_id)
    html = client.get("/gui/fragments/bestellungen").text
    _, stale = _cancel_button(html, order_id)
    [row] = _rows(db, order_id)
    _fix(db, tenant_id, order_id, rows=(RowEdit(row.id, 3),))

    html = client.post(stale, headers=HX).text
    assert "inzwischen geändert" in html
    assert _cancel_button(html, order_id)[1] != stale
    assert _fresh(db, order_id).status == "confirmed"


def test_a_tap_on_a_card_another_tablet_cancelled_says_why(client, db, tenant_id):
    order_id = _order(db, tenant_id)
    html = client.get("/gui/fragments/bestellungen").text
    _, url = _cancel_button(html, order_id)
    assert client.post(url, headers=HX).status_code == 200

    html = client.post(f"/gui/bestellungen/{order_id}/passt", headers=HX).text
    assert "inzwischen geändert" in html
    assert f'data-id="{order_id}"' not in html


def test_a_cancelled_card_only_waits_for_its_note(client, db, tenant_id):
    """The note did not print: the card is red until it does, and nothing but
    "Nochmal senden" is left to tap, whatever day it is from."""
    order_id = _order(db, tenant_id)
    db.execute(
        update(Order)
        .where(Order.id == order_id)
        .values(
            status="cancelled",
            handover_state="failed",
            created_at=NOW - timedelta(days=2),
        )
    )
    db.commit()

    html = client.get("/gui/fragments/bestellungen").text
    assert f'data-id="{order_id}"' in html
    assert "Storno-Zettel nicht gedruckt" in html
    assert "Nochmal senden" in html
    for gone in (
        "Passt",
        "Korrigieren",
        "In Kasse eingegeben",
        "Bestellung stornieren",
    ):
        assert gone not in html
    # The state shows on the card now that the order is cancelled.
    assert 'badge accent">Storniert</span>' in html
    assert "in der Kasse" not in html


def test_unmarked_order_of_an_earlier_day_is_red_with_its_day(client, db, tenant_id):
    order_id = _order(db, tenant_id)
    client.post(f"/gui/bestellungen/{order_id}/passt", headers=HX)
    db.execute(
        update(Order)
        .where(Order.id == order_id)
        .values(created_at=NOW - timedelta(days=2))
    )
    db.commit()

    html = client.get("/gui/fragments/bestellungen").text
    assert "Vom 13.09., nicht als in der Kasse markiert" in html
    assert re.search(rf'class="card order overdue"\s+data-id="{order_id}"', html)
    # Red by the day, not by a missed "Passt": the tablet rings no alarm for it.
    assert "data-overdue" not in html


def test_a_card_taken_after_midnight_names_its_business_day(client, db, tenant_id):
    """00:30 belongs to the business day before (05:00), as in the daily push."""
    order_id = _order(db, tenant_id)
    client.post(f"/gui/bestellungen/{order_id}/passt", headers=HX)
    db.execute(
        update(Order)
        .where(Order.id == order_id)
        .values(created_at=NOW.replace(hour=0, minute=30))
    )
    db.commit()

    html = client.get("/gui/fragments/bestellungen").text
    assert "Vom 14.09., nicht als in der Kasse markiert" in html
