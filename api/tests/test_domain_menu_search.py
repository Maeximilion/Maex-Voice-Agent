"""search_menu: Nummer, Alias, unscharf, Nachfrage, nie raten (T-4.3, docs/04 §search_menu)."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, update
from sqlalchemy.orm import Session

from api.config import settings
from api.core.errors import Ambiguous, NotFound
from api.db import get_db
from api.domain.menu import has_item_number_marker
from api.domain.menu.importer import (
    ALIASES_FILE,
    ALLERGENS_FILE,
    MENU_FILE,
    OPTIONS_FILE,
    apply,
    parse,
)
from api.domain.menu.normalize import normalize_query
from api.domain.menu.search import search_menu
from api.main import app
from api.models import MenuItem
from api.tests.conftest import p95_ms
from scripts.seed import seed

NOW = datetime(2026, 9, 18, 16, 0, tzinfo=UTC)
AUTH = {"Authorization": f"Bearer {settings.agent_api_token}"}

KARTE = {
    MENU_FILE: (
        "number;name;category;price_eur;description;active\n"
        "23;Frühlingsrollen (4 Stück);Vorspeisen;6,90;;ja\n"
        "24;Sommerrollen mit Garnelen;Vorspeisen;7,50;;ja\n"
        "12;Wan-Tan-Suppe;Suppen;5,00;;ja\n"
        "13;Pho Bo;Suppen;11,90;;ja\n"
        "47;Ente knusprig;Hauptgerichte;15,50;;ja\n"
        "48;Ente süß-sauer;Hauptgerichte;14,90;;ja\n"
        "50;Altes Gericht;Hauptgerichte;9,00;;nein\n"
    ),
    OPTIONS_FILE: (
        "number;group_name;option_name;price_delta_eur;is_default;required\n"
        "47;Fleisch;Ente;0,00;ja;ja\n"
        "47;Fleisch;Huhn;-1,00;nein;ja\n"
        "47;Sauce;Erdnuss;0,50;nein;nein\n"
    ),
    ALLERGENS_FILE: "number;allergen_codes;confirmed_by\n",
    ALIASES_FILE: (
        "number;alias\n"
        "23;Frühlingsrollen\n"
        "23;die knusprigen Rollen\n"
        "24;Sommerrollen\n"
        "12;Suppe\n"
        "13;Suppe\n"
        "13;Pho\n"
        "47;die knusprige Ente\n"
        "48;süßsaure Ente\n"
        "50;alt\n"
    ),
}


# --- ohne Datenbank ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("gesagt", "rest"),
    [
        ("Ich hätte gern zweimal die knusprige Ente, bitte", "knusprige ente"),
        ("zwei Frühlingsrollen", "frühlingsrollen"),
        ("einmal die Nummer dreiundzwanzig", ""),
        ("die 23", ""),
        ("2 x Pho Bo", "pho bo"),
        ("Wan-Tan-Suppe", "wan-tan-suppe"),
        # Kompakte Mengen wie in numberwords (Codex PR #117, P2)
        ("2x Pho", "pho"),
        ("2 Stück Pho", "pho"),
        ("2 stk Pho", "pho"),
        ("2 st Pho", "pho"),
        ("Frühlingsrollen (4 Stück)", "frühlingsrollen"),
        # Erkennung ohne Umlaut: "haette" darf nicht als Name stehen bleiben,
        # sonst trifft der exakte Alias "pho" nicht mehr (Codex PR #117, P2).
        ("ich haette gern Pho", "pho"),
        ("ich moechte Pho", "pho"),
        ("aeh Pho", "pho"),
        # Der Gerichtname selbst wird nie umgeschrieben.
        ("Frühlingsrollen", "frühlingsrollen"),
    ],
)
def test_normalize_query(gesagt, rest):
    assert normalize_query(gesagt) == rest


@pytest.mark.parametrize(
    ("gesagt", "marker"),
    [
        ("Nummer 23", True),
        ("die Nr. 23", True),
        ("die 23", False),
        ("zwei Rollen", False),
    ],
)
def test_nummer_marker(gesagt, marker):
    assert has_item_number_marker(gesagt) is marker


# --- mit Datenbank -----------------------------------------------------------------


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


def suche(session, tenant_id, text, **kw):
    return search_menu(session, tenant_id, text, now=NOW, **kw)


def nummern(result) -> list[str]:
    return [hit.number for hit in result.results]


@pytest.mark.parametrize(
    "gesagt",
    ["einmal die Nummer dreiundzwanzig", "die 23", "Nummer 23", "dreiundzwanzig"],
)
def test_nummer_trifft_exakt(session, tenant_id, gesagt):
    result = suche(session, tenant_id, gesagt)

    assert result.match_type == "exact_number" and nummern(result) == ["23"]
    hit = result.results[0]
    assert hit.price_cents == 690 and hit.sold_out is False and result.say is None


def test_unbekannte_nummer_raet_nicht(session, tenant_id):
    """Nummer 99 gibt es nicht: kein Ausweichen auf einen aehnlichen Namen."""
    with pytest.raises(NotFound) as err:
        suche(session, tenant_id, "Nummer 99")
    assert "Nummer 99" in err.value.say


def test_menge_ist_keine_nummer(session, tenant_id):
    """ "zwei Frühlingsrollen" ist nicht Gericht 2, sondern zweimal die 23."""
    result = suche(session, tenant_id, "zwei Frühlingsrollen")

    assert result.match_type == "alias" and nummern(result) == ["23"]


@pytest.mark.parametrize(
    ("gesagt", "nummer"),
    [
        ("die knusprigen Rollen", "23"),
        ("Die knusprigen Rollen, bitte", "23"),
        ("Sommerrollen", "24"),
        ("Pho", "13"),
        # Fuellwoerter auf beiden Seiten egal: Alias "die knusprige ente".
        ("knusprige Ente bitte", "47"),
        ("2x Pho", "13"),
        ("zwei Stück Sommerrollen", "24"),
    ],
)
def test_alias_exakt(session, tenant_id, gesagt, nummer):
    result = suche(session, tenant_id, gesagt)

    assert result.match_type == "alias" and nummern(result) == [nummer]


def test_alias_an_zwei_gerichten_fragt_nach(session, tenant_id):
    result = suche(session, tenant_id, "eine Suppe bitte")

    assert result.match_type == "ambiguous"
    assert nummern(result) == ["12", "13"]
    assert result.say.startswith("Meinen Sie Nummer 12")


def test_zwei_starke_treffer_fragen_nach(session, tenant_id):
    """ "Ente" passt auf zwei Gerichte gleich gut: nachfragen, nicht waehlen."""
    result = suche(session, tenant_id, "Ente")

    assert result.match_type == "ambiguous"
    assert set(nummern(result)) == {"47", "48"}
    assert " oder " in result.say


def test_eindeutig_unscharf(session, tenant_id):
    """Kein Alias passt ("Ente knusprig" steht so nur als Name da): Trigram entscheidet."""
    result = suche(session, tenant_id, "Ente knusprig, bitte")

    assert result.match_type == "fuzzy_single" and nummern(result) == ["47"]


def test_tippfehler_der_erkennung_findet_das_gericht(session, tenant_id):
    result = suche(session, tenant_id, "Frülingsrolle")

    assert result.match_type in {"fuzzy_single", "ambiguous"}
    assert nummern(result)[0] == "23"


@pytest.mark.parametrize("gesagt", ["Pizza Hawaii", "bitte", "ähm"])
def test_nichts_gefunden(session, tenant_id, gesagt):
    with pytest.raises(NotFound) as err:
        suche(session, tenant_id, gesagt)
    assert "Nummer sagen" in err.value.say


@pytest.mark.parametrize("gesagt", ["Altes Gericht", "Nummer 50", "alt"])
def test_inaktives_gericht_wird_nie_gefunden(session, tenant_id, gesagt):
    with pytest.raises(NotFound):
        suche(session, tenant_id, gesagt)


def test_ausverkauft_mit_satz(session, tenant_id):
    session.execute(
        update(MenuItem)
        .where(MenuItem.tenant_id == tenant_id, MenuItem.number == "47")
        .values(sold_out_until=NOW + timedelta(hours=6))
    )
    session.commit()

    result = suche(session, tenant_id, "Nummer 47")

    assert result.results[0].sold_out is True
    assert result.say == "Ente knusprig ist heute leider aus."


def test_ausverkauft_abgelaufen_ist_wieder_da(session, tenant_id):
    session.execute(
        update(MenuItem)
        .where(MenuItem.tenant_id == tenant_id, MenuItem.number == "47")
        .values(sold_out_until=NOW - timedelta(minutes=1))
    )
    session.commit()

    assert suche(session, tenant_id, "Nummer 47").results[0].sold_out is False


def test_optionen_nach_gruppe_default_zuerst(session, tenant_id):
    hit = suche(session, tenant_id, "Nummer 47").results[0]

    gruppen = {g.group: g for g in hit.option_groups}
    assert set(gruppen) == {"Fleisch", "Sauce"}
    fleisch = gruppen["Fleisch"]
    assert fleisch.required is True
    assert [(o.name, o.default) for o in fleisch.options] == [
        ("Ente", True),
        ("Huhn", False),
    ]
    assert fleisch.options[1].price_delta_cents == -100


def test_max_results_begrenzt_vorschlaege(session, tenant_id):
    result = suche(session, tenant_id, "eine Suppe bitte", max_results=1)

    assert result.match_type == "ambiguous" and nummern(result) == ["12"]


def test_fremder_mandant_sieht_die_karte_nicht(session, tenant_id):
    anderer = uuid.UUID(
        seed(session, tenant_name="Zweitbetrieb", timezone="Europe/Berlin").tenant_id
    )
    with pytest.raises(NotFound):
        suche(session, anderer, "Nummer 23")


# --- HTTP-Huelle ---------------------------------------------------------------


@pytest.fixture
def client(engine, tenant_id):
    def override_get_db():
        with Session(engine) as s:
            yield s

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def post(client, tenant_id, query, **extra):
    body = {
        "call_id": str(uuid.uuid4()),
        "tenant_id": str(tenant_id),
        "query": query,
        **extra,
    }
    return client.post("/v1/tools/search_menu", json=body, headers=AUTH)


def test_tool_folgt_der_huelle(client, tenant_id):
    response = post(client, tenant_id, "einmal die Nummer dreiundzwanzig")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True and body["say"] is None
    assert body["data"]["match_type"] == "exact_number"
    hit = body["data"]["results"][0]
    assert set(hit) == {
        "menu_item_id",
        "number",
        "name",
        "price_cents",
        "sold_out",
        "option_groups",
    }
    assert hit["number"] == "23" and hit["price_cents"] == 690


def test_tool_nicht_gefunden_als_fehler_mit_satz(client, tenant_id):
    body = post(client, tenant_id, "Pizza Hawaii").json()

    assert body["ok"] is False and body["error"]["code"] == "not_found"
    assert "Nummer sagen" in body["say"]


def test_tool_braucht_token(client, tenant_id):
    body = {"call_id": str(uuid.uuid4()), "tenant_id": str(tenant_id), "query": "23"}
    assert client.post("/v1/tools/search_menu", json=body).status_code == 401


@pytest.mark.parametrize(
    "extra", [{"query": ""}, {"max_results": 0}, {"max_results": 9}]
)
def test_tool_prueft_eingaben(client, tenant_id, extra):
    body = {"call_id": str(uuid.uuid4()), "tenant_id": str(tenant_id), "query": "Ente"}
    body.update(extra)
    response = client.post("/v1/tools/search_menu", json=body, headers=AUTH)

    assert response.json()["error"]["code"] == "invalid_input"


def test_tool_bleibt_schnell_mit_grosser_karte(client, session, tenant_id):
    """Latenzbudget 300 ms p95 (docs/04), auch mit 200 Gerichten und unscharfer Suche."""
    zeilen = "".join(
        f"{100 + i};Testgericht Nummer {i} mit Reis;Test;9,90;;ja\n" for i in range(200)
    )
    plan = parse(
        {MENU_FILE: KARTE[MENU_FILE] + zeilen, ALIASES_FILE: KARTE[ALIASES_FILE]}
    )
    assert plan.ok, plan.errors
    apply(session, tenant_id, plan, now=NOW)

    assert p95_ms(lambda: post(client, tenant_id, "knusprige Ente bitte")) < 300
    assert p95_ms(lambda: post(client, tenant_id, "Frülingsrolle")) < 300


# --- Kartennummern als Text (Befund Codex PR #116, P1) ---------------------------


@pytest.fixture
def buchstaben(session, tenant_id):
    """Karte mit 23 und 23a, 31a ohne 31, 7 und 08 (fuehrende Null)."""
    zeilen = (
        "23a;Frühlingsrollen vegan;Vorspeisen;6,90;;ja\n"
        "31a;Glasnudelsalat;Vorspeisen;7,90;;ja\n"
        "7;Misosuppe;Suppen;4,50;;ja\n"
        "08;Edamame;Vorspeisen;4,90;;ja\n"
    )
    plan = parse(
        {MENU_FILE: KARTE[MENU_FILE] + zeilen, ALIASES_FILE: KARTE[ALIASES_FILE]}
    )
    assert plan.ok, plan.errors
    apply(session, tenant_id, plan, now=NOW)
    return tenant_id


@pytest.mark.parametrize(
    ("gesagt", "nummer"),
    [
        ("Nummer 23a", "23a"),
        ("Nummer 23 a", "23a"),
        ("die 23A bitte", "23a"),
        ("Nummer 23", "23"),
        ("Nummer 31a", "31a"),
        ("Nummer 07", "7"),
        ("Nummer 7", "7"),
        ("Nummer acht", "08"),
        ("Nummer 08", "08"),
    ],
)
def test_nummer_als_text(session, buchstaben, gesagt, nummer):
    result = suche(session, buchstaben, gesagt)

    assert result.match_type == "exact_number" and nummern(result) == [nummer]


def test_zahl_ohne_buchstabe_trifft_nicht_die_variante(session, buchstaben):
    """Es gibt 31a, aber keine 31: "Nummer 31" ist nicht gefunden, nicht 31a."""
    with pytest.raises(NotFound) as err:
        suche(session, buchstaben, "Nummer 31")
    assert "Nummer 31 " in err.value.say


def test_unbekannte_buchstabennummer(session, buchstaben):
    with pytest.raises(NotFound) as err:
        suche(session, buchstaben, "Nummer 23c")
    assert "Nummer 23c" in err.value.say


# --- Eine Fundstelle fuer Nummer, Buchstabe und Marker (Codex PR #117, P1) --------


def test_marker_ohne_zahl_dahinter_macht_menge_nicht_zur_nummer(session, tenant_id):
    """ "Nummer weiß ich nicht": das Wort Nummer gehoert nicht zur zwei."""
    result = suche(session, tenant_id, "zwei Frühlingsrollen, Nummer weiß ich nicht")

    assert result.match_type != "exact_number"
    assert nummern(result)[0] == "23"


@pytest.mark.parametrize("gesagt", ["Nummer 23g", "Nummer 23ab", "Nummer 23 g"])
def test_ungueltiger_buchstabe_wird_nicht_abgeschnitten(session, tenant_id, gesagt):
    """Befund Codex PR #117: "23g" ist nicht die 23 - nicht gefunden statt still 23."""
    with pytest.raises(NotFound) as err:
        suche(session, tenant_id, gesagt)
    assert "Nummer 23" in err.value.say and err.value.say != ""
    assert "23 habe" not in err.value.say


def test_zwei_x_bleibt_eine_menge(session, tenant_id):
    assert nummern(suche(session, tenant_id, "2x Pho")) == ["13"]


@pytest.mark.parametrize(
    ("gesagt", "nummer"),
    [("drei Portionen von der 23", "23"), ("die 23 a", "23a")],
)
def test_verbindungswoerter_und_abgesetzter_buchstabe(
    session, buchstaben, gesagt, nummer
):
    """Codex PR #117: "von" und ein abgesetztes "a" sind kein Gerichtname."""
    result = suche(session, buchstaben, gesagt)

    assert result.match_type == "exact_number" and nummern(result) == [nummer]


def test_nummer_ist_23(session, tenant_id):
    result = suche(session, tenant_id, "die Nummer ist 23")

    assert result.match_type == "exact_number" and nummern(result) == ["23"]


def test_unmarkierte_ungueltige_nummer_liefert_nie_die_23(session, tenant_id):
    """ "die 23g" ohne Marker: nicht gefunden, aber nie still Gericht 23."""
    with pytest.raises(NotFound):
        suche(session, tenant_id, "die 23g")


def test_alias_mit_ziffer_vorn(session, tenant_id):
    """Codex PR #117: ein Alias wie "7up" darf nicht als Kartennummer verschwinden."""
    getraenk = "90;Seven Up;Getränke;2,50;;ja"
    plan = parse(
        {
            MENU_FILE: KARTE[MENU_FILE] + getraenk + "\n",
            ALIASES_FILE: KARTE[ALIASES_FILE] + "90;7up" + "\n",
        }
    )
    assert plan.ok, plan.errors
    apply(session, tenant_id, plan, now=NOW)

    for gesagt in ("7up", "ein 7up bitte"):
        assert nummern(suche(session, tenant_id, gesagt)) == ["90"], gesagt


# --- Regel A: Nummer nur direkt, wenn der Satz eindeutig ist ----------------------


@pytest.mark.parametrize(
    "gesagt",
    [
        "Nummer 23",
        "die 23",
        "dreiundzwanzig",
        "einmal die Nummer dreiundzwanzig",
        "drei Portionen von der 23",
        "die Nummer ist 23",
        "Nummer 23 zweimal",
        "zwei Nummer 23",
        "2x die 23",
        "Nummer 23 bitte",
        "Nummer 23, äh, bitte",
    ],
)
def test_eindeutiger_nummernsatz_trifft(session, tenant_id, gesagt):
    result = suche(session, tenant_id, gesagt)

    assert result.match_type == "exact_number" and nummern(result) == ["23"]


@pytest.mark.parametrize(
    "gesagt",
    [
        "Nummer 23 oder 24",
        "Nummer 23, nein 24",
        "Nummer 23 oder Nummer 24",
        "Nummer 23, äh, 24",
        "Nummer 23 und 24",
        "Nummer 47, die Ente",
        "Nummer 23 mit 2 Soßen",
        "Nummer 1000 und 23",
        "23a, nein, Nummer 23",
        "23 und 24",
        "Nummer 07 oder Nummer 7",
        # Ohne Marker: sonst suchte die Namenssuche nach "oder" (Codex PR #117).
        "23 oder 24",
        "Nummer A12 oder 24",
        # Der Marker ueberlebt Fuellwort und Zoegerlaut (Codex PR #117, P1).
        "Nummer bitte 23, Pho",
    ],
)
def test_nummer_mit_mehr_im_satz_fragt_nach(session, tenant_id, gesagt):
    """Regel A: eine zweite Zahl oder Text neben der Nummer - nie selbst waehlen."""
    with pytest.raises(Ambiguous) as err:
        suche(session, tenant_id, gesagt)
    assert "Welche Nummer" in err.value.say


def test_ohne_nummer_im_satz_bleibt_die_namenssuche(session, tenant_id):
    """ "Nummer weiß ich nicht" ist kein Nummernsatz: die Namenssuche entscheidet."""
    result = suche(session, tenant_id, "zwei Frühlingsrollen, Nummer weiß ich nicht")

    assert nummern(result)[0] == "23" and result.match_type != "exact_number"
