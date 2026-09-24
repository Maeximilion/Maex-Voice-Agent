"""Wuensche zu einer Position (T-4.10, docs/04 §search_menu, Entscheidung D8).

"die 23 ohne Karotten" fand vorher nichts: der Wunsch stand im Satz und die
Suche bekam ihn mit. Jetzt trennt `domain/menu/wishes.py` Gericht und Wunsch,
und der Wunsch wird eingeordnet:

- Weglassen ("ohne", "kein") ist ein Hinweis ohne Preis.
- Was als Option auf der Karte steht ("mit Nudeln statt Reis"), ist diese Option
  mit ihrem Aufpreis aus `item_options` - nie ein Preis vom Modell.
- Eine Allergie ist kein Wunsch, sondern ein Hinweis an die Kueche ohne Zusage.
- Alles andere bietet der Agent nicht an (D8).
"""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from api.domain.menu.details import get_item_details
from api.domain.menu.importer import (
    ALIASES_FILE,
    ALLERGENS_FILE,
    MENU_FILE,
    OPTIONS_FILE,
    apply,
    parse,
)
from api.domain.menu.search import search_menu
from api.domain.menu.wishes import classify_wish, split_wish
from api.models import ItemOption, MenuItem
from api.schemas.menu import OptionGroup, OptionOut
from api.tests.conftest import p95_ms
from scripts.seed import seed

NOW = datetime(2026, 9, 18, 16, 0, tzinfo=UTC)
GRUND = "Nudeln brauchen eine zweite Station in der Küche"

KARTE_WUENSCHE = {
    MENU_FILE: (
        "number;name;category;price_eur;description;active\n"
        "23;Frühlingsrollen (4 Stück);Vorspeisen;6,90;;ja\n"
        "24;Sommerrollen mit Garnelen;Vorspeisen;7,50;;ja\n"
        "13;Pho Bo;Suppen;11,90;;ja\n"
        "47;Ente knusprig;Hauptgerichte;15,50;;ja\n"
    ),
    OPTIONS_FILE: (
        "number;group_name;option_name;price_delta_eur;is_default;required;"
        "price_reason\n"
        "47;Fleisch;Ente;0,00;ja;ja;\n"
        "47;Fleisch;Huhn;-1,00;nein;ja;\n"
        "47;Beilage;Reis;0,00;ja;nein;\n"
        f"47;Beilage;Nudeln;3,00;nein;nein;{GRUND}\n"
    ),
    ALLERGENS_FILE: "number;allergen_codes;confirmed_by\n",
    ALIASES_FILE: (
        "number;alias\n"
        "23;Frühlingsrollen\n"
        "24;Sommerrollen\n"
        "13;Pho\n"
        "47;die knusprige Ente\n"
    ),
}

BEILAGE = [
    OptionGroup(
        group="Fleisch",
        required=True,
        options=[
            OptionOut(name="Ente", price_delta_cents=0, default=True),
            OptionOut(name="Huhn", price_delta_cents=-100, default=False),
        ],
    ),
    OptionGroup(
        group="Beilage",
        required=False,
        options=[
            OptionOut(name="Reis", price_delta_cents=0, default=True),
            OptionOut(
                name="Nudeln", price_delta_cents=300, default=False, reason=GRUND
            ),
        ],
    ),
]


# --- ohne Datenbank: trennen ---------------------------------------------------------


@pytest.mark.parametrize(
    ("gesagt", "gericht", "wunsch"),
    [
        ("die 23 ohne Karotten", "die 23", "ohne Karotten"),
        ("Pho Bo, ohne Koriander bitte", "Pho Bo", "ohne Koriander"),
        ("Nummer 23, keine Karotten bitte", "Nummer 23", "keine Karotten"),
        (
            "die knusprige Ente mit Nudeln statt Reis",
            "die knusprige Ente",
            "mit Nudeln statt Reis",
        ),
        ("die 47, Nudeln statt Reis", "die 47", "Nudeln statt Reis"),
        (
            "Pho Bo, ich habe eine Erdnussallergie",
            "Pho Bo",
            "ich habe eine Erdnussallergie",
        ),
        ("Nummer 23 extra scharf", "Nummer 23", "extra scharf"),
        # Die Suche entscheidet spaeter, ob "mit Garnelen" zum Namen gehoert.
        ("Sommerrollen mit Garnelen", "Sommerrollen", "mit Garnelen"),
        ("die 23", "die 23", None),
        ("zweimal Pho Bo", "zweimal Pho Bo", None),
    ],
)
def test_gericht_und_wunsch_trennen(gesagt, gericht, wunsch):
    assert split_wish(gesagt) == (gericht, wunsch)


# --- ohne Datenbank: einordnen -------------------------------------------------------


@pytest.mark.parametrize(
    "wunsch", ["ohne Karotten", "keine Zwiebeln", "kein Koriander"]
)
def test_weglassen_ist_ein_hinweis(wunsch):
    wish = classify_wish(wunsch, BEILAGE)
    assert wish.kind == "note"
    assert wish.price_delta_cents is None


def test_option_der_karte_mit_aufpreis_und_grund():
    wish = classify_wish("mit Nudeln statt Reis", BEILAGE)
    assert (wish.kind, wish.group, wish.option) == ("option", "Beilage", "Nudeln")
    assert wish.price_delta_cents == 300
    assert wish.reason == GRUND


def test_option_ohne_aufpreis_gilt_auch():
    wish = classify_wish("mit Huhn", BEILAGE)
    assert (wish.kind, wish.option, wish.price_delta_cents) == ("option", "Huhn", -100)


@pytest.mark.parametrize("wunsch", ["mit Pommes", "extra scharf", "Nudeln statt Reis"])
def test_was_die_karte_nicht_kennt_wird_nicht_angeboten(wunsch):
    """D8: ohne Option auf der Karte kein Angebot - auch "Nudeln statt Reis"
    nicht, wenn das Gericht keine Beilagen-Gruppe hat."""
    groups = [] if wunsch == "Nudeln statt Reis" else BEILAGE
    assert classify_wish(wunsch, groups).kind == "unknown"


@pytest.mark.parametrize(
    "wunsch", ["ich habe eine Erdnussallergie", "ich bin allergisch gegen Sesam"]
)
def test_allergie_ist_kein_wunsch(wunsch):
    assert classify_wish(wunsch, BEILAGE).kind == "allergy"


# --- mit Datenbank ---------------------------------------------------------------


@pytest.fixture
def session(migrated_db_url):
    engine = create_engine(migrated_db_url)
    with Session(engine) as s:
        yield s
    engine.dispose()


@pytest.fixture
def tenant_id(session) -> uuid.UUID:
    tid = uuid.UUID(
        seed(session, tenant_name="Wuensche", timezone="Europe/Berlin").tenant_id
    )
    plan = parse(KARTE_WUENSCHE)
    assert plan.ok, plan.errors
    apply(session, tid, plan, now=NOW)
    return tid


def suche(session, tenant_id, text):
    return search_menu(session, tenant_id, text, now=NOW)


def test_nummer_mit_hinweis_wird_gefunden(session, tenant_id):
    """Vorher: "Das habe ich auf der Karte nicht gefunden." obwohl die 23 fiel."""
    result = suche(session, tenant_id, "die 23 ohne Karotten")
    assert [h.number for h in result.results] == ["23"]
    assert (result.wish.kind, result.wish.text) == ("note", "ohne Karotten")


def test_name_mit_hinweis_wird_gefunden(session, tenant_id):
    result = suche(session, tenant_id, "Pho Bo, ohne Koriander bitte")
    assert [h.number for h in result.results] == ["13"]
    assert result.wish.kind == "note"


def test_option_der_karte_als_wunsch(session, tenant_id):
    result = suche(session, tenant_id, "die knusprige Ente mit Nudeln statt Reis")
    assert [h.number for h in result.results] == ["47"]
    assert (result.wish.kind, result.wish.option) == ("option", "Nudeln")
    assert result.wish.price_delta_cents == 300


def test_teil_des_namens_ist_kein_wunsch(session, tenant_id):
    """ "Sommerrollen mit Garnelen" heisst so - "mit Garnelen" ist kein Wunsch."""
    result = suche(session, tenant_id, "Sommerrollen mit Garnelen")
    assert [h.number for h in result.results] == ["24"]
    assert result.wish is None


def test_unbekannter_wunsch_wird_gesagt_nicht_angeboten(session, tenant_id):
    result = suche(session, tenant_id, "die 23 mit Pommes")
    assert [h.number for h in result.results] == ["23"]
    assert result.wish.kind == "unknown"
    assert "kann ich leider nicht anbieten" in result.say


def test_allergie_kommt_als_hinweis_ohne_zusage(session, tenant_id):
    result = suche(session, tenant_id, "Pho Bo, ich habe eine Erdnussallergie")
    assert [h.number for h in result.results] == ["13"]
    assert result.wish.kind == "allergy"
    assert "Küche" in result.say
    assert "frei" in result.say


def test_ohne_wunsch_bleibt_alles_wie_bisher(session, tenant_id):
    result = suche(session, tenant_id, "die 23")
    assert result.wish is None and result.say is None


def test_wunsch_bleibt_im_latenzbudget(session, tenant_id):
    assert (
        p95_ms(
            lambda: suche(
                session, tenant_id, "die knusprige Ente mit Nudeln statt Reis"
            )
        )
        < 300
    )


# --- Grund fuer den Aufpreis: Import und Details -----------------------------------


def test_grund_wird_importiert(session, tenant_id):
    reason = session.scalar(
        select(ItemOption.price_reason)
        .join(MenuItem, ItemOption.menu_item_id == MenuItem.id)
        .where(MenuItem.tenant_id == tenant_id, ItemOption.option_name == "Nudeln")
    )
    assert reason == GRUND


def test_optionsdatei_ohne_grund_spalte_geht_weiter(session):
    """Die Spalte ist optional: alte Dateien ohne sie importieren wie bisher."""
    karte = {
        **KARTE_WUENSCHE,
        OPTIONS_FILE: (
            "number;group_name;option_name;price_delta_eur;is_default;required\n"
            "47;Fleisch;Ente;0,00;ja;ja\n"
        ),
    }
    plan = parse(karte)
    assert plan.ok, plan.errors


def test_details_nennen_den_grund(session, tenant_id):
    ente = session.scalar(
        select(MenuItem.id).where(
            MenuItem.tenant_id == tenant_id, MenuItem.number == "47"
        )
    )
    details = get_item_details(
        session, tenant_id, ente, allergen_question=False, now=NOW
    )
    beilage = next(g for g in details.option_groups if g.group == "Beilage")
    nudeln = next(o for o in beilage.options if o.name == "Nudeln")
    assert nudeln.reason == GRUND
    reis = next(o for o in beilage.options if o.name == "Reis")
    assert reis.reason is None


SAUCE = [
    OptionGroup(
        group="Sauce",
        required=False,
        options=[
            OptionOut(name="süßsauer", price_delta_cents=0, default=True),
            OptionOut(name="Erdnuss", price_delta_cents=50, default=False),
        ],
    ),
    OptionGroup(
        group="Beilage",
        required=False,
        options=[
            OptionOut(name="Reis", price_delta_cents=0, default=True),
            OptionOut(name="Nudeln", price_delta_cents=300, default=False),
        ],
    ),
]


def test_option_als_wortzusammensetzung_mit_der_gruppe():
    """ "Erdnusssauce" ist Option Erdnuss der Gruppe Sauce."""
    wish = classify_wish("mit Erdnusssauce", SAUCE)
    assert (wish.kind, wish.group, wish.option) == ("option", "Sauce", "Erdnuss")


def test_kein_vorsilben_treffer():
    """ "Reisnudeln" ist nicht die Option Reis: nur Option plus Gruppenname zaehlt
    zusammengesetzt, kein beliebiger Wortanfang."""
    assert classify_wish("mit Reisnudeln", SAUCE).kind == "unknown"


# --- Allergie im festen Wortlaut (E14, Maxi 24.09.2026) ----------------------------


@pytest.mark.parametrize(
    ("gesagt", "hinweis"),
    [
        ("ich habe eine Erdnussallergie", "WICHTIG: Keine Erdnuss. Grund: Allergie"),
        ("ich bin allergisch gegen Sesam", "WICHTIG: Keine Sesam. Grund: Allergie"),
        ("ich vertrage keine Erdnüsse", "WICHTIG: Keine Erdnüsse. Grund: Allergie"),
        ("Allergie gegen Sellerie", "WICHTIG: Keine Sellerie. Grund: Allergie"),
    ],
)
def test_allergie_als_kuechenhinweis_im_festen_wortlaut(gesagt, hinweis):
    """E14: der Hinweis an die Kueche hat einen festen Wortlaut; er wird beim
    Vorlesen wiederholt, nie mit der Zusage, das Gericht sei frei davon."""
    wish = classify_wish(gesagt, BEILAGE)
    assert (wish.kind, wish.text) == ("allergy", hinweis)


def test_vertraegt_keine_ist_eine_allergie_kein_weglassen():
    """ "ich vertrage keine Erdnüsse" ist eine Allergie (docs/05 §Allergie), kein
    "keine Karotten" - der Satzteil beginnt am Komma."""
    assert split_wish("Pho Bo, ich vertrage keine Erdnüsse") == (
        "Pho Bo",
        "ich vertrage keine Erdnüsse",
    )


def test_allergie_ohne_zutat_wird_nachgefragt(session, tenant_id):
    result = suche(session, tenant_id, "Pho Bo, ich habe eine Allergie")
    assert result.wish.kind == "allergy"
    assert "Wogegen" in result.say


def test_allergie_in_der_suche_im_festen_wortlaut(session, tenant_id):
    result = suche(session, tenant_id, "Pho Bo, ich habe eine Erdnussallergie")
    assert result.wish.text == "WICHTIG: Keine Erdnuss. Grund: Allergie"


# --- Codex PR #139 -----------------------------------------------------------------


def test_allergie_ohne_komma_beginnt_am_satzteil():
    """Codex PR #139, P2: Spracherkennung setzt selten Kommas. "ich habe eine" gehoert
    zur Allergie, nicht zum Gericht."""
    assert split_wish("Pho Bo ich habe eine Erdnussallergie") == (
        "Pho Bo",
        "ich habe eine Erdnussallergie",
    )


def test_merkmal_im_namen_und_danach_ein_wunsch(session, tenant_id):
    """Codex PR #139, P2: "Sommerrollen mit Garnelen" heisst so; "ohne Koriander"
    danach ist der Wunsch."""
    result = suche(session, tenant_id, "Sommerrollen mit Garnelen ohne Koriander")
    assert [h.number for h in result.results] == ["24"]
    assert (result.wish.kind, result.wish.text) == ("note", "ohne Koriander")


def test_weglassen_einer_zutat_aus_dem_namen_ist_ein_hinweis(session, tenant_id):
    """Codex PR #139, P2: "Sommerrollen ohne Garnelen" - die Garnelen stehen im
    Namen, das "ohne" macht daraus trotzdem einen Hinweis fuer die Kueche."""
    result = suche(session, tenant_id, "Sommerrollen ohne Garnelen")
    assert [h.number for h in result.results] == ["24"]
    assert (result.wish.kind, result.wish.text) == ("note", "ohne Garnelen")


def test_option_mit_unbekanntem_rest_wird_nicht_still_verkuerzt():
    """Codex PR #139, P2: "mit Nudeln und Pommes" - Pommes kennt die Karte nicht.
    Nur Nudeln zu nehmen verschwiege die Pommes; der Wunsch gilt als unbekannt."""
    assert classify_wish("mit Nudeln und Pommes", BEILAGE).kind == "unknown"
    # Was nach "statt" steht, ist das Ersetzte, kein weiterer Wunsch.
    assert classify_wish("mit Nudeln statt Reis bitte", BEILAGE).kind == "option"
