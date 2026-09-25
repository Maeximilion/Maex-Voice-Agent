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
from api.domain.menu.wishes import classify_wish, wish_candidates
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
        "60;Pizza mit Salami;Pizza;9,50;;ja\n"
        "61;Pizza mit Pilzen;Pizza;9,00;;ja\n"
        "62;Pizza;Pizza;7,00;;ja\n"
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
        "62;Pizza\n"
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
    assert first_split(gesagt) == (gericht, wunsch)


def first_split(text):
    """Die erste Trennung, wie die Suche sie zuerst versucht."""
    candidates = wish_candidates(text)
    return (candidates[0][0], candidates[0][1]) if candidates else (text, None)


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
    assert first_split("Pho Bo, ich vertrage keine Erdnüsse") == (
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
    assert first_split("Pho Bo ich habe eine Erdnussallergie") == (
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


# --- Eigenes Review PR #139 --------------------------------------------------------


def test_weglassen_und_hinzufuegen_zusammen():
    """(1) "ohne Zwiebeln, dafür mit Nudeln": die Zugabe ist die Option mit
    Aufpreis, das Weglassen kommt als Hinweis mit - nie beides als freie Notiz."""
    wish = classify_wish("ohne Zwiebeln, dafür mit Nudeln", BEILAGE)
    assert (wish.kind, wish.option, wish.price_delta_cents) == ("option", "Nudeln", 300)
    assert wish.note == "ohne Zwiebeln"


def test_weglassen_und_unbekannte_zugabe():
    wish = classify_wish("ohne Zwiebeln, dafür mit Pommes", BEILAGE)
    assert wish.kind == "unknown"
    assert wish.text == "mit Pommes"
    assert wish.note == "ohne Zwiebeln"


def test_name_mit_klammern_gehoert_zum_namen():
    """(2) Satzzeichen im Namen: "Sommerrollen (mit Garnelen)"."""
    from api.domain.menu.wishes import names_it

    assert names_it("Sommerrollen (mit Garnelen)", "mit Garnelen")


def test_frage_nach_allergenen_ist_keine_eigene_allergie():
    """(3) "welche Allergene" fragt nach dem Gericht - das ist der Allergenpfad,
    kein Hinweis an die Kueche."""
    assert wish_candidates("die 23, welche Allergene sind drin") == []


def test_menge_am_ende_bleibt_nicht_im_hinweis():
    """(4) "zweimal" gehoert zur Position, nicht in den Hinweis fuer die Kueche."""
    wish = classify_wish("ohne Zwiebeln, zweimal", BEILAGE)
    assert (wish.kind, wish.text) == ("note", "ohne Zwiebeln")


@pytest.mark.parametrize(
    ("gesagt", "hinweis"),
    [
        # (5) Die Zutat endet am Satzteil.
        (
            "ich bin allergisch gegen Sesam und dann noch eine Cola",
            "WICHTIG: Keine Sesam. Grund: Allergie",
        ),
        # (6) Andere Wortstellung.
        ("ich bin gegen Nüsse allergisch", "WICHTIG: Keine Nüsse. Grund: Allergie"),
    ],
)
def test_zutat_der_allergie(gesagt, hinweis):
    assert classify_wish(gesagt, BEILAGE).text == hinweis


def test_option_in_zwei_gruppen_wird_nachgefragt():
    """(9) Reis als Beilage und als Extra: nie selbst waehlen, nachfragen."""
    groups = [
        *BEILAGE,
        OptionGroup(
            group="Extra",
            required=False,
            options=[OptionOut(name="Reis", price_delta_cents=200, default=False)],
        ),
    ]
    wish = classify_wish("mit Reis", groups)
    assert wish.kind == "open"
    assert wish.groups == ["Beilage", "Extra"]


def test_leere_felder_gehen_nicht_ans_modell():
    """(11) CLAUDE.md §2 Regel 6: keine "reason": null in jeder Option."""
    assert (
        "reason"
        not in OptionOut(name="Reis", price_delta_cents=0, default=True).model_dump()
    )
    note = classify_wish("ohne Karotten", BEILAGE).model_dump()
    assert set(note) == {"text", "kind"}


def test_zweite_trennung_ohne_neue_suche(session, tenant_id, monkeypatch):
    """(8, 10) Gehoert der erste Satzteil zum Namen, gilt der naechste Wunsch fuer
    dasselbe Gericht - keine zweite Suche, die scheitern koennte."""
    import api.domain.menu.search as search_module

    calls = []
    original = search_module.search_menu

    def counting(*args, **kwargs):
        calls.append(args[2])
        return original(*args, **kwargs)

    monkeypatch.setattr(search_module, "search_menu", counting)
    result = counting(
        session, tenant_id, "Sommerrollen mit Garnelen ohne Koriander", now=NOW
    )
    assert (result.wish.kind, result.wish.text) == ("note", "ohne Koriander")
    assert "Sommerrollen mit Garnelen" not in calls[1:]


def test_voller_name_statt_offener_wunsch(session, tenant_id):
    """Codex PR #139: "Pizza mit Salami" ist ein Gericht. Die Suche nur nach
    "Pizza" faende zwei und fragte nach - der ganze Name entscheidet."""
    result = suche(session, tenant_id, "Pizza mit Salami")
    assert [h.number for h in result.results] == ["60"]
    assert result.wish is None


def test_optionsname_mit_bindestrich():
    """Codex PR #139: "Süß-Sauer" als Option trifft den gesprochenen Wunsch."""
    groups = [
        OptionGroup(
            group="Sauce",
            required=False,
            options=[OptionOut(name="Süß-Sauer", price_delta_cents=0, default=False)],
        )
    ]
    assert classify_wish("mit Süß-Sauer", groups).kind == "option"


@pytest.mark.parametrize(
    ("gesagt", "hinweis"),
    [
        (
            "ich bin allergisch gegen Erdnüsse und Sesam",
            "WICHTIG: Keine Erdnüsse und Sesam. Grund: Allergie",
        ),
        (
            "ich vertrage keine Erdnüsse, Sesam und Soja",
            "WICHTIG: Keine Erdnüsse, Sesam und Soja. Grund: Allergie",
        ),
        (
            "allergisch gegen Sesam und dann noch eine Cola",
            "WICHTIG: Keine Sesam. Grund: Allergie",
        ),
    ],
)
def test_jede_zutat_einer_allergie_bleibt(gesagt, hinweis):
    """Codex PR #139, P1: eine Aufzaehlung von Zutaten endet nicht am ersten
    "und" - sonst fehlte eine Allergie im Hinweis an die Kueche. Erst ein neuer
    Satzteil ("und dann noch eine Cola") beendet sie."""
    assert classify_wish(gesagt, BEILAGE).text == hinweis


def test_ganzer_name_vor_eindeutigem_praefix(session, tenant_id):
    """Codex PR #139: neben "Pizza" steht "Pizza mit Salami" auf der Karte. Die
    Suche nach "Pizza" allein waere eindeutig - trotzdem ist "Pizza mit Salami"
    das genannte Gericht, kein Wunsch zur Pizza."""
    result = suche(session, tenant_id, "Pizza mit Salami")
    assert [h.number for h in result.results] == ["60"]
    assert result.wish is None


def test_wunsch_zur_einfachen_pizza_bleibt_wunsch(session, tenant_id):
    result = suche(session, tenant_id, "Pizza ohne Zwiebeln")
    assert [h.number for h in result.results] == ["62"]
    assert (result.wish.kind, result.wish.text) == ("note", "ohne Zwiebeln")


def test_ganzer_name_bei_mehrdeutigem_praefix_mit_zweitem_wunsch(session, tenant_id):
    """Codex PR #139, P2: ohne die einfache Pizza ist "Pizza" mehrdeutig. In
    "Pizza mit Salami ohne Zwiebeln" gilt der ganze Name, der zweite Satzteil
    bleibt Wunsch - sonst fiele "ohne Zwiebeln" nach der Rueckfrage weg."""
    pizza = session.scalar(
        select(MenuItem).where(MenuItem.tenant_id == tenant_id, MenuItem.number == "62")
    )
    pizza.active = False
    session.flush()
    result = suche(session, tenant_id, "Pizza mit Salami ohne Zwiebeln")
    assert [h.number for h in result.results] == ["60"]
    assert (result.wish.kind, result.wish.text) == ("note", "ohne Zwiebeln")


@pytest.mark.parametrize("gesagt", ["ohne extra Käse", "keine extra Nudeln"])
def test_extra_im_weglassen_ist_keine_zugabe(gesagt):
    """Codex PR #139, P2: "extra" direkt nach "ohne" gehoert zum Weglassen. Sonst
    kaeme die abgelehnte Zugabe als Option in die Bestellung."""
    wish = classify_wish(gesagt, BEILAGE)
    assert (wish.kind, wish.text, wish.option) == ("note", gesagt, None)
    assert first_split(f"die 47 {gesagt}") == ("die 47", gesagt)


def test_extra_nach_dem_weglassen_bleibt_zugabe():
    wish = classify_wish("ohne Zwiebeln, extra Nudeln", BEILAGE)
    assert (wish.kind, wish.option, wish.note) == ("option", "Nudeln", "ohne Zwiebeln")


@pytest.mark.parametrize(
    "gesagt",
    [
        "allergisch gegen weiß ich nicht",
        "allergisch gegen nein",
        "Allergie gegen keine Ahnung",
    ],
)
def test_keine_zutat_ist_keine_allergie_zutat(gesagt):
    """Codex PR #139, P1: Unsicherheit oder Ablehnung ist keine Zutat. Der Hinweis
    "Keine Weiß ich nicht" ginge an die Kueche, die Allergie bliebe unbekannt."""
    wish = classify_wish(gesagt, [])
    assert (wish.kind, wish.ingredient) == ("allergy", None)


@pytest.mark.parametrize(
    "gesagt", ["mit Nudeln ohne Zwiebeln", "mit Nudeln, aber ohne Zwiebeln"]
)
def test_weglassen_nach_der_option_bleibt(gesagt):
    """Codex PR #139, P2: die Option zuerst, das Weglassen danach - beides zaehlt,
    wie in der umgekehrten Reihenfolge."""
    wish = classify_wish(gesagt, BEILAGE)
    assert (wish.kind, wish.option, wish.note) == ("option", "Nudeln", "ohne Zwiebeln")


def test_unbekannte_zugabe_mit_weglassen_bleibt_unbekannt():
    assert classify_wish("mit Pommes ohne Zwiebeln", BEILAGE).kind == "unknown"


def test_option_und_weglassen_in_der_suche(session, tenant_id):
    result = suche(session, tenant_id, "die 47 mit Nudeln ohne Zwiebeln")
    assert [h.number for h in result.results] == ["47"]
    assert (result.wish.kind, result.wish.option, result.wish.note) == (
        "option",
        "Nudeln",
        "ohne Zwiebeln",
    )


def test_extra_zutat_aus_dem_namen_ist_wunsch(session, tenant_id):
    """Codex PR #139, P2: "extra Garnelen" steht nicht im Namen, nur die
    Garnelen. Der Wunsch bleibt und wird abgelehnt, statt still zu fehlen."""
    result = suche(session, tenant_id, "Sommerrollen extra Garnelen")
    assert [h.number for h in result.results] == ["24"]
    assert (result.wish.kind, result.wish.text) == ("unknown", "extra Garnelen")


@pytest.mark.parametrize(
    ("gesagt", "hinweis"),
    [
        (
            "ich habe eine Nuss- und Sesamallergie",
            "WICHTIG: Keine Nuss und Sesam. Grund: Allergie",
        ),
        (
            "ich habe eine Milchallergie und eine Nussallergie",
            "WICHTIG: Keine Milch und Nuss. Grund: Allergie",
        ),
        (
            "Erdnussallergie und allergisch gegen Sesam",
            "WICHTIG: Keine Erdnuss und Sesam. Grund: Allergie",
        ),
        ("ich habe eine Erdnuss-Allergie", "WICHTIG: Keine Erdnuss. Grund: Allergie"),
    ],
)
def test_jede_allergie_im_zusammengesetzten_namen(gesagt, hinweis):
    """Codex PR #139, P1: "Nuss- und Sesamallergie" sind zwei Allergien. Fehlt
    eine im Hinweis, erfaehrt die Kueche nichts davon."""
    assert classify_wish(gesagt, BEILAGE).text == hinweis


@pytest.mark.parametrize(
    ("gesagt", "hinweis"),
    [
        ("mit Laktoseintoleranz", "WICHTIG: Keine Laktose. Grund: Allergie"),
        (
            "ich habe eine Glutenunverträglichkeit",
            "WICHTIG: Keine Gluten. Grund: Allergie",
        ),
        (
            "Laktose- und Glutenintoleranz",
            "WICHTIG: Keine Laktose und Gluten. Grund: Allergie",
        ),
        ("ich bin intolerant gegen Laktose", "WICHTIG: Keine Laktose. Grund: Allergie"),
    ],
)
def test_intoleranz_ist_allergie(gesagt, hinweis):
    """Codex PR #139, P1: "Intoleranz" und "Unvertraeglichkeit" sind Allergie-
    Rede (docs/17), kein unbekannter Wunsch."""
    wish = classify_wish(gesagt, BEILAGE)
    assert (wish.kind, wish.text) == ("allergy", hinweis)


def test_intoleranz_in_der_suche(session, tenant_id):
    result = suche(session, tenant_id, "die 23 mit Laktoseintoleranz")
    assert [h.number for h in result.results] == ["23"]
    assert result.wish.text == "WICHTIG: Keine Laktose. Grund: Allergie"


@pytest.mark.parametrize(
    "gesagt",
    [
        "ich bin gegen Erdnüsse allergisch und gegen Sesam allergisch",
        "allergisch gegen Erdnüsse und allergisch gegen Sesam",
        "allergisch gegen Erdnüsse und gegen Sesam allergisch",
        "allergisch gegen Erdnüsse, Allergie gegen Sesam",
        "allergisch gegen Erdnüsse und Sesamallergie",
    ],
)
def test_jede_wiederholte_allergie_bleibt(gesagt):
    """Codex PR #139, P1: dieselbe Wendung zweimal ("gegen X allergisch und gegen
    Y allergisch") - jede Zutat kommt in den Hinweis, nicht nur die erste."""
    wish = classify_wish(gesagt, BEILAGE)
    assert wish.ingredient in ("Erdnüsse und Sesam",)
