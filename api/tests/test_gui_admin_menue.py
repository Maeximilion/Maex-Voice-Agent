"""The tab "Menü" of the admin area (#571, plan number 9): the menu as the
agent knows it, with a search and the notes "Allergene fehlen" and "Kasse".
The page only reads. These tests act as a signed-in device (conftest); the
refusal without a session is in test_gui_login.py."""

import re
import uuid
from html import unescape

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from api.db import get_db
from api.domain.menu import listing, sold_out
from api.main import app
from api.models import AuditLog, MenuItem
from api.tests.test_domain_menu_listing import dish
from scripts.seed import seed

PAGE = "/gui/menue"


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
def tenant_id(db) -> uuid.UUID:
    return uuid.UUID(
        seed(db, tenant_name="Testbetrieb", timezone="Europe/Berlin").tenant_id
    )


@pytest.fixture
def client(engine):
    def override_get_db():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app)
    client.cookies.set("csrf_token", "testtoken")
    try:
        yield client
    finally:
        app.dependency_overrides.pop(get_db, None)


def _rows(html: str) -> list[str]:
    """The text of each dish row, tags dropped, as a reader sees it."""
    body = html.split('<ul class="menu-list"', 1)
    if len(body) == 1:
        return []
    items = re.findall(r"<li\b[^>]*>(.*?)</li>", body[1].split("</ul>", 1)[0], re.S)
    return [" ".join(unescape(re.sub(r"<[^>]+>", " ", item)).split()) for item in items]


# --- the page ---------------------------------------------------------------------


def test_the_page_shows_the_frame_and_the_dishes_from_the_database(
    client, db, tenant_id
):
    dish(db, tenant_id, "12", "Knusprige Ente", 1380, allergens=("A", "F"))
    dish(db, tenant_id, "2", "Wan-Tan-Suppe", 450)
    dish(db, tenant_id, "23", "Frühlingsrollen", 690, allergens=("A",), register=720)
    dish(db, tenant_id, "99", "Nicht mehr da", active=False)
    page = client.get(PAGE)
    assert page.status_code == 200
    assert page.headers["Cache-Control"] == "no-store"
    assert '<a href="/gui/menue" aria-current="page">Menü</a>' in page.text
    for tab in ("/gui/zeiten", "/gui/lieferung", "/gui/anrufe", "/gui/geraete"):
        assert f'href="{tab}"' in page.text
    assert _rows(page.text) == [
        "2 Wan-Tan-Suppe 4,50 € Allergene fehlen",
        "12 Knusprige Ente 13,80 €",
        "23 Frühlingsrollen 6,90 € Kasse: 7,20 €",
    ]
    assert "Nicht mehr da" not in page.text
    assert "3 Gerichte" in page.text
    assert (
        "1 ohne Allergen-Angabe" in page.text
        and "1 mit offener Preisabweichung" in page.text
    )


def test_every_amount_is_the_cents_of_the_database(client, db, tenant_id):
    """No float on the way: 0, one cent, and an amount a float would round."""
    dish(db, tenant_id, "1", "Frei", 0, register=1)
    dish(db, tenant_id, "2", "Teuer", 123456789, register=0)
    dish(db, tenant_id, "3", "Krumm", 1005)
    assert _rows(client.get(PAGE).text) == [
        "1 Frei 0,00 € Allergene fehlen Kasse: 0,01 €",
        "2 Teuer 1234567,89 € Allergene fehlen Kasse: 0,00 €",
        "3 Krumm 10,05 € Allergene fehlen",
    ]


def test_a_rejected_register_price_is_no_note(client, db, tenant_id):
    dish(
        db, tenant_id, "1", "Suppe", 450, allergens=("A",), register=500, rejected=True
    )
    page = client.get(PAGE).text
    assert _rows(page) == ["1 Suppe 4,50 €"]
    # The words say "open": a rejected price that still differs is no
    # difference the page counts, and the legend says where it went.
    assert "5,00" not in page and "0 mit offener Preisabweichung" in page
    assert "Ein verworfener Kassenpreis steht hier nicht mehr." in page
    assert '<option value="preis">Preisabweichung offen</option>' in page


def test_the_page_names_no_allergen(client, db, tenant_id):
    """The list says only whether something is kept, never what a dish
    contains or that it contains nothing."""
    dish(db, tenant_id, "1", "Ente", allergens=("A", "G"))
    dish(db, tenant_id, "2", "Reis")
    page = client.get(PAGE).text
    for word in ("Gluten", "Milch", "glutenhaltig", "frei von", "keine Allergene"):
        assert word not in page
    assert _rows(page) == ["1 Ente 6,90 €", "2 Reis 6,90 € Allergene fehlen"]


def test_a_name_with_markup_is_shown_as_text(client, db, tenant_id):
    dish(db, tenant_id, "1", '<script>alert(1)</script>"><b>Ente')
    page = client.get(PAGE).text
    assert "<script>alert" not in page and '"><b>' not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    # What was typed comes back in the field, as text too.
    typed = client.get(f"{PAGE}?q=%22%3E%3Cscript%3Ex").text
    assert "<script>x" not in typed
    assert 'name="q" value="&#34;&gt;&lt;script&gt;x"' in typed


def test_a_name_that_turns_the_reading_direction_stays_in_its_own_box(
    client, db, tenant_id
):
    """A direction override in a name would show the price beside it
    backwards ("08,31" for 13,80): number and name are isolated."""
    turned = "Ente" + chr(0x202E)
    dish(db, tenant_id, "1", turned, 1380)
    page = client.get(PAGE).text
    assert f'<bdi class="menu-name">{turned}</bdi>' in page
    assert '<bdi class="nr">1</bdi>' in page
    assert '<span class="menu-price">13,80 €</span>' in page


def test_without_a_dish_the_page_says_so(client, tenant_id):
    page = client.get(PAGE)
    assert page.status_code == 200
    assert _rows(page.text) == []
    assert "Noch kein Gericht in der Karte." in page.text


def test_without_a_restaurant_the_page_is_a_503(client):
    assert client.get(PAGE).status_code == 503


# --- the search and the filter ----------------------------------------------------


def test_the_search_filters_and_keeps_what_was_typed(client, db, tenant_id):
    dish(db, tenant_id, "07", "Wan-Tan-Suppe", allergens=("A",))
    dish(db, tenant_id, "70", "Knusprige Ente", allergens=("A",))
    by_name = client.get(f"{PAGE}?q=ente").text
    assert _rows(by_name) == ["70 Knusprige Ente 6,90 €"]
    assert 'name="q" value="ente"' in by_name
    assert "1 von 2 Gerichten" in by_name
    assert _rows(client.get(f"{PAGE}?q=7").text) == [
        "07 Wan-Tan-Suppe 6,90 €",
        "70 Knusprige Ente 6,90 €",
    ]
    # A no-break space from a pasted name is a space, a character nobody
    # sees is dropped.
    pasted = client.get(f"{PAGE}?q=knusprige%C2%A0en%E2%80%8Bte").text
    assert _rows(pasted) == ["70 Knusprige Ente 6,90 €"]
    assert 'name="q" value="knusprige ente"' in pasted
    nothing = client.get(f"{PAGE}?q=pizza").text
    assert _rows(nothing) == [] and "Kein Gericht gefunden." in nothing


def test_the_filter_shows_the_dishes_with_that_note(client, db, tenant_id):
    dish(db, tenant_id, "1", "Ente", register=700)
    dish(db, tenant_id, "2", "Suppe", allergens=("A",), register=500)
    dish(db, tenant_id, "3", "Reis", allergens=("A",))
    missing = client.get(f"{PAGE}?hinweis=allergene").text
    assert _rows(missing) == ["1 Ente 6,90 € Allergene fehlen Kasse: 7,00 €"]
    assert '<option value="allergene" selected>' in missing
    price = client.get(f"{PAGE}?hinweis=preis&q=suppe").text
    assert _rows(price) == ["2 Suppe 6,90 € Kasse: 5,00 €"]


@pytest.mark.parametrize(
    "address",
    [
        "?hinweis=alles&seite=x&q=",
        "?seite=-1",
        "?seite=99999999999999999999999999",
        "?seite=" + "9" * 5000,
        "?seite=1e3",
        "?seite=%C2%B2",
        "?q=" + "a" * 5000,
        "?q=%00%E2%80%AE&hinweis=%00",
        "?q=a&q=b&seite=1&seite=2",
    ],
)
def test_an_address_nobody_offers_still_shows_a_page(client, db, tenant_id, address):
    dish(db, tenant_id, "1", "Ente")
    page = client.get(PAGE + address)
    assert page.status_code == 200
    assert "a" * (sold_out.QUERY_MAX + 1) not in page.text


def test_a_filter_that_finds_nothing_says_so(client, db, tenant_id):
    dish(db, tenant_id, "1", "Ente", allergens=("A",))
    page = client.get(f"{PAGE}?hinweis=preis").text
    assert _rows(page) == [] and "Kein Gericht gefunden." in page
    assert "Noch kein Gericht" not in page
    assert "0 von 1 Gericht" in page
    assert 'href="/gui/menue">Alle zeigen</a>' in page


# --- pages ------------------------------------------------------------------------


def test_a_large_menu_comes_in_pages_that_keep_the_search(
    client, db, tenant_id, monkeypatch
):
    monkeypatch.setattr(listing, "PAGE_SIZE", 2)
    for number in range(1, 6):
        dish(db, tenant_id, str(number), f"Ente & Reis {number}")
    first = client.get(f"{PAGE}?q=ente+%26").text
    assert len(_rows(first)) == 2 and "Seite 1 von 3" in first
    assert 'href="/gui/menue?q=ente+%26&amp;seite=2"' in first
    assert "Vorherige Seite" not in first and "Nächste Seite</a>" in first
    last = client.get(f"{PAGE}?q=ente+%26&seite=3").text
    assert _rows(last) == ["5 Ente & Reis 5 6,90 € Allergene fehlen"]
    assert 'href="/gui/menue?q=ente+%26&amp;seite=2"' in last
    assert "Nächste Seite" not in last and "Vorherige Seite</a>" in last
    # Past the end is the last page, with the filter kept in its links.
    far = client.get(f"{PAGE}?hinweis=allergene&seite=40").text
    assert "Seite 3 von 3" in far
    assert 'href="/gui/menue?hinweis=allergene&amp;seite=2"' in far
    # So is a number longer than any page count.
    assert "Seite 3 von 3" in client.get(f"{PAGE}?seite=" + "9" * 30).text


def test_one_page_has_no_pager(client, db, tenant_id):
    dish(db, tenant_id, "1", "Ente")
    assert '<nav class="pager"' not in client.get(PAGE).text


# --- it only reads ----------------------------------------------------------------


def test_the_page_changes_nothing_and_takes_no_write(client, db, tenant_id):
    item_id = dish(db, tenant_id, "1", "Ente", 690, register=720)
    assert client.get(f"{PAGE}?q=ente&hinweis=preis").status_code == 200
    hx = {"HX-Request": "true", "X-CSRF-Token": "testtoken"}
    for method in ("post", "put", "patch", "delete"):
        assert getattr(client, method)(PAGE, headers=hx).status_code == 405
    db.expire_all()
    item = db.get(MenuItem, item_id)
    assert (item.price_cents, item.register_price_cents) == (690, 720)
    assert not item.register_price_rejected
    assert db.scalar(select(func.count()).select_from(AuditLog)) == 0
    # The price is answered on the board, as before; the page says where.
    assert "Übersicht" in client.get(PAGE).text
