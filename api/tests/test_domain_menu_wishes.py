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
from api.domain.menu.search import position_parts, search_menu
from api.domain.menu.wishes import (
    classify_wish,
    denies_allergy,
    kitchen_note,
    open_wish,
    removal_with,
    wish_candidates,
)
from api.models import ItemOption, MenuItem
from api.schemas.menu import OptionGroup, OptionOut
from api.schemas.orders import NOTE_MAX
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
    assert "frei" in result.say
    # No promise to pass it on: the core asks for consent first (T-11.1, L13).
    assert "Küche" not in result.say


def test_search_keeps_the_removal_before_the_allergy(session, tenant_id):
    result = suche(
        session, tenant_id, "Pho Bo ohne Zwiebeln, ich habe eine Erdnussallergie"
    )
    assert [h.number for h in result.results] == ["13"]
    assert (result.wish.kind, result.wish.text) == (
        "allergy",
        "WICHTIG: Keine Erdnuss. Grund: Allergie. Ohne Zwiebeln",
    )
    assert "frei davon" in result.say


def test_search_finds_the_dish_before_gegen_x_bin_ich_allergisch(session, tenant_id):
    result = suche(session, tenant_id, "Pho Bo, gegen Erdnüsse bin ich allergisch")
    assert [h.number for h in result.results] == ["13"]
    assert (result.wish.kind, result.wish.ingredient) == ("allergy", "Erdnüsse")


@pytest.mark.parametrize(
    "said",
    [
        "Pho Bo, ich habe keine Erdnussallergie",
        "die 13, ich habe keine Allergie",
        "Pho Bo, keine Allergie",
        "die 13, nicht allergisch",
        "Pho Bo, kein Allergiker",
        "Pho Bo, eine Erdnussallergie habe ich nicht",
        "Pho Bo, gegen Erdnüsse bin ich nicht allergisch",
        "Pho Bo, ich bin allergisch gegen nichts",
    ],
)
def test_search_with_a_denied_allergy_has_no_wish(session, tenant_id, said):
    result = suche(session, tenant_id, said)
    assert [h.number for h in result.results] == ["13"]
    assert result.wish is None and result.say is None


def test_ohne_wunsch_bleibt_alles_wie_bisher(session, tenant_id):
    result = suche(session, tenant_id, "die 23")
    assert result.wish is None and result.say is None


@pytest.mark.latency
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


@pytest.mark.parametrize(
    ("gesagt", "zutaten"),
    [
        ("ich habe eine Erdnuss und Sesamallergie", "Erdnuss und Sesam"),
        ("Milch, Ei und Nussallergie", "Milch, Ei und Nuss"),
        ("Laktose und Glutenintoleranz", "Laktose und Gluten"),
        ("ich habe eine Sesamallergie", "Sesam"),
    ],
)
def test_zusammengesetzte_allergie_ohne_bindestrich(gesagt, zutaten):
    """Codex PR #139, P1: die Erkennung schreibt "Erdnuss und Sesamallergie"
    ohne Bindestrich - die Erdnuss gehoert trotzdem in den Hinweis. Ein
    Gericht davor ist keine Zutat."""
    assert classify_wish(gesagt, BEILAGE).ingredient == zutaten


def test_ganzer_name_vor_aber(session, tenant_id):
    """Codex PR #139, P2: "Pizza mit Salami, aber ohne Zwiebeln" - das "aber"
    gehoert nicht zum Namen, die Salami-Pizza bleibt das Gericht."""
    result = suche(session, tenant_id, "Pizza mit Salami, aber ohne Zwiebeln")
    assert [h.number for h in result.results] == ["60"]
    assert (result.wish.kind, result.wish.text) == ("note", "ohne Zwiebeln")


@pytest.mark.parametrize("gesagt", ["allergiefrei", "ist das allergenfrei"])
def test_allergiefrei_ist_keine_eigene_allergie(gesagt):
    """Codex PR #139, P2: "allergiefrei?" fragt nach dem Gericht (Allergenpfad),
    keine eigene Allergie - sonst kaeme "Wogegen sind Sie allergisch?"."""
    assert classify_wish(gesagt, BEILAGE).kind != "allergy"


@pytest.mark.parametrize(
    ("gesagt", "zutaten"),
    [
        ("allergisch gegen Erdnüsse und auch Sesam", "Erdnüsse und Sesam"),
        ("allergisch gegen Erdnüsse und noch Sesam", "Erdnüsse und Sesam"),
        ("allergisch gegen Sesam und auch noch eine Cola", "Sesam"),
    ],
)
def test_beiwort_nach_und_beendet_die_liste_nicht(gesagt, zutaten):
    """Codex PR #139, P1: "und auch Sesam" - das "auch" beendet die Liste der
    Zutaten nicht; sonst fehlte der Sesam im Hinweis."""
    assert classify_wish(gesagt, BEILAGE).ingredient == zutaten


@pytest.mark.parametrize(
    "gesagt",
    [
        "eine Pho mit einer Erdnussallergie und einer Sesamallergie",
        "eine Pho mit Erdnussallergie und eine Sesamallergie habe ich auch",
    ],
)
def test_allergie_mit_artikel_bleibt_beim_gericht(session, tenant_id, gesagt):
    """Codex PR #139, P1: "und einer Sesamallergie" ist keine neue Position,
    sondern die zweite Allergie zur Pho - sonst fehlte der Sesam im Hinweis."""
    assert position_parts(session, tenant_id, gesagt, now=NOW) == [gesagt]
    result = suche(session, tenant_id, gesagt)
    assert [h.number for h in result.results] == ["13"]
    assert result.wish.ingredient == "Erdnuss und Sesam"


def test_zweites_gericht_mit_allergie_bleibt_eigene_position(session, tenant_id):
    parts = position_parts(
        session,
        tenant_id,
        "Pho Bo mit Allergie und Frühlingsrollen mit Allergie",
        now=NOW,
    )
    assert parts == ["Pho Bo mit Allergie", "Frühlingsrollen mit Allergie"]


@pytest.mark.parametrize(
    "gesagt",
    ["ich habe eine Nahrungsmittelallergie", "ich habe eine Lebensmittelallergie"],
)
def test_allgemeine_allergie_fragt_nach(gesagt):
    """Review PR #139: "Nahrungsmittel" ist keine Zutat - die Frage "Wogegen?"
    bleibt offen, statt "Keine Nahrungsmittel" an die Kueche zu geben."""
    wish = classify_wish(gesagt, [])
    assert (wish.kind, wish.ingredient) == ("allergy", None)


# --- Allergy clause next to a wish, denied allergy (issue #252) ----------------------


@pytest.mark.parametrize(
    ("said", "note", "ingredient"),
    [
        (
            "ohne Zwiebeln, ich habe eine Erdnussallergie",
            "WICHTIG: Keine Erdnuss. Grund: Allergie. Ohne Zwiebeln",
            "Erdnuss",
        ),
        (
            "ohne Zwiebeln ich habe eine Erdnussallergie",
            "WICHTIG: Keine Erdnuss. Grund: Allergie. Ohne Zwiebeln",
            "Erdnuss",
        ),
        (
            "ohne Zwiebeln und ich bin allergisch gegen Sesam",
            "WICHTIG: Keine Sesam. Grund: Allergie. Ohne Zwiebeln",
            "Sesam",
        ),
        (
            "keine Zwiebeln, ich vertrage keine Erdnüsse",
            "WICHTIG: Keine Erdnüsse. Grund: Allergie. Keine Zwiebeln",
            "Erdnüsse",
        ),
        (
            "ohne Zwiebeln, weil ich eine Erdnussallergie habe",
            "WICHTIG: Keine Erdnuss. Grund: Allergie. Ohne Zwiebeln",
            "Erdnuss",
        ),
        # Without a comma or "ich" the phrase still begins at its own words
        # (own review): none of them goes into the removal.
        (
            "ohne Zwiebeln hab eine Erdnussallergie",
            "WICHTIG: Keine Erdnuss. Grund: Allergie. Ohne Zwiebeln",
            "Erdnuss",
        ),
        (
            "ohne Zwiebeln und eine Erdnussallergie",
            "WICHTIG: Keine Erdnuss. Grund: Allergie. Ohne Zwiebeln",
            "Erdnuss",
        ),
        (
            "ohne Zwiebeln bin allergisch gegen Nüsse",
            "WICHTIG: Keine Nüsse. Grund: Allergie. Ohne Zwiebeln",
            "Nüsse",
        ),
        (
            "ohne Zwiebeln gegen Nüsse allergisch",
            "WICHTIG: Keine Nüsse. Grund: Allergie. Ohne Zwiebeln",
            "Nüsse",
        ),
        # The removal that comes with an addition stays too; the addition
        # itself is not taken next to an allergy (docs/01_STATUS.md, open point).
        (
            "ohne Zwiebeln, dafür mit Nudeln, ich habe eine Erdnussallergie",
            "WICHTIG: Keine Erdnuss. Grund: Allergie. Ohne Zwiebeln",
            "Erdnuss",
        ),
    ],
)
def test_removal_before_the_allergy_is_kept(said, note, ingredient):
    """Codex PR #139 (P2): "ohne Zwiebeln, ich habe eine Erdnussallergie" gave
    only the allergy note, the removal was dropped. The kitchen note now names
    both, the allergy first in the fixed wording of E14, then the removal
    as the guest said it."""
    wish = classify_wish(said, BEILAGE)
    assert (wish.kind, wish.text, wish.ingredient, wish.note) == (
        "allergy",
        note,
        ingredient,
        None,
    )


def test_removal_before_an_allergy_without_ingredient_waits_in_note():
    """Without an ingredient the agent asks "Wogegen?" first; the removal waits
    in `note` and goes after the note made from the answer."""
    wish = classify_wish("ohne Zwiebeln, ich habe eine Allergie", BEILAGE)
    assert (wish.kind, wish.ingredient, wish.note) == (
        "allergy",
        None,
        "ohne Zwiebeln",
    )
    assert wish.text == "ich habe eine Allergie"


@pytest.mark.parametrize(
    ("removal", "allergy", "note"),
    [
        ("ohne Zwiebeln", "WICHTIG: Keine Erdnuss. Grund: Allergie", None),
        (None, "WICHTIG: Keine Erdnuss. Grund: Allergie", None),
        # Too long for an order line: the allergy stays, the removal goes.
        (
            "ohne " + "Zwiebeln, " * 20 + "Koriander",
            "WICHTIG: Keine Erdnuss. Grund: Allergie",
            "WICHTIG: Keine Erdnuss. Grund: Allergie",
        ),
    ],
)
def test_kitchen_note_names_the_allergy_first(removal, allergy, note):
    """The allergy first, as the kitchen reads it (E14), then the removal."""
    expected = note or (
        f"{allergy}. {removal[0].upper()}{removal[1:]}" if removal else allergy
    )
    assert kitchen_note(removal, allergy) == expected


@pytest.mark.parametrize(
    "said",
    [
        "ich habe keine Erdnussallergie",
        "ich bin nicht gegen Erdnüsse allergisch",
        "ich bin nicht allergisch gegen Erdnüsse",
        "ich bin nicht allergisch gegen Erdnüsse und Sesam",
        "ich habe keine Allergie",
        "keine Allergie",
        "nicht allergisch",
        "ich habe doch gar keine Allergie",
        "ich habe keinerlei Allergien",
        "ich bin kein Allergiker",
        "ich habe keine Laktoseintoleranz",
        "ich bin gegen nichts allergisch",
        "ich bin auf nichts allergisch",
        # Round 6.
        "ich bin gegen keine Nüsse allergisch",
        "Gegen nichts allergisch",
        # Round 7: the usual phrases.
        "ich habe keine Allergien oder Unverträglichkeiten",
        "keine Allergien und keine Unverträglichkeiten",
        "Keine Allergie habe ich",
        "ich bin gegen gar nichts allergisch",
        "ich habe keine Allergie oder so",
        # Codex PR #330.
        "ich habe weder Allergien noch Unverträglichkeiten",
        "Weder Allergien noch Unverträglichkeiten",
        "ich habe weder eine Allergie noch eine Unverträglichkeit",
        "ich bin weder gegen Erdnüsse noch gegen Soja allergisch",
        # Codex PR #330, round 2: a modifier before the noun.
        "ich habe keine bekannten Allergien",
        "keine bekannten Allergien oder Unverträglichkeiten",
        "Mir sind keine Allergien bekannt",
        "Mir ist keine Allergie bekannt",
        "Keine Allergien sind mir bekannt",
        "ich habe keine weitere Allergie",
        "ich habe keine anderen Allergien",
        "Keine Allergie ist mir bekannt",
        "Allergien sind mir keine bekannt",
        # Codex PR #330: the negation after the noun.
        "eine Erdnussallergie habe ich nicht",
        "Allergien habe ich keine",
        "gegen Erdnüsse bin ich nicht allergisch",
        "eine Erdnussallergie habe ich gar nicht",
        "gegen nichts bin ich allergisch",
        "gegen gar nichts bin ich allergisch",
        "ich bin allergisch gegen nichts",
        "ich bin allergisch gegen gar nichts",
        "ich bin gegen Erdnüsse nicht allergisch",
        "ich habe keine Allergie oder sonstige Unverträglichkeiten",
        "weder eine bekannte Allergie noch eine Unverträglichkeit",
        "weder eine bekannte Erdnussallergie noch eine Sesamallergie",
        "ich habe keine weiteren Allergien",
        "weder bekannte Allergien noch Unverträglichkeiten",
        # Answers to "Wogegen?" that take the allergy back (own review).
        "Nein, ich habe keine Allergie",
        "Doch, keine Allergie",
        "ich habe keine Allergie, danke",
        "ich habe keine Allergie, das wars",
    ],
)
def test_denied_allergy_is_no_wish(said):
    """Codex PR #139 (P2): "ich habe keine Erdnussallergie" gave the note
    "WICHTIG: Keine Erdnuss", and "ich habe keine Allergie" the question
    "Wogegen?". A phrase that only denies an allergy is nothing to note."""
    assert classify_wish(said, BEILAGE) is None


@pytest.mark.parametrize(
    ("said", "kind", "text"),
    [
        ("ohne Zwiebeln, ich habe keine Allergie", "note", "ohne Zwiebeln"),
        ("ohne Zwiebeln, keine Allergie", "note", "ohne Zwiebeln"),
        ("ohne Zwiebeln keine Erdnussallergie", "note", "ohne Zwiebeln"),
        ("ohne Zwiebeln, aber keine Allergie", "note", "ohne Zwiebeln"),
        ("ohne Zwiebeln, weil ich keine Allergie habe", "note", "ohne Zwiebeln"),
        # The nearest "ich" begins the denial, not "meine" (own review).
        (
            "ohne Zwiebeln für meine Frau ich habe keine Allergie",
            "note",
            "ohne Zwiebeln für meine Frau",
        ),
        ("ich habe keine Allergie, ohne Zwiebeln, sonst nichts", "note", None),
        ("ich habe keine Allergie, nur ohne Zwiebeln", "note", "ohne Zwiebeln"),
        (
            "ohne Zwiebeln für meinen Bekannten ich habe keine Allergie",
            "note",
            "ohne Zwiebeln für meinen Bekannten",
        ),
        # A full stop the words do not show (own review).
        ("ich habe keine Allergie. Ohne Koriander bitte.", "note", "Ohne Koriander"),
        ("mit Nudeln, ich bin nicht allergisch", "option", "mit Nudeln"),
        # A wish after the denial (own review).
        (
            "ich habe keine Allergie, aber bitte ohne Koriander",
            "note",
            "ohne Koriander",
        ),
        (
            "ohne Zwiebeln, ich habe keine Allergie, aber ohne Koriander",
            "note",
            "ohne Zwiebeln, ohne Koriander",
        ),
    ],
)
def test_denied_allergy_keeps_the_wishes_around_it(said, kind, text):
    wish = classify_wish(said, BEILAGE)
    assert (wish.kind, wish.text if text else None) == (kind, text)


def test_denied_allergy_keeps_an_option_in_two_groups():
    """Own review: the option to ask about turned into "Wogegen?"."""
    reis = OptionOut(name="Reis", price_delta_cents=0, default=False)
    groups = [
        OptionGroup(group="Beilage", required=False, options=[reis]),
        OptionGroup(group="Extra", required=False, options=[reis]),
    ]
    wish = classify_wish("mit Reis, ich habe keine Allergie", groups)
    assert (wish.kind, wish.groups) == ("open", ["Beilage", "Extra"])


@pytest.mark.parametrize(
    "said",
    [
        # Own review: each of these named the denied ingredient, missed the
        # real one, or lost the allergy altogether.
        "ich habe keine Allergie gegen Erdnüsse, aber gegen Sesam",
        "ich bin nicht allergisch gegen Erdnüsse, sondern gegen Sesam",
        "ich bin nicht gegen Nüsse allergisch, gegen Sesam",
        "ich habe keine Allergie gegen Erdnüsse, gegen Sesam schon",
        "keine Allergie gegen Nüsse, eher gegen Sesam",
        "ich habe keine Allergie gegen Erdnüsse eher Sesam",
        "ich bin nicht gegen Erdnüsse allergisch, ich bin Sesam allergisch",
        "nicht gegen Nüsse, auf Sesam allergisch",
        "ich habe keine Erdnussallergie, aber eine Sesamallergie",
        "ich bin gegen Nüsse allergisch, nicht gegen Sesam",
        "ich habe keine Allergie, aber eine Frage",
        # Own review, round 3: an allergen around the denial.
        "gegen Sesam schon, aber keine Allergie gegen Nüsse",
        "keine Allergie, aber mit Gluten habe ich Probleme",
        "ich bin nicht allergisch, ohne Erdnüsse, weil ich die nicht vertrage",
        # Round 4: an intolerance before the denial.
        "Laktose vertrage ich überhaupt nicht, ich bin aber nicht allergisch",
        "Gluten verträgt mein Magen nicht, ich habe keine Allergie",
        # Round 6: a "vertrage" whose ingredient comes before it.
        "Erdnussallergie, aber Sesam vertrage ich nicht",
        # Own review of the Codex fix: "weder" mixed with more.
        "ich bin weder gegen Erdnüsse noch gegen Soja allergisch, sondern gegen Sesam",
        "ich bin weder gegen Sesam allergisch noch gegen Soja, aber gegen Erdnüsse",
        "ich habe weder eine Erdnussallergie noch eine Sojaallergie, aber eine Sesamallergie",
        "ich vertrage weder Gluten noch Laktose",
        "ich habe keine Erdnussallergie und eine Sesamallergie",
        "ich habe keine Erdnussallergie und eine bekannte Sesamallergie",
        "ich bin kein Allergiker und laktoseintolerant",
        # Own review of the Codex round 2 fix: a generic denial next to more.
        "ich habe keine bekannten Allergien, aber eine Sesamallergie",
        "ich habe keine Allergie und eine Laktoseintoleranz",
        "ich habe keine Allergie und Laktoseintoleranz",
        "Gegen Erdnüsse bin ich nicht allergisch, aber gegen Sesam",
        "ich habe keine Allergie oder Unverträglichkeit gegen Laktose, aber gegen Gluten",
        "ich bin weder allergisch noch intolerant gegen Laktose, aber gegen Gluten",
        # A denial goes on only with a negation of its own (own review): an
        # allergy said after "oder"/"und" is asked about.
        "ich habe keine Allergie oder eine Unverträglichkeit",
        "keine Allergie oder eine Allergie gegen Sesam",
        "ich habe keine Erdnussallergie oder eine Sesamallergie",
        "ich habe keine Erdnussallergie und weitere Allergien gegen Sesam",
        "ich habe keine Nussallergie und andere Allergien",
        "ich habe keine Nussallergie und Allergien gegen Sesam",
        "eine Erdnussallergie habe ich nicht, aber eine Sesamallergie",
        "eine Erdnussallergie habe ich gar nicht aber gegen Sesam",
        "eine Allergie gegen Erdnüsse habe ich nicht aber gegen Sesam",
        "Nur Erdnüsse, gegen Sesam bin ich nicht allergisch",
        "keine Erdnussallergie oder Allergie gegen Sesam",
    ],
)
def test_negation_mixed_with_more_asks_for_the_ingredient(said):
    """A negation next to more than a plain denial: which ingredient is meant
    is not read from the words but asked ("Wogegen?"). The denied one never
    goes to the kitchen, the real one is never lost."""
    wish = classify_wish(said, BEILAGE)
    assert (wish.kind, wish.ingredient) == ("allergy", None)


@pytest.mark.parametrize(
    ("said", "ingredient"),
    [
        # "nicht nur" does not deny.
        ("ich bin nicht nur gegen Erdnüsse allergisch", "Erdnüsse"),
        # The negation that names the allergy itself.
        ("ich vertrage keine Erdnüsse", "Erdnüsse"),
        # "nicht nur", and "keine" that begins a removal (own review).
        ("eine Erdnussallergie hab ich nicht nur", "Erdnuss"),
        ("eine Erdnussallergie habe ich keine Erdnüsse bitte", "Erdnuss"),
        ("ich habe eine Erdnussallergie, weitere Allergien habe ich keine", "Erdnuss"),
        # Not right in front: unsure, so the allergy stays.
        ("ich habe nicht eine Erdnussallergie", "Erdnuss"),
    ],
)
def test_negation_that_does_not_deny_keeps_the_allergy(said, ingredient):
    wish = classify_wish(said, BEILAGE)
    assert (wish.kind, wish.ingredient) == ("allergy", ingredient)


def test_removal_next_to_a_denial_that_asks_stays():
    """Own review, round 5: when the denial falls through to "Wogegen?", the
    removal said with it stays."""
    wish = classify_wish(
        "ich habe keine Allergie, aber ohne Koriander, dafür mit Pommes", BEILAGE
    )
    assert (wish.kind, wish.ingredient, wish.note) == (
        "allergy",
        None,
        "ohne Koriander",
    )


def test_denial_next_to_an_addition_waits_for_the_dish():
    """Own review, round 5: with several hits the options are not known yet,
    so the addition is decided after the choice, not asked about."""
    wish = open_wish("mit Nudeln, ich habe keine Allergie")
    assert wish.kind == "open"
    assert classify_wish(wish.text, BEILAGE).kind == "option"


def test_removal_without_a_tab():
    wish = classify_wish("ohne Zwiebeln\tich habe eine Erdnussallergie", [])
    assert wish.text == "WICHTIG: Keine Erdnuss. Grund: Allergie. Ohne Zwiebeln"


@pytest.mark.parametrize(
    ("said", "text", "note"),
    [
        (
            "ich bin allergisch gegen Erdnüsse, aber ohne Zwiebeln",
            "WICHTIG: Keine Erdnüsse. Grund: Allergie. Ohne Zwiebeln",
            None,
        ),
        (
            "ich habe eine Erdnussallergie, ohne Zwiebeln bitte",
            "WICHTIG: Keine Erdnuss. Grund: Allergie. Ohne Zwiebeln",
            None,
        ),
        (
            "ohne Koriander, ich habe eine Erdnussallergie, ohne Zwiebeln",
            "WICHTIG: Keine Erdnuss. Grund: Allergie. Ohne Koriander, ohne Zwiebeln",
            None,
        ),
        (
            "ich habe eine Allergie, ohne Zwiebeln",
            "ich habe eine Allergie",
            "ohne Zwiebeln",
        ),
        # Codex PR #330, round 8: "aber keine".
        (
            "ich habe eine Erdnussallergie, aber keine Zwiebeln",
            "WICHTIG: Keine Erdnuss. Grund: Allergie. Keine Zwiebeln",
            None,
        ),
        # Between two allergies, and up to a new dish (own review).
        (
            "ich habe eine Erdnussallergie, ohne Zwiebeln, und eine Sesamallergie",
            "WICHTIG: Keine Erdnuss und Sesam. Grund: Allergie. Ohne Zwiebeln",
            None,
        ),
        # Codex PR #330, round 3: before every later allergy.
        (
            "ich habe eine Erdnussallergie, aber ohne Zwiebeln sowie eine Sesamallergie",
            "WICHTIG: Keine Erdnuss und Sesam. Grund: Allergie. Ohne Zwiebeln",
            None,
        ),
        (
            "ich habe eine Erdnussallergie, ohne Zwiebeln oder gegen Sesam bin ich auch allergisch",
            "WICHTIG: Keine Erdnuss und Sesam. Grund: Allergie. Ohne Zwiebeln",
            None,
        ),
        (
            "ich habe eine Erdnussallergie, ohne Zwiebeln. Ich habe auch eine Sesamallergie",
            "WICHTIG: Keine Erdnuss und Sesam. Grund: Allergie. Ohne Zwiebeln",
            None,
        ),
        (
            "ich habe eine Erdnussallergie, ohne Zwiebeln, und die Sommerrollen",
            "WICHTIG: Keine Erdnuss. Grund: Allergie. Ohne Zwiebeln",
            None,
        ),
        (
            "ich habe eine Allergie gegen Erdnüsse, also ohne Erdnusssauce",
            "WICHTIG: Keine Erdnüsse. Grund: Allergie. Ohne Erdnusssauce",
            None,
        ),
    ],
)
def test_removal_after_the_allergy_is_kept(said, text, note):
    """Codex PR #330: "allergisch gegen Erdnuesse, aber ohne Zwiebeln" lost the
    removal behind the allergy."""
    wish = classify_wish(said, BEILAGE)
    assert (wish.kind, wish.text, wish.note) == ("allergy", text, note)


def test_addition_after_the_ingredient_is_no_ingredient():
    """Own review, round 7: "mit Nudeln" went into the allergy note unpriced."""
    wish = classify_wish("ich bin allergisch gegen Erdnüsse, mit Nudeln", BEILAGE)
    assert (wish.kind, wish.ingredient) == ("allergy", "Erdnüsse")


@pytest.mark.parametrize(
    ("said", "ingredient"),
    [
        # "sonst keine Allergien" at the end denies no ingredient.
        ("ich habe eine Erdnussallergie, sonst keine bekannten Allergien", "Erdnuss"),
        (
            "ich habe eine Erdnussallergie, sonst keine anderen Allergien bekannt",
            "Erdnuss",
        ),
        ("ich habe eine Erdnussallergie, sonst keine Allergien, danke", "Erdnuss"),
        (
            "ich habe eine Erdnussallergie, sonst keine Allergien oder Unverträglichkeiten",
            "Erdnuss",
        ),
        # "richtige" is a hedge, not a denial (own review).
        ("ich habe keine richtige Allergie gegen Nüsse", "Nüsse"),
    ],
)
def test_generic_denial_next_to_a_named_allergy(said, ingredient):
    wish = classify_wish(said, BEILAGE)
    assert (wish.kind, wish.ingredient) == ("allergy", ingredient)


@pytest.mark.parametrize(
    "said",
    [
        "ich bin allergisch gegen Erdnüsse und habe eine Sesamallergie",
        "ich bin allergisch gegen Erdnüsse, habe eine Sesamallergie",
    ],
)
def test_no_joiner_left_in_the_note(said):
    """Codex PR #330, round 7: "Keine Erdnuesse und und Sesam"."""
    assert classify_wish(said, []).ingredient == "Erdnüsse und Sesam"


def test_sonst_does_not_end_the_ingredients():
    """Own review: "sonst" ended the list and the sesame was lost."""
    wish = classify_wish(
        "ich bin allergisch gegen Erdnüsse, sonst auch gegen Sesam", []
    )
    assert "Sesam" in wish.ingredient


def test_negation_before_the_phrase_keeps_its_ingredient():
    """Own review: a "nicht auf" in the wish before the allergy cancelled the
    ingredient the guest said clearly; only the phrase itself is read then."""
    wish = classify_wish(
        "die Soße nicht auf den Reis, ich habe eine Erdnussallergie", []
    )
    assert (wish.kind, wish.ingredient) == ("allergy", "Erdnuss")


@pytest.mark.parametrize(
    ("said", "removal"),
    [
        ("ohne Zwiebeln, ich habe eine Erdnussallergie", "ohne Zwiebeln"),
        ("ohne Zwiebeln, ich habe eine Allergie", "ohne Zwiebeln"),
        ("ich habe eine Erdnussallergie", None),
        ("ich habe eine Allergie", None),
    ],
)
def test_removal_with_an_allergy_is_repeated(said, removal):
    assert removal_with(classify_wish(said, [])) == removal


@pytest.mark.parametrize(
    "said", ["Pho Bo bin allergisch gegen Nüsse", "Pho Bo hab eine Erdnussallergie"]
)
def test_dish_ends_where_the_allergy_phrase_begins(said):
    """Own review: the search got "Pho Bo bin" and "Pho Bo hab eine"."""
    assert first_split(said)[0] == "Pho Bo"


@pytest.mark.parametrize(
    ("said", "denied"),
    [
        # Only the clause, as the stand-in asks (round 4): "nichts" in front.
        ("ich habe keine Allergie", True),
        ("sonst nichts, ich habe keine Allergie", False),
        # The whole opening is no plain denial (the dish is in it); the
        # stand-in asks about the clause the search classifies.
        ("Ich moechte Pho Bo zum Abholen, ich habe keine Allergie", False),
        # Own review: an ingredient before the denial is not dropped.
        ("Erdnüsse, eine Sesamallergie habe ich nicht", False),
        # Round 5: the same answer as classify_wish.
        ("ich habe keine Allergie, aber mit Erdnüssen habe ich Probleme", False),
        ("ich habe eine Erdnussallergie", False),
        ("ich habe keine Allergie gegen Erdnüsse, aber gegen Sesam", False),
        ("allergiefrei?", False),
    ],
)
def test_denies_allergy(said, denied):
    """The stand-in asks this before it sends an opening with an allergy to
    the team (own review): the nearest "ich" begins the denial."""
    assert denies_allergy(said) is denied


@pytest.mark.parametrize(
    ("said", "note"),
    [
        (
            "ohne Zwiebeln für uns ich habe eine Erdnussallergie",
            "WICHTIG: Keine Erdnuss. Grund: Allergie. Ohne Zwiebeln für uns",
        ),
    ],
)
def test_words_of_the_allergy_phrase_stay_out_of_the_removal(said, note):
    assert classify_wish(said, []).text == note


def test_auf_in_a_removal_stays_with_it():
    """Own review: the "auf" of "ohne Zwiebeln auf Reis" was moved into the
    allergy clause."""
    wish = classify_wish("ohne Zwiebeln auf Reis ich habe eine Erdnussallergie", [])
    assert (
        wish.text == "WICHTIG: Keine Erdnuss. Grund: Allergie. Ohne Zwiebeln auf Reis"
    )


def test_gegen_of_the_dish_name_is_no_allergy_phrase():
    """Own review: "allergisch" jumped back to the "auf" of the dish."""
    assert first_split("Ente auf Thai Art bin allergisch gegen Nüsse")[0] == (
        "Ente auf Thai Art"
    )


@pytest.mark.parametrize(
    ("said", "note"),
    [
        (
            "mit Soße auf dem Reis ohne Zwiebeln bin allergisch gegen Erdnüsse",
            "WICHTIG: Keine Erdnüsse. Grund: Allergie. Ohne Zwiebeln",
        ),
        # Round 6: the verb at the end of the clause is no ingredient.
        (
            "ohne Zwiebeln, weil ich allergisch gegen Erdnüsse bin",
            "WICHTIG: Keine Erdnüsse. Grund: Allergie. Ohne Zwiebeln",
        ),
        (
            "ohne Zwiebeln für meine Frau und ich habe eine Erdnussallergie",
            "WICHTIG: Keine Erdnuss. Grund: Allergie. Ohne Zwiebeln für meine Frau",
        ),
    ],
)
def test_removal_is_not_cut_by_the_phrase(said, note):
    """Own review: the phrase swallowed the removal or cut it in half."""
    assert classify_wish(said, []).text == note


def test_vertrage_alles_ist_keine_allergie():
    """Review PR #139: "ich vertrage alles" ist keine Allergie."""
    assert classify_wish("ich vertrage alles", []).kind != "allergy"
    assert wish_candidates("Pho Bo, ich vertrage alles") == []
    assert classify_wish("ich vertrage keine Erdnüsse", []).ingredient == "Erdnüsse"


def test_weglassen_nach_unbekannter_zugabe_bleibt_notiert():
    """Review PR #139: "mit Pommes ohne Zwiebeln" - die Zugabe wird abgelehnt, das
    Weglassen bleibt, wie in der umgekehrten Reihenfolge."""
    wish = classify_wish("mit Pommes ohne Zwiebeln", BEILAGE)
    assert (wish.kind, wish.note) == ("unknown", "ohne Zwiebeln")


@pytest.mark.parametrize(
    ("said", "text", "note"),
    [
        # Nothing is named between the removal word and "statt".
        ("ohne statt Reis", "ohne statt Reis", None),
        ("keine anstatt Reis", "keine anstatt Reis", None),
        # A filler in front of it is not something left out either.
        ("bitte ohne statt Reis", "bitte ohne statt Reis", None),
        # A removal that is named stays noted; the rest is not understood.
        ("ohne Zwiebeln, kein statt Reis", "kein statt Reis", "ohne Zwiebeln"),
    ],
)
def test_removal_word_directly_before_statt_is_not_understood(said, text, note):
    """ "ohne statt Reis" names neither what to leave out nor what to take
    instead. It ran into endless recursion (eval abholung_0075). It is a wish
    the agent does not offer, never a free note for the kitchen."""
    wish = classify_wish(said, BEILAGE)
    assert (wish.kind, wish.text, wish.note) == ("unknown", text, note)


@pytest.mark.parametrize(
    "gesagt", ["allergisch gegen Sesam, ja genau", "allergisch gegen Sesam, richtig"]
)
def test_bestaetigung_nach_der_zutat(gesagt):
    """Review PR #139: ein "ja genau" nach der Zutat macht sie nicht ungueltig."""
    assert classify_wish(gesagt, []).ingredient == "Sesam"


def test_allergie_vor_dem_gericht_gehoert_zum_gericht(session, tenant_id):
    """Review PR #139: die Allergie vorn im Satz ist keine eigene Position."""
    gesagt = "Ich habe eine Nussallergie, die 23"
    assert position_parts(session, tenant_id, gesagt, now=NOW) == [
        "die 23, Ich habe eine Nussallergie"
    ]


def test_schema_nennt_die_felder():
    """Review PR #139: ohne leere Felder im Ergebnis, aber mit Schema."""
    from api.schemas.menu import OptionOut, Wish

    for model in (OptionOut, Wish):
        schema = model.model_json_schema(mode="serialization")
        assert "properties" in schema, model
    assert (
        "reason"
        not in OptionOut(name="Reis", price_delta_cents=0, default=True).model_dump()
    )


def test_alte_optionsdatei_behaelt_den_grund(session, tenant_id):
    """Review PR #139: eine Datei ohne Spalte price_reason loescht den gepflegten
    Grund nicht."""
    ohne_spalte = {
        **KARTE_WUENSCHE,
        OPTIONS_FILE: (
            "number;group_name;option_name;price_delta_eur;is_default;required\n"
            "47;Fleisch;Ente;0,00;ja;ja\n"
            "47;Fleisch;Huhn;-1,00;nein;ja\n"
            "47;Beilage;Reis;0,00;ja;nein\n"
            "47;Beilage;Nudeln;3,00;nein;nein\n"
        ),
    }
    plan = parse(ohne_spalte)
    assert plan.ok, plan.errors
    apply(session, tenant_id, plan, now=NOW)
    reason = session.scalar(
        select(ItemOption.price_reason).where(ItemOption.option_name == "Nudeln")
    )
    assert reason == GRUND


@pytest.mark.parametrize(
    "said",
    [
        "ich habe keine Allergie und allergisch auf Erdnüsse",
        "ich bin nicht allergisch und intolerant gegen Laktose",
        "keine Allergien und allergisch gegen Sesam",
        "ohne Zwiebeln, ich habe keine Allergie und allergisch gegen Erdnüsse",
        # "nicht" governs no noun.
        "ich bin nicht allergisch und Unverträglichkeit gegen Laktose",
    ],
)
def test_denial_joined_by_und_to_a_named_allergen_is_asked_about(said):
    """Review of PR #330: the denial was carried over "und" to an allergy
    adjective and took its "gegen X" as denied, so the allergy was dropped
    without a question (None, or the removal alone). "keine" does not govern
    an adjective: the agent asks."""
    wish = classify_wish(said, BEILAGE)
    assert (wish.kind, wish.ingredient) == ("allergy", None)


@pytest.mark.parametrize(
    "said",
    [
        "keine Allergien und Unverträglichkeiten",
        "ich habe keine Allergien oder Unverträglichkeiten gegen Laktose",
        "weder Allergie noch Unverträglichkeit gegen Laktose",
        # Codex PR #338: the last allergy word is negated on its own.
        "keine Allergien und Unverträglichkeiten und keine Intoleranz gegen Laktose",
        # Codex PR #338: "keine" governs both nouns.
        "ich habe keine Allergien und Unverträglichkeiten gegen Laktose",
        "keine Allergie und Unverträglichkeit gegen Laktose",
    ],
)
def test_a_denial_of_both_stays_a_denial(said):
    assert classify_wish(said, BEILAGE) is None


@pytest.mark.parametrize(
    ("said", "ingredient"),
    [
        ("ich bin allergisch gegen Erdnüsse und gegen Sesam", "Erdnüsse und Sesam"),
        ("ich bin gegen Erdnüsse und gegen Sesam allergisch", "Erdnüsse und Sesam"),
        (
            "ich habe eine Allergie gegen Erdnüsse und auch gegen Sesam",
            "Erdnüsse und Sesam",
        ),
        (
            "ich bin allergisch gegen Erdnüsse, gegen Sesam und gegen Milch",
            "Erdnüsse, Sesam und Milch",
        ),
        ("ich bin allergisch auf Erdnüsse und auf Sesam", "Erdnüsse und Sesam"),
        # The ear wrote no comma.
        ("ich bin gegen Erdnüsse gegen Sesam allergisch", "Erdnüsse und Sesam"),
        # Codex PR #338: the clause after the list stays out of it.
        (
            "ich bin allergisch gegen Erdnüsse und gegen Sesam und möchte bestellen",
            "Erdnüsse und Sesam",
        ),
        (
            "ich bin allergisch gegen Erdnüsse und gegen Sesam. Danke",
            "Erdnüsse und Sesam",
        ),
        # Two phrases across a full stop, each read on its own.
        (
            "ich bin allergisch gegen Erdnüsse. Gegen Sesam bin ich allergisch",
            "Erdnüsse und Sesam",
        ),
        (
            "ich bin allergisch gegen Erdnüsse. Und gegen Sesam bin ich allergisch",
            "Erdnüsse und Sesam",
        ),
        (
            "ohne Zwiebeln, ich bin allergisch gegen Erdnüsse und gegen Sesam",
            "Erdnüsse und Sesam",
        ),
    ],
)
def test_a_second_gegen_keeps_its_ingredient(said, ingredient):
    """Review of PR #330, older than it: the ingredient behind a second
    "gegen" without its own allergy word was lost, only one reached the
    kitchen."""
    assert classify_wish(said, BEILAGE).ingredient == ingredient


@pytest.mark.parametrize(
    "said",
    [
        "ich bin allergisch gegen Erdnüsse und gegen 18 Uhr hole ich ab",
        "ich bin allergisch gegen Erdnüsse, gegen halb sieben hole ich ab",
        "ich bin allergisch gegen Erdnüsse und gegen Abend komme ich",
        "ich bin allergisch gegen Erdnüsse und auf jeden Fall ohne Zwiebeln",
        "Pho Bo und gegen Erdnüsse bin ich allergisch",
        # Codex PR #338: a clause of its own, not a second allergen.
        "ich bin allergisch gegen Erdnüsse und auf dem Weg zu euch",
        "ich bin allergisch gegen Erdnüsse und auf Antibiotika angewiesen",
        "ich bin allergisch gegen Erdnüsse, auf Wiedersehen",
        # Codex PR #338: a list repeats its own preposition.
        "ich bin allergisch gegen Erdnüsse und auf Diät",
        "ich bin allergisch gegen Erdnüsse und auf Reisen",
        # Codex PR #338: the order goes on after the list.
        "ich bin allergisch gegen Erdnüsse und möchte bestellen",
        "ich bin allergisch gegen Erdnüsse, und möchte zwei Pho Bo",
    ],
)
def test_a_gegen_that_lists_nothing_stays_out(said):
    """A time, "auf jeden Fall" or a clause of its own is no second
    ingredient, and a "gegen" without one before it begins the list."""
    assert classify_wish(said, BEILAGE).ingredient == "Erdnüsse"


@pytest.mark.parametrize(
    ("said", "kept"),
    [
        ("ich bin allergisch gegen Erdnüsse und möchte keine Zwiebeln", "Zwiebeln"),
        ("ich bin allergisch gegen Erdnüsse und möchte auch keinen Sesam", "Sesam"),
        ("ich bin allergisch gegen Erdnüsse und kann keine Milch trinken", "Milch"),
    ],
)
def test_a_clause_that_names_something_stays_in_the_note(said, kept):
    """The list ends at an order verb only when nothing to leave out follows
    it: the kitchen still reads "und möchte keine Zwiebeln" (Codex PR #338)."""
    assert kept in classify_wish(said, BEILAGE).text


@pytest.mark.parametrize(
    "said",
    [
        # Issue #207, P1: a second allergen behind the "allergisch" of a
        # "gegen ... allergisch" phrase.
        "Ich bin gegen Erdnüsse allergisch und gegen Sesam",
        "Ich bin gegen Erdnüsse allergisch, und gegen Sesam",
        "Ich bin gegen Erdnüsse allergisch sowie gegen Sesam",
        "Ich bin gegen Erdnüsse allergisch und auch gegen Sesam",
        "Ich bin gegen Erdnüsse allergisch und Sesam",
        # Issue #207, P1: a list behind a compound allergy noun.
        "Ich habe eine Erdnussallergie und gegen Sesam",
        "Ich habe eine Erdnussallergie und Sesam",
        "Ich habe eine Erdnussallergie, außerdem gegen Sesam",
        "Ich habe eine Erdnuss-Allergie und Sesam",
        "Ich habe eine Erdnussallergie, Sesam und Sellerie",
        "Ich habe eine Erdnussallergie, Sesam vertrage ich auch nicht",
        "Ich habe eine Laktoseintoleranz und gegen Sesam",
        # Issue #207, P1: "aber auch" after the list.
        "Ich bin allergisch gegen Erdnüsse, aber auch gegen Sesam",
        "Ich bin allergisch gegen Erdnüsse, aber auch Sesam",
        "Pho Bo, ich bin allergisch gegen Erdnüsse, aber auch Peter möchte bestellen",
        # A generic allergy word names nothing itself.
        "Ich habe eine Allergie und gegen Sesam",
        # A "gegen" no list holds, also after a full stop.
        "Ich habe eine Erdnussallergie. Und gegen Sesam",
        "Ich habe eine Erdnussallergie und meine Frau gegen Sesam",
        # Issue #207, P1: a filler behind the joiner hid the next allergen.
        "Ich habe eine Erdnussallergie und dann noch Sesam",
        "Ich habe eine Erdnussallergie und dann auch Sesam",
        "Ich habe eine Erdnussallergie und dann Sesam",
        "Ich bin allergisch gegen Erdnüsse und dann noch Sesam",
        "Ich bin allergisch gegen Erdnüsse, dann auch Sesam",
        "Ich habe eine Erdnussallergie und dazu noch Sesam",
        "Ich habe eine Erdnussallergie und vielleicht auch Sesam",
        "Ich habe eine Erdnussallergie und dann noch gegen Sesam",
    ],
)
def test_more_than_the_parser_reads_is_asked_about(said):
    """Issue #207: only the first allergen reached the kitchen note and the
    agent did not ask. What the parser does not read is never noted in part:
    the ingredient stays open, and the agent asks "Wogegen?" (PR #361)."""
    wish = classify_wish(said, BEILAGE)
    assert (wish.kind, wish.ingredient) == ("allergy", None)


@pytest.mark.parametrize(
    ("said", "ingredient"),
    [
        # A time, "auf jeden Fall", an order verb, a new subject or dish, a
        # thanks, or a new sentence after the allergy word: the call goes on.
        ("Ich habe eine Erdnussallergie und gegen 18 Uhr hole ich ab", "Erdnuss"),
        ("Ich bin gegen Erdnüsse allergisch und gegen Abend komme ich", "Erdnüsse"),
        ("Ich habe eine Erdnussallergie und auf jeden Fall ohne Zwiebeln", "Erdnuss"),
        ("Ich habe eine Erdnussallergie und möchte bestellen", "Erdnuss"),
        ("Ich habe eine Erdnussallergie und die Sommerrollen", "Erdnuss"),
        ("Ich habe eine Erdnussallergie, danke", "Erdnuss"),
        ("Ich habe eine Erdnussallergie. Und Cola", "Erdnuss"),
        ("Ich bin gegen Erdnüsse allergisch! Und Cola", "Erdnüsse"),
        ("Ich habe eine Erdnussallergie; und gegen Abend komme ich", "Erdnuss"),
        (
            "Ich habe eine Glutenunverträglichkeit und meine Frau eine Erdnussallergie",
            "Gluten und Erdnuss",
        ),
        # Read as on main: the parser takes these lists itself.
        ("Ich bin allergisch gegen Erdnüsse und gegen Sesam", "Erdnüsse und Sesam"),
        ("Ich bin allergisch gegen Erdnüsse und auch Sesam", "Erdnüsse und Sesam"),
        ("Ich habe eine Erdnussallergie und eine Sesamallergie", "Erdnuss und Sesam"),
        # An intensifier names no allergen.
        ("ich bin hochallergisch gegen Erdnüsse", "Erdnüsse"),
        ("ich bin stark allergisch gegen Erdnüsse", "Erdnüsse"),
        # A generic word followed by a dish: "Wogegen?", never the dish.
        ("Ich habe eine Allergie und Frühlingsrollen", None),
        # A filler before what goes on with the call (#207).
        ("Ich bin allergisch gegen Erdnüsse und dann noch eine Cola", "Erdnüsse"),
        ("Ich habe eine Erdnussallergie und dann noch die 13", "Erdnuss"),
        ("Ich habe eine Erdnussallergie und dann noch zwei Cola", "Erdnuss"),
        ("Ich habe eine Erdnussallergie und dann möchte ich bestellen", "Erdnuss"),
        ("Ich habe eine Erdnussallergie und dann hole ich es ab", "Erdnuss"),
    ],
)
def test_what_goes_on_with_the_call_reads_as_on_main(said, ingredient):
    wish = classify_wish(said, BEILAGE)
    assert (wish.kind, wish.ingredient) == ("allergy", ingredient)


@pytest.mark.parametrize(
    ("said", "text", "note"),
    [
        # Issue #331 (Codex PR #330): "aber bitte keine".
        (
            "ich bin allergisch gegen Erdnüsse, aber bitte keine Zwiebeln",
            "WICHTIG: Keine Erdnüsse. Grund: Allergie. Keine Zwiebeln",
            None,
        ),
        (
            "ich habe eine Erdnussallergie, aber auch keine Zwiebeln",
            "WICHTIG: Keine Erdnuss. Grund: Allergie. Keine Zwiebeln",
            None,
        ),
        (
            "ich habe eine Allergie, aber bitte keine Zwiebeln",
            "ich habe eine Allergie",
            "keine Zwiebeln",
        ),
        # Before, the list ended at "bitte" and the request was lost.
        (
            "ich vertrage keine Erdnüsse und bitte keine Mandeln",
            "WICHTIG: Keine Erdnüsse. Grund: Allergie. Keine Mandeln",
            None,
        ),
    ],
)
def test_a_removal_after_aber_or_bitte_is_kept(said, text, note):
    """Issue #331: only a "keine" right after "aber" opened a removal, so
    "aber bitte keine Zwiebeln" lost the onions."""
    wish = classify_wish(said, BEILAGE)
    assert (wish.kind, wish.text, wish.note) == ("allergy", text, note)


def test_keine_without_aber_or_bitte_still_belongs_to_the_allergy():
    """A "keine" that no "aber" or "bitte" leads to may still name an
    allergen: it is not taken as a removal (Codex PR #330)."""
    wish = classify_wish("ich vertrage keine Erdnüsse, keine Mandeln", BEILAGE)
    assert (wish.kind, wish.ingredient) == ("allergy", "Erdnüsse, keine Mandeln")


@pytest.mark.parametrize(
    ("removal", "kept"),
    [
        # Issue #331 (Codex PR #330): too long for an order line.
        ("ohne " + "Zwiebeln, " * 20 + "Koriander", None),
        ("ohne " + "x" * (NOTE_MAX - 5), "ohne " + "x" * (NOTE_MAX - 5)),
        ("ohne " + "x" * (NOTE_MAX - 4), None),
    ],
)
def test_a_waiting_removal_fits_an_order_line(removal, kept):
    """A removal that waits in `note` for the answer to "Wogegen?" is put on
    the order line as it is when the guest withdraws the allergy; one too
    long for the line is lost, as in `kitchen_note`, and the allergy stays."""
    wish = classify_wish(f"{removal}, ich habe eine Allergie", BEILAGE)
    assert (wish.kind, wish.ingredient, wish.note) == ("allergy", None, kept)


@pytest.mark.parametrize(
    "said",
    [
        # Codex PR #361: the lactose of the adjective is not read, so the
        # list would hold only the sesame.
        "Pho Bo, ich bin laktoseintolerant und gegen Sesam",
        "ich bin laktoseintolerant und Sesam",
        # The same loss, older than PR #361.
        "ich bin laktoseintolerant und allergisch gegen Sesam",
        "ich bin Erdnussallergiker und allergisch gegen Sesam",
        "ich bin laktoseintolerant gegen Sesam",
    ],
)
def test_an_allergen_the_parser_cannot_read_is_asked_about(said):
    """An allergy word that names an allergen the parser does not read
    ("laktoseintolerant", "Erdnussallergiker") never gives the rest of the
    list alone: the agent asks "Wogegen?"."""
    wish = classify_wish(said, BEILAGE)
    assert (wish.kind, wish.ingredient) == ("allergy", None)


@pytest.mark.parametrize(
    ("said", "ingredient"),
    [
        # The guest's own verb after the item: read as on main.
        (
            "ich bin allergisch gegen Erdnüsse und Sesam habe ich auch",
            "Erdnüsse und Sesam",
        ),
        (
            "ich bin allergisch gegen Erdnüsse und Sesam bin ich auch",
            "Erdnüsse und Sesam",
        ),
        ("ich bin allergisch gegen Erdnüsse und möchte bestellen", "Erdnüsse"),
        # An adjective the parser does not read, after the list.
        ("ich bin allergisch gegen Erdnüsse und laktoseintolerant", None),
    ],
)
def test_the_guests_own_verb_keeps_the_list(said, ingredient):
    wish = classify_wish(said, BEILAGE)
    assert (wish.kind, wish.ingredient) == ("allergy", ingredient)


@pytest.mark.parametrize(
    "said",
    [
        # Codex PR #361, rounds 2 and 3: "keine" denies the allergy noun after
        # it and leaves out no food: no removal "Keine", no "weiteren" in the
        # list. The denial is a mixed one, so the agent asks.
        "ich bin allergisch gegen Erdnüsse, aber auch keine weiteren Allergien",
        "ich bin allergisch gegen Erdnüsse, aber keine weiteren Allergien",
        "ich habe eine Erdnussallergie, aber auch keine anderen Unverträglichkeiten",
        # A denial that names an allergen: never the denied sesame noted.
        "ich bin allergisch gegen Erdnüsse, aber keine Sesamallergie",
        "ich habe eine Erdnussallergie, aber keine Allergie gegen Sesam",
    ],
)
def test_a_keine_that_denies_an_allergy_is_no_removal(said):
    wish = classify_wish(said, BEILAGE)
    assert (wish.kind, wish.ingredient, wish.note) == ("allergy", None, None)
    assert "Keine" not in wish.text.split(". ")[-1:]


@pytest.mark.parametrize(
    "said",
    [
        # Codex PR #361, round 4: a word after a comma is no allergen.
        "Ich habe eine Erdnussallergie, leider",
        "Ich habe eine Erdnussallergie, chronisch",
    ],
)
def test_a_word_after_a_comma_is_asked_about_never_noted(said):
    """ "Keine Erdnuss und Leider": the word after the comma went on the
    note. The parser does not read it, so the agent asks "Wogegen?"."""
    wish = classify_wish(said, BEILAGE)
    assert (wish.kind, wish.ingredient) == ("allergy", None)


def test_a_denial_after_aber_keeps_the_question():
    """Codex PR #361, round 4: "aber ich habe keine Allergie" contradicts the
    allergy before it; it is asked about, never noted."""
    wish = classify_wish(
        "ich bin allergisch gegen Erdnüsse, aber ich habe keine Allergie", BEILAGE
    )
    assert (wish.kind, wish.ingredient) == ("allergy", None)
