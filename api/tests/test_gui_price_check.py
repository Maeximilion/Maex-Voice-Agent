"""gui: the price difference of the register on the tablet (T-4.9, docs/06 §3)."""

import uuid
from datetime import UTC
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from api.db import get_db
from api.domain.menu import price_check
from api.domain.menu.importer import MENU_FILE, apply, parse
from api.main import app
from api.models import AuditLog, MenuItem
from api.tests.test_domain_draft_order import KARTE, NOW, _tenant, item

HX = {"HX-Request": "true", "X-CSRF-Token": "testtoken"}


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
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


def difference(db, tenant_id, register="7,20") -> uuid.UUID:
    """The register copy says another price for 23 Frühlingsrollen."""
    plan = parse({**KARTE, MENU_FILE: KARTE[MENU_FILE].replace("6,90", register)})
    assert plan.ok, plan.errors
    report = apply(db, tenant_id, plan, dry_run=True, now=NOW)
    price_check.record(db, tenant_id, report.price_changes)
    return item(db, tenant_id, "23")


def price(db, item_id) -> int:
    db.expire_all()
    return db.scalar(select(MenuItem.price_cents).where(MenuItem.id == item_id))


def test_no_difference_shows_no_card(client, tenant_id):
    html = client.get("/gui/").text

    assert 'id="preise-liste"' in html
    assert "Kasse:" not in html


def test_a_difference_shows_as_a_red_badge_with_both_answers(client, db, tenant_id):
    dish = difference(db, tenant_id)

    html = client.get("/gui/").text

    assert '<span class="badge danger">Kasse: 7,20 €</span>' in html
    assert "6,90 €" in html and "Frühlingsrollen" in html
    assert f'hx-post="/gui/preise/{dish}/720/uebernehmen"' in html
    assert f'hx-post="/gui/preise/{dish}/720/verwerfen"' in html


def test_the_fragment_serves_the_same_list(client, db, tenant_id):
    difference(db, tenant_id)

    html = client.get("/gui/fragments/preise").text

    assert "Kasse: 7,20 €" in html


def test_take_changes_the_price_and_the_card_goes(client, db, tenant_id):
    dish = difference(db, tenant_id)

    answer = client.post(f"/gui/preise/{dish}/720/uebernehmen", headers=HX)

    assert answer.status_code == 200
    assert "Kasse:" not in answer.text
    assert price(db, dish) == 720
    [entry] = db.scalars(
        select(AuditLog).where(AuditLog.action == price_check.ACTION_TAKEN)
    ).all()
    assert entry.actor == "gui:tablet"


def test_reject_keeps_the_price_and_the_card_goes(client, db, tenant_id):
    dish = difference(db, tenant_id)

    answer = client.post(f"/gui/preise/{dish}/720/verwerfen", headers=HX)

    assert answer.status_code == 200
    assert "Kasse:" not in answer.text
    assert price(db, dish) == 690


def test_a_tap_on_a_price_that_moved_changes_nothing(client, db, tenant_id):
    """Another copy came in while the card was open: the list comes back fresh."""
    dish = difference(db, tenant_id)
    difference(db, tenant_id, "7,50")

    answer = client.post(f"/gui/preise/{dish}/720/uebernehmen", headers=HX)

    assert answer.status_code == 200
    assert "Kasse: 7,50 €" in answer.text
    assert price(db, dish) == 690


def test_a_second_tablet_gets_the_fresh_list(client, db, tenant_id):
    dish = difference(db, tenant_id)
    client.post(f"/gui/preise/{dish}/720/uebernehmen", headers=HX)

    answer = client.post(f"/gui/preise/{dish}/720/uebernehmen", headers=HX)

    assert answer.status_code == 200
    assert price(db, dish) == 720


def test_an_unknown_choice_is_not_a_page(client, db, tenant_id):
    dish = difference(db, tenant_id)

    answer = client.post(f"/gui/preise/{dish}/720/loeschen", headers=HX)

    assert answer.status_code == 404
    assert price(db, dish) == 690


@pytest.mark.parametrize(
    "headers",
    [{}, {"HX-Request": "true"}, {"HX-Request": "true", "X-CSRF-Token": "falsch"}],
)
def test_a_price_changes_only_with_the_page_token(client, db, tenant_id, headers):
    dish = difference(db, tenant_id)

    answer = client.post(f"/gui/preise/{dish}/720/uebernehmen", headers=headers)

    assert answer.status_code == 403
    assert price(db, dish) == 690


@pytest.mark.parametrize("cents", ["0", "-720", "99999999999", str(10**30)])
def test_a_price_in_the_address_is_never_taken(client, db, tenant_id, cents):
    """The price comes from the database only: the address can name the one on
    the card and nothing else, and a wild number is no server error."""
    dish = difference(db, tenant_id)

    answer = client.post(f"/gui/preise/{dish}/{cents}/uebernehmen", headers=HX)

    assert answer.status_code == 200
    assert "Kasse: 7,20 €" in answer.text
    assert price(db, dish) == 690


def test_a_get_never_changes_a_price(client, db, tenant_id):
    dish = difference(db, tenant_id)

    assert client.get(f"/gui/preise/{dish}/720/uebernehmen").status_code == 405
    assert price(db, dish) == 690


def test_the_page_reloads_the_list_on_the_event():
    script = (Path(__file__).parents[1] / "gui/static/app.js").read_text(
        encoding="utf-8"
    )

    assert 'strom.addEventListener("prices"' in script
    assert '"/gui/fragments/preise"' in script
