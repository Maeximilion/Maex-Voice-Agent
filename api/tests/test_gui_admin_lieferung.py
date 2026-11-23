"""The tab "Lieferung" of the admin area (#571, plan number 8): the delivery
zones with their postal codes, fee, minimum order and delivery time behind the
admin PIN. These tests act as a signed-in device with the admin PIN
(conftest); the refusal without it is in test_gui_login.py."""

import re
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import Session

from api.db import get_db
from api.domain.delivery import owner_zones
from api.domain.delivery.zones import check_delivery
from api.main import app
from api.models import AuditLog, DeliveryZone, Tenant
from api.tests.test_gui_admin_zeiten import _described, _fields, _tag
from scripts.seed import seed

HX = {"HX-Request": "true", "X-CSRF-Token": "testtoken"}
PAGE = "/gui/lieferung"
NEW = f"{PAGE}/neu"
ZONE = {
    "name": "Zone 1",
    "plz": "76530, 76532",
    "pauschale": "2,50",
    "mindestwert": "15",
    "lieferzeit": "40",
    "aktiv": "1",
}


@pytest.fixture
def engine(migrated_db_url):
    engine = create_engine(migrated_db_url)
    with Session(engine) as s:
        seed(s, tenant_name="Testbetrieb", timezone="Europe/Berlin")
    yield engine
    engine.dispose()


@pytest.fixture
def client(engine):
    def override_get_db():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app)
    client.cookies.set("maex_csrf", "testtoken")
    try:
        yield client
    finally:
        app.dependency_overrides.pop(get_db, None)


def _tenant_id(engine) -> uuid.UUID:
    with Session(engine) as session:
        return session.scalars(select(Tenant.id)).one()


def _zones(engine) -> list[DeliveryZone]:
    with Session(engine) as session:
        return owner_zones.zones(session, _tenant_id(engine))


def _fee(engine, code) -> int:
    with Session(engine) as session:
        return check_delivery(session, _tenant_id(engine), code).fee_cents


def _audits(engine) -> list[AuditLog]:
    with Session(engine) as session:
        return list(
            session.scalars(
                select(AuditLog)
                .where(AuditLog.entity == "delivery_zones")
                .order_by(AuditLog.id)
            )
        )


def _create(client, **changes) -> None:
    answer = client.post(NEW, data={**ZONE, **changes}, headers=HX)
    assert answer.status_code == 200 and "Gespeichert" in answer.text, answer.text


def _edit(client, zone) -> dict[str, str]:
    form = client.get(f"{PAGE}/zone/{zone.id}", headers=HX)
    assert form.status_code == 200
    fields = _fields(form.text)
    # The helper reads the markup with patterns: a form it no longer
    # matches must not pass as an empty one.
    assert {"gesehen", "name", "plz", "pauschale"} <= set(fields)
    return fields


# --- the page ---------------------------------------------------------------------


def test_the_page_shows_the_frame_and_the_zones_from_the_database(client, engine):
    _create(client)
    _create(client, name="Zone 2", plz="76534", pauschale="0", aktiv="")
    page = client.get(PAGE)
    assert page.status_code == 200
    assert page.headers["Cache-Control"] == "no-store"
    assert '<a href="/gui/lieferung" aria-current="page">Lieferung</a>' in page.text
    for tab in ("/gui/zeiten", "/gui/anrufe", "/gui/kennzahlen", "/gui/geraete"):
        assert f'href="{tab}"' in page.text
    for shown in (
        "Zone 1",
        "76530, 76532",
        "2,50 €",
        "15,00 €",
        "40 Min",
        "Zone 2",
        "0,00 €",
    ):
        assert shown in page.text, shown
    assert "aus" in page.text and "Inhaber-Bereich schließen" in page.text
    assert "Keine aktive Lieferzone" not in page.text


def test_without_an_active_zone_the_page_says_what_callers_hear(client, engine):
    page = client.get(PAGE)
    assert page.status_code == 200
    assert "Noch keine Lieferzone" in page.text
    assert "Keine aktive Lieferzone" in page.text
    assert "nimmt die KI keine Lieferung an" in page.text
    _create(client, aktiv="")
    assert "Keine aktive Lieferzone" in client.get(PAGE).text
    _create(client, name="Zone 2", plz="76534")
    assert "Keine aktive Lieferzone" not in client.get(PAGE).text


# --- a new zone -------------------------------------------------------------------


def test_the_empty_form_saves_a_new_zone(client, engine):
    form = client.get(NEW, headers=HX)
    assert form.status_code == 200 and form.headers["Cache-Control"] == "no-store"
    fields = _fields(form.text)
    assert set(fields) == {
        "gesehen",
        "name",
        "plz",
        "pauschale",
        "mindestwert",
        "lieferzeit",
        "aktiv",
    }
    answer = client.post(NEW, data={**fields, **ZONE}, headers=HX)
    assert answer.status_code == 200 and "Gespeichert" in answer.text
    assert answer.headers["Cache-Control"] == "no-store"
    assert "2,50 €" in answer.text
    (zone,) = _zones(engine)
    assert (zone.name, zone.postal_codes, zone.active) == (
        "Zone 1",
        ["76530", "76532"],
        True,
    )
    assert (zone.fee_cents, zone.min_order_cents, zone.eta_minutes) == (250, 1500, 40)
    assert _fee(engine, "76530") == 250
    (audit,) = _audits(engine)
    assert (audit.action, audit.actor) == ("delivery_zone.created", "gui:owner")

    # The same form sent twice (a double tap) is one zone.
    again = client.post(NEW, data=ZONE, headers=HX)
    assert again.status_code == 200 and "Keine Änderung" in again.text
    assert len(_zones(engine)) == 1


def test_a_refused_form_shows_what_was_typed_and_its_reason(client, engine):
    typed = {**ZONE, "pauschale": "2.50", "name": "Zone <b>1</b>"}
    answer = client.post(NEW, data=typed, headers=HX)
    assert answer.status_code == 200 and 'role="alert"' in answer.text
    assert answer.headers["Cache-Control"] == "no-store"
    assert "Lieferpauschale" in answer.text
    assert _zones(engine) == [] and _audits(engine) == []
    fields = _fields(answer.text)
    assert fields["pauschale"] == "2.50" and fields["plz"] == "76530, 76532"
    assert fields["aktiv"] == "1"
    # Typed text is shown as text, in a field and in a message.
    assert "<b>1</b>" not in answer.text and "Zone &lt;b&gt;1&lt;/b&gt;" in answer.text
    answer = client.post(NEW, data={**ZONE, "plz": "<i>7</i>"}, headers=HX)
    assert "fünf Ziffern" in answer.text and "<i>7</i>" not in answer.text


def test_a_refused_form_echoes_no_more_than_a_field_holds(client, engine):
    answer = client.post(
        NEW,
        data={**ZONE, "pauschale": "9" * 500, "plz": "7" * 20000, "name": "n" * 500},
        headers=HX,
    )
    assert answer.status_code == 200 and 'role="alert"' in answer.text
    fields = _fields(answer.text)
    assert len(fields["pauschale"]) <= 12 and len(fields["name"]) <= 60
    assert len(fields["plz"]) <= 8000


# --- a change ---------------------------------------------------------------------


def test_an_untouched_form_changes_nothing(client, engine):
    _create(client)
    (zone,) = _zones(engine)
    fields = _edit(client, zone)
    assert fields["name"] == "Zone 1" and fields["plz"] == "76530, 76532"
    assert (fields["pauschale"], fields["mindestwert"]) == ("2,50", "15,00")
    assert fields["lieferzeit"] == "40" and fields["aktiv"] == "1"
    answer = client.post(f"{PAGE}/zone/{zone.id}", data=fields, headers=HX)
    assert answer.status_code == 200 and "Keine Änderung" in answer.text
    assert len(_audits(engine)) == 1


def test_a_change_is_saved_and_holds_for_the_next_call(client, engine):
    _create(client)
    (zone,) = _zones(engine)
    fields = {**_edit(client, zone), "pauschale": "3,90", "plz": "76530 76534"}
    answer = client.post(f"{PAGE}/zone/{zone.id}", data=fields, headers=HX)
    assert answer.status_code == 200 and "Gespeichert" in answer.text
    assert "3,90 €" in answer.text and "76530, 76534" in answer.text
    assert _fee(engine, "76534") == 390
    assert [a.action for a in _audits(engine)] == [
        "delivery_zone.created",
        "delivery_zone.changed",
    ]


def test_a_zone_is_switched_off_by_its_box(client, engine):
    _create(client)
    (zone,) = _zones(engine)
    fields = _edit(client, zone)
    del fields["aktiv"]
    answer = client.post(f"{PAGE}/zone/{zone.id}", data=fields, headers=HX)
    assert answer.status_code == 200 and "Gespeichert" in answer.text
    assert "Keine aktive Lieferzone" in answer.text
    (zone,) = _zones(engine)
    assert zone.active is False
    assert "aktiv" not in _edit(client, zone)


def test_a_refused_change_keeps_the_token_it_was_typed_against(client, engine):
    _create(client)
    (zone,) = _zones(engine)
    fields = _edit(client, zone)
    refused = client.post(
        f"{PAGE}/zone/{zone.id}", data={**fields, "lieferzeit": "999"}, headers=HX
    )
    assert refused.status_code == 200 and "Lieferzeit" in refused.text
    again = _fields(refused.text)
    assert again["gesehen"] == fields["gesehen"] and again["lieferzeit"] == "999"

    # Another device changes the zone; the corrected form is not written over it.
    other = {**_edit(client, zone), "pauschale": "4"}
    assert (
        "Gespeichert"
        in client.post(f"{PAGE}/zone/{zone.id}", data=other, headers=HX).text
    )
    late = client.post(
        f"{PAGE}/zone/{zone.id}", data={**again, "lieferzeit": "50"}, headers=HX
    )
    assert late.status_code == 200 and "gerade geändert" in late.text
    fresh = _fields(late.text)
    # The fresh form: what holds now, with its token.
    assert fresh["pauschale"] == "4,00" and fresh["lieferzeit"] == "40"
    assert fresh["gesehen"] != fields["gesehen"]
    assert _fee(engine, "76530") == 400
    saved = client.post(
        f"{PAGE}/zone/{zone.id}", data={**fresh, "lieferzeit": "50"}, headers=HX
    )
    assert "Gespeichert" in saved.text


def test_a_code_of_another_active_zone_is_refused_with_its_zone(client, engine):
    _create(client)
    answer = client.post(
        NEW, data={**ZONE, "name": "Zone 2", "plz": "76534 76532"}, headers=HX
    )
    assert answer.status_code == 200 and 'role="alert"' in answer.text
    assert "76532" in answer.text and "Zone 1" in answer.text
    assert len(_zones(engine)) == 1


def test_the_list_again_is_what_abbrechen_fetches(client, engine):
    _create(client)
    part = client.get(f"{PAGE}/zonen", headers=HX)
    assert part.status_code == 200 and part.headers["Cache-Control"] == "no-store"
    assert "Zone 1" in part.text and "<html" not in part.text


# --- what is refused --------------------------------------------------------------


def test_no_zone_of_that_id(client, engine):
    assert client.get(f"{PAGE}/zone/{uuid.uuid4()}", headers=HX).status_code == 404
    gone = client.post(f"{PAGE}/zone/{uuid.uuid4()}", data=ZONE, headers=HX)
    assert gone.status_code == 404
    assert client.get(f"{PAGE}/zone/keine", headers=HX).status_code in (404, 422)
    assert _zones(engine) == []


def test_another_restaurants_zone_is_no_zone_here(client, engine):
    with Session(engine) as session:
        other = Tenant(name="Zweitbetrieb", timezone="Europe/Berlin")
        session.add(other)
        session.flush()
        theirs = DeliveryZone(
            tenant_id=other.id,
            name="Fremd",
            match_type="postal_code",
            postal_codes=["76530"],
            fee_cents=100,
            min_order_cents=0,
            eta_minutes=30,
        )
        session.add(theirs)
        session.commit()
        zone_id = theirs.id
    assert "Fremd" not in client.get(PAGE).text
    assert client.get(f"{PAGE}/zone/{zone_id}", headers=HX).status_code == 404
    answer = client.post(f"{PAGE}/zone/{zone_id}", data=ZONE, headers=HX)
    assert answer.status_code == 404
    with Session(engine) as session:
        assert session.get_one(DeliveryZone, zone_id).fee_cents == 100


@pytest.mark.parametrize(
    "headers",
    [{}, {"HX-Request": "true"}, {"HX-Request": "true", "X-CSRF-Token": "falsch"}],
)
def test_a_write_without_the_pages_token_is_refused(client, engine, headers):
    assert client.post(NEW, data=ZONE, headers=headers).status_code == 403
    assert _zones(engine) == []


# --- found by the review --------------------------------------------------------------


def test_a_name_with_signs_of_html_goes_through_the_form_unchanged(client, engine):
    name = 'Müller & Söhne "Nord" <1>'
    _create(client, name=name)
    (zone,) = _zones(engine)
    assert zone.name == name
    page = client.get(PAGE).text
    assert "<1>" not in page and "Müller &amp; Söhne" in page
    form = client.get(f"{PAGE}/zone/{zone.id}", headers=HX).text
    assert "<1>" not in form and '"Nord"' not in form
    fields = _edit(client, zone)
    assert fields["name"] == name
    answer = client.post(f"{PAGE}/zone/{zone.id}", data=fields, headers=HX)
    assert answer.status_code == 200 and "Keine Änderung" in answer.text
    # In a message built from the stored name, too.
    taken = client.post(NEW, data={**ZONE, "name": "Zone 2"}, headers=HX)
    assert 'role="alert"' in taken.text and "Müller &amp; Söhne" in taken.text
    assert "<1>" not in taken.text
    assert len(_zones(engine)) == 1


def test_a_long_name_of_the_import_survives_a_refused_form(client, engine):
    """The import knows no limit for a name. A refused form shows the field
    whole when it came back as it was shown, so the corrected save neither
    shortens the name nor is refused for it."""
    _create(client)
    (zone,) = _zones(engine)
    long_name = "Zone " + "x" * 70
    with Session(engine) as session:
        session.execute(
            update(DeliveryZone)
            .where(DeliveryZone.id == zone.id)
            .values(name=long_name)
        )
        session.commit()
    fields = _edit(client, zone)
    assert fields["name"] == long_name
    refused = client.post(
        f"{PAGE}/zone/{zone.id}", data={**fields, "pauschale": "2.50"}, headers=HX
    )
    assert refused.status_code == 200 and 'role="alert"' in refused.text
    again = _fields(refused.text)
    assert again["name"] == long_name and again["pauschale"] == "2.50"
    saved = client.post(
        f"{PAGE}/zone/{zone.id}", data={**again, "pauschale": "3"}, headers=HX
    )
    assert "Gespeichert" in saved.text and _fee(engine, "76530") == 300
    (zone,) = _zones(engine)
    assert zone.name == long_name
    # A name the owner types that long is still cut and refused.
    typed = client.post(
        f"{PAGE}/zone/{zone.id}",
        data={**_edit(client, zone), "name": "Neu " + "y" * 90},
        headers=HX,
    )
    assert "40 Zeichen" in typed.text and len(_fields(typed.text)["name"]) == 60


def test_the_postal_codes_keep_the_full_keyboard(client):
    """A number pad has no space to part the codes; the amounts take the
    decimal pad, whose separator is the device's (#829-6, below)."""
    form = client.get(NEW, headers=HX).text
    assert "inputmode" not in _tag(form, "plz")


# --- forms and accessibility (#821, #829) -----------------------------------------


def test_every_fault_of_a_zone_is_shown_by_its_field(client, engine):
    sent = {**ZONE, "plz": "abc", "pauschale": "2.5x", "lieferzeit": "500"}
    html = client.post(NEW, data=sent, headers=HX).text
    for name in ("plz", "pauschale", "lieferzeit"):
        assert 'aria-invalid="true"' in _tag(html, name), name
    for name in ("name", "mindestwert"):
        assert "aria-invalid" not in _tag(html, name), name
    assert "„abc“ ist keine Postleitzahl" in _described(html, "plz")
    assert "Lieferpauschale" in _described(html, "pauschale")
    assert "ganzen Minuten" in _described(html, "lieferzeit")
    summary = html.split("data-fokus", 1)[1].split("</div>", 1)[0]
    assert summary.count("<li>") == 3 and 'href="#f-plz"' in summary
    assert _zones(engine) == []


def test_the_helper_texts_belong_to_their_fields(client):
    """A screen reader reads a field with its rule (#829-7)."""
    html = client.get(NEW, headers=HX).text
    assert "Preise und Bedingungen" in _described(html, "name")
    assert "nur in einer aktiven Zone" in _described(html, "plz")
    assert "zum Beispiel 2,50" in _described(html, "pauschale")
    assert "zum Beispiel 2,50" in _described(html, "mindestwert")
    assert "Höchstens" in _described(html, "lieferzeit")


def test_amounts_open_the_number_pad_with_a_comma(client):
    """#829-6: the decimal pad has the comma of the device's language."""
    html = client.get(NEW, headers=HX).text
    assert 'inputmode="decimal"' in _tag(html, "pauschale")
    assert 'inputmode="decimal"' in _tag(html, "mindestwert")
    assert 'inputmode="numeric"' in _tag(html, "lieferzeit")
    assert 'hx-disabled-elt="find button"' in html


def test_the_zone_list_has_a_header_on_its_buttons_and_a_focused_result(client):
    answer = client.post(NEW, data=ZONE, headers=HX).text
    head = answer.split("</thead>", 1)[0]
    assert "<th></th>" not in head and ">Ändern</th>" in head
    assert re.search(r'<p\b[^>]*tabindex="-1"[^>]*data-fokus[^>]*>Gespeichert', answer)
