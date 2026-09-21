"""get_item_details: Allergen-Regel "unbekannt ist nicht keine", Optionen, Hülle (T-4.4, docs/04)."""

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import Session

from api.config import settings
from api.core.errors import NotFound
from api.db import get_db
from api.domain.menu.details import SAY_ALLERGENS_UNKNOWN, get_item_details
from api.domain.menu.importer import (
    ALIASES_FILE,
    ALLERGENS_FILE,
    MENU_FILE,
    OPTIONS_FILE,
    apply,
    parse,
)
from api.main import app
from api.models import MenuItem
from api.tests.conftest import p95_ms
from scripts.seed import seed

NOW = datetime(2026, 9, 18, 16, 0, tzinfo=UTC)
AUTH = {"Authorization": f"Bearer {settings.agent_api_token}"}

# Eigene Karte, weil es hier auf die Allergene ankommt: 23 ist gepflegt, 12 nicht.
KARTE = {
    MENU_FILE: (
        "number;name;category;price_eur;description;active\n"
        "23;Frühlingsrollen (4 Stück);Vorspeisen;6,90;mit Gemüsefüllung, dazu süßsaure Sauce;ja\n"
        "12;Wan-Tan-Suppe;Suppen;5,00;;ja\n"
        "50;Altes Gericht;Hauptgerichte;9,00;;nein\n"
    ),
    OPTIONS_FILE: (
        "number;group_name;option_name;price_delta_eur;is_default;required\n"
        "23;Sauce;süßsauer;0,00;ja;ja\n"
        "23;Sauce;Erdnuss;0,50;nein;ja\n"
    ),
    ALLERGENS_FILE: ("number;allergen_codes;confirmed_by\n23;F,A;Küche\n"),
    ALIASES_FILE: "number;alias\n23;Frühlingsrollen\n",
}


@pytest.fixture
def engine(migrated_db_url):
    engine = create_engine(migrated_db_url)
    yield engine
    engine.dispose()


@pytest.fixture
def session(engine):
    with Session(engine) as s:
        yield s


@pytest.fixture
def tenant_id(session) -> uuid.UUID:
    tid = uuid.UUID(
        seed(session, tenant_name="Testbetrieb", timezone="Europe/Berlin").tenant_id
    )
    plan = parse(KARTE)
    assert plan.ok, plan.errors
    apply(session, tid, plan, now=NOW)
    return tid


def gericht(session, tenant_id, nummer: str) -> MenuItem:
    return session.scalar(
        select(MenuItem).where(
            MenuItem.tenant_id == tenant_id, MenuItem.number == nummer
        )
    )


def details(session, tenant_id, nummer: str, **kw):
    return get_item_details(
        session, tenant_id, gericht(session, tenant_id, nummer).id, now=NOW, **kw
    )


# --- Fachlogik ---------------------------------------------------------------------


def test_gepflegte_allergene_in_der_reihenfolge_der_lmiv(session, tenant_id):
    result = details(session, tenant_id, "23")

    assert result.allergens.known is True
    # Die Datei nennt F vor A, vorgelesen wird trotzdem immer A vor F.
    assert result.allergens.codes == ["A", "F"]
    assert result.allergens.confirmed_at == NOW.date()
    assert result.say is None


def test_ohne_gepflegten_wert_gibt_es_keine_auskunft(session, tenant_id):
    result = details(session, tenant_id, "12")

    assert result.allergens.known is False
    assert result.allergens.codes == []
    assert result.allergens.confirmed_at is None
    assert result.say == SAY_ALLERGENS_UNKNOWN


def test_beschreibung_und_optionen_kommen_mit(session, tenant_id):
    result = details(session, tenant_id, "23")

    assert result.number == "23" and result.price_cents == 690
    assert result.description == "mit Gemüsefüllung, dazu süßsaure Sauce"
    gruppen = {g.group: g for g in result.option_groups}
    assert set(gruppen) == {"Sauce"}
    assert gruppen["Sauce"].required is True
    assert [
        (o.name, o.price_delta_cents, o.default) for o in gruppen["Sauce"].options
    ] == [
        ("süßsauer", 0, True),
        ("Erdnuss", 50, False),
    ]


def test_ausverkauft_steht_auch_in_den_details(session, tenant_id):
    item = gericht(session, tenant_id, "23")
    session.execute(
        update(MenuItem)
        .where(MenuItem.id == item.id)
        .values(sold_out_until=NOW + timedelta(hours=6))
    )
    session.commit()

    result = details(session, tenant_id, "23")

    assert result.sold_out is True
    # Gepflegte Allergene, also bleibt Platz für den Satz aus der Suche.
    assert result.say == "Frühlingsrollen (4 Stück) ist heute leider aus."


def test_fehlende_allergen_auskunft_wiegt_schwerer_als_ausverkauft(session, tenant_id):
    item = gericht(session, tenant_id, "12")
    session.execute(
        update(MenuItem)
        .where(MenuItem.id == item.id)
        .values(sold_out_until=NOW + timedelta(hours=6))
    )
    session.commit()

    result = details(session, tenant_id, "12")

    assert result.sold_out is True
    assert result.say == SAY_ALLERGENS_UNKNOWN


def test_unbekannte_id_ist_not_found(session, tenant_id):
    with pytest.raises(NotFound):
        get_item_details(session, tenant_id, uuid.uuid4(), now=NOW)


def test_inaktives_gericht_liefert_keine_details(session, tenant_id):
    with pytest.raises(NotFound):
        details(session, tenant_id, "50")


def test_fremder_mandant_bekommt_das_gericht_nicht(session, tenant_id):
    item = gericht(session, tenant_id, "23")
    with pytest.raises(NotFound):
        get_item_details(session, uuid.uuid4(), item.id, now=NOW)


# --- HTTP-Hülle --------------------------------------------------------------------


@pytest.fixture
def client(engine, tenant_id):
    def override_get_db():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def post(client, tenant_id, menu_item_id, **extra):
    body = {
        "call_id": str(uuid.uuid4()),
        "tenant_id": str(tenant_id),
        "menu_item_id": str(menu_item_id),
        **extra,
    }
    return client.post("/v1/tools/get_item_details", json=body, headers=AUTH)


def test_tool_folgt_der_huelle(client, session, tenant_id):
    item = gericht(session, tenant_id, "23")

    body = post(client, tenant_id, item.id).json()

    assert body["ok"] is True and body["say"] is None
    assert set(body["data"]) == {
        "menu_item_id",
        "number",
        "name",
        "price_cents",
        "sold_out",
        "description",
        "allergens",
        "option_groups",
    }
    assert body["data"]["allergens"] == {
        "known": True,
        "codes": ["A", "F"],
        "confirmed_at": str(date(2026, 9, 18)),
    }


def test_tool_sagt_den_rueckruf_zu(client, session, tenant_id):
    item = gericht(session, tenant_id, "12")

    body = post(client, tenant_id, item.id).json()

    assert body["data"]["allergens"]["known"] is False
    assert body["say"] == SAY_ALLERGENS_UNKNOWN


def test_tool_unbekannte_id_ist_not_found(client, tenant_id):
    body = post(client, tenant_id, uuid.uuid4()).json()

    assert body["ok"] is False
    assert body["error"]["code"] == "not_found"


def test_tool_ohne_token_kein_zugriff(client, session, tenant_id):
    item = gericht(session, tenant_id, "23")

    r = client.post(
        "/v1/tools/get_item_details",
        json={
            "call_id": str(uuid.uuid4()),
            "tenant_id": str(tenant_id),
            "menu_item_id": str(item.id),
        },
    )

    assert r.status_code == 401


def test_tool_ohne_menu_item_id_ist_invalid_input(client, tenant_id):
    r = client.post(
        "/v1/tools/get_item_details",
        json={"call_id": str(uuid.uuid4()), "tenant_id": str(tenant_id)},
        headers=AUTH,
    )

    assert r.json()["error"]["code"] == "invalid_input"


def test_latenz_p95_unter_300_ms(client, session, tenant_id):
    item = gericht(session, tenant_id, "23")

    p95 = p95_ms(lambda: post(client, tenant_id, item.id), n=20)

    print(f"\nget_item_details p95 = {p95:.1f} ms")
    assert p95 < 300, f"p95 {p95:.1f} ms über dem Budget aus docs/04 §1"
