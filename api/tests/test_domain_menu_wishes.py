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

from api.core.errors import Ambiguous
from api.domain.menu import wishes
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
    mentions_allergy,
    open_wish,
    removal_with,
    speaks_of_health,
    wish_candidates,
)
from api.models import ItemOption, MenuItem
from api.schemas.menu import OptionGroup, OptionOut, Wish
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


def wish_of(text: str, groups: list[OptionGroup]) -> Wish:
    """classify_wish for a text that must be classified (not None)."""
    wish = classify_wish(text, groups)
    assert wish is not None
    return wish


def first_split(text):
    """Die erste Trennung, wie die Suche sie zuerst versucht."""
    candidates = wish_candidates(text)
    return (candidates[0][0], candidates[0][1]) if candidates else (text, None)


# --- ohne Datenbank: einordnen -------------------------------------------------------


@pytest.mark.parametrize(
    "wunsch", ["ohne Karotten", "keine Zwiebeln", "kein Koriander"]
)
def test_weglassen_ist_ein_hinweis(wunsch):
    wish = wish_of(wunsch, BEILAGE)
    assert wish.kind == "note"
    assert wish.price_delta_cents is None


def test_option_der_karte_mit_aufpreis_und_grund():
    wish = wish_of("mit Nudeln statt Reis", BEILAGE)
    assert (wish.kind, wish.group, wish.option) == ("option", "Beilage", "Nudeln")
    assert wish.price_delta_cents == 300
    assert wish.reason == GRUND


def test_option_ohne_aufpreis_gilt_auch():
    wish = wish_of("mit Huhn", BEILAGE)
    assert (wish.kind, wish.option, wish.price_delta_cents) == ("option", "Huhn", -100)


@pytest.mark.parametrize("wunsch", ["mit Pommes", "extra scharf", "Nudeln statt Reis"])
def test_was_die_karte_nicht_kennt_wird_nicht_angeboten(wunsch):
    """D8: ohne Option auf der Karte kein Angebot - auch "Nudeln statt Reis"
    nicht, wenn das Gericht keine Beilagen-Gruppe hat."""
    groups = [] if wunsch == "Nudeln statt Reis" else BEILAGE
    assert wish_of(wunsch, groups).kind == "unknown"


@pytest.mark.parametrize(
    "wunsch", ["ich habe eine Erdnussallergie", "ich bin allergisch gegen Sesam"]
)
def test_allergie_ist_kein_wunsch(wunsch):
    assert wish_of(wunsch, BEILAGE).kind == "allergy"


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


def test_two_options_through_the_search(session, tenant_id):
    """Codex PR #139 (docs/24): both options reach the model, read back with
    the card's sum."""
    result = suche(session, tenant_id, "die knusprige Ente mit Huhn und Nudeln")
    assert [h.number for h in result.results] == ["47"]
    assert [(o.group, o.option) for o in result.wish.options] == [
        ("Fleisch", "Huhn"),
        ("Beilage", "Nudeln"),
    ]
    assert result.wish.option == "Huhn, ein Euro günstiger, und Nudeln, 3 Euro Aufpreis"


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
        "WICHTIG !!! Allergie: Erdnuss. Ohne Zwiebeln",
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
    wish = wish_of("mit Erdnusssauce", SAUCE)
    assert (wish.kind, wish.group, wish.option) == ("option", "Sauce", "Erdnuss")


def test_kein_vorsilben_treffer():
    """ "Reisnudeln" ist nicht die Option Reis: nur Option plus Gruppenname zaehlt
    zusammengesetzt, kein beliebiger Wortanfang."""
    assert wish_of("mit Reisnudeln", SAUCE).kind == "unknown"


# --- Allergie im festen Wortlaut (E14, Maxi 24.09.2026) ----------------------------


@pytest.mark.parametrize(
    ("gesagt", "hinweis"),
    [
        ("ich habe eine Erdnussallergie", "WICHTIG !!! Allergie: Erdnuss"),
        ("ich bin allergisch gegen Sesam", "WICHTIG !!! Allergie: Sesam"),
        ("ich vertrage keine Erdnüsse", "WICHTIG !!! Allergie: Erdnüsse"),
        ("Allergie gegen Sellerie", "WICHTIG !!! Allergie: Sellerie"),
    ],
)
def test_allergie_als_kuechenhinweis_im_festen_wortlaut(gesagt, hinweis):
    """E14: der Hinweis an die Kueche hat einen festen Wortlaut; er wird beim
    Vorlesen wiederholt, nie mit der Zusage, das Gericht sei frei davon."""
    wish = wish_of(gesagt, BEILAGE)
    assert (wish.kind, wish.text) == ("allergy", hinweis)


def test_vertraegt_keine_ist_eine_allergie_kein_weglassen():
    """ "ich vertrage keine Erdnüsse" ist eine Allergie (docs/05 §Allergie), kein
    "keine Karotten" - der Satzteil beginnt am Komma."""
    assert first_split("Pho Bo, ich vertrage keine Erdnüsse") == (
        "Pho Bo",
        "ich vertrage keine Erdnüsse",
    )


def test_an_allergy_without_its_ingredient_is_the_models_to_ask(session, tenant_id):
    """Maxi, 15.11.2026: the core asks no "Wogegen?"; the dish is found."""
    result = suche(session, tenant_id, "Pho Bo, ich habe eine Allergie")
    assert [hit.name for hit in result.results] == ["Pho Bo"]
    assert (result.wish.kind, result.wish.ingredient) == ("allergy", None)
    assert "Wogegen" not in (result.say or "")


def test_allergie_in_der_suche_im_festen_wortlaut(session, tenant_id):
    result = suche(session, tenant_id, "Pho Bo, ich habe eine Erdnussallergie")
    assert result.wish.text == "WICHTIG !!! Allergie: Erdnuss"


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
    assert wish_of("mit Nudeln und Pommes", BEILAGE).kind == "unknown"
    # The one word after "statt" is what is swapped out, no further wish.
    assert wish_of("mit Nudeln statt Reis bitte", BEILAGE).kind == "option"


# --- Eigenes Review PR #139 --------------------------------------------------------


def test_weglassen_und_hinzufuegen_zusammen():
    """(1) "ohne Zwiebeln, dafür mit Nudeln": die Zugabe ist die Option mit
    Aufpreis, das Weglassen kommt als Hinweis mit - nie beides als freie Notiz."""
    wish = wish_of("ohne Zwiebeln, dafür mit Nudeln", BEILAGE)
    assert (wish.kind, wish.option, wish.price_delta_cents) == ("option", "Nudeln", 300)
    assert wish.note == "ohne Zwiebeln"


def test_weglassen_und_unbekannte_zugabe():
    wish = wish_of("ohne Zwiebeln, dafür mit Pommes", BEILAGE)
    assert wish.kind == "unknown"
    assert wish.text == "mit Pommes"
    assert wish.note == "ohne Zwiebeln"


@pytest.mark.parametrize(
    "said",
    [
        "ohne Hund, dafür mit Nudeln",
        "ohne Hund, mit Nudeln",
        "ohne Hund und mit Nudeln",
    ],
)
def test_a_word_that_only_ends_in_a_joiner_stays_whole(said):
    """ "und" at the end of "Hund" is no joining word; the note kept "ohne H"
    (docs/24 row, now in docs/HISTORY.md)."""
    wish = wish_of(said, BEILAGE)
    assert (wish.kind, wish.option, wish.note) == ("option", "Nudeln", "ohne Hund")


def test_a_word_that_only_ends_in_a_joiner_stays_whole_before_a_removal():
    """The same cut in front of "ohne": what is asked back names the whole word."""
    wish = wish_of("mit Nudeln, gesund, ohne Zwiebeln", BEILAGE)
    assert (wish.kind, wish.text, wish.note) == (
        "unknown",
        "mit Nudeln, gesund",
        "ohne Zwiebeln",
    )


@pytest.mark.parametrize(
    ("said", "note"),
    [
        ("ohne Zwiebeln und, dafür mit Nudeln", "ohne Zwiebeln"),
        ("ohne Zwiebeln aber mit Nudeln", "ohne Zwiebeln"),
        ("ohne Zwiebeln, und dafür mit Nudeln", "ohne Zwiebeln"),
        # Own review: a hesitation, "bitte", "dann", a second joining word or
        # a capital after a full stop stayed in the kitchen note.
        ("ohne Zwiebeln und... dafür mit Nudeln", "ohne Zwiebeln"),
        ("ohne Zwiebeln bitte, dafür mit Nudeln", "ohne Zwiebeln"),
        ("ohne Zwiebeln, aber bitte mit Nudeln", "ohne Zwiebeln"),
        ("ohne Zwiebeln und dann mit Nudeln", "ohne Zwiebeln"),
        ("Ohne Zwiebeln. Aber mit Nudeln.", "Ohne Zwiebeln"),
        ("ohne Zwiebeln und. Aber mit Nudeln", "ohne Zwiebeln"),
        ("ohne Zwiebeln, bitte! Mit Nudeln", "ohne Zwiebeln"),
        ("ohne Zwiebeln und... aber mit Nudeln", "ohne Zwiebeln"),
        ("ohne Zwiebeln und... dann mit Nudeln", "ohne Zwiebeln"),
        ("ohne Zwiebeln, bitte. Dann mit Nudeln", "ohne Zwiebeln"),
        ("ohne Zwiebeln! Aber mit Nudeln", "ohne Zwiebeln"),
    ],
)
def test_a_joining_word_of_its_own_is_still_cut(said, note):
    wish = wish_of(said, BEILAGE)
    assert (wish.kind, wish.option, wish.note) == ("option", "Nudeln", note)


def test_joining_words_in_front_of_a_removal_are_not_asked_back():
    """Own review: "mit Pommes und dann" was the text the agent asked about."""
    wish = wish_of("mit Pommes und dann ohne Zwiebeln", BEILAGE)
    assert (wish.kind, wish.text, wish.note) == (
        "unknown",
        "mit Pommes",
        "ohne Zwiebeln",
    )


@pytest.mark.parametrize(
    ("part", "left"),
    [
        ("ohne Zwiebeln und.\t", "ohne Zwiebeln"),
        ("ohne Zwiebeln;\n", "ohne Zwiebeln"),
        ("ohne Hund, ", "ohne Hund"),
        ("bitte ohne Zwiebeln, aber", "bitte ohne Zwiebeln"),
        ("und", "und"),
    ],
)
def test_the_part_in_front_of_the_next_one_loses_only_its_tail(part, left):
    """A joining word at the very start stays; whitespace and punctuation at
    the end go in any order (the copying version left "ohne Zwiebeln;\\n")."""
    assert wishes._without_glue(part) == left


def test_bitte_with_a_comma_behind_it_is_cut():
    """Own review: "bitte," stayed in the note."""
    wish = wish_of("ohne Zwiebeln, bitte,", BEILAGE)
    assert (wish.kind, wish.text) == ("note", "ohne Zwiebeln")


@pytest.mark.parametrize(
    ("text", "left"),
    [
        ("die Abbitte", "die Abbitte"),
        ("ohne Koriander bitte!", "ohne Koriander"),
        ("ohne Koriander, Bitte", "ohne Koriander"),
    ],
)
def test_bitte_is_cut_only_as_a_word_of_its_own(text, left):
    """The other pattern `_cut_tail` runs had the same missing word boundary."""
    assert wishes._cut_tail(wishes._PLEASE_AT_END, text) == left


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
    wish = wish_of("ohne Zwiebeln, zweimal", BEILAGE)
    assert (wish.kind, wish.text) == ("note", "ohne Zwiebeln")


@pytest.mark.parametrize(
    ("gesagt", "hinweis"),
    [
        # (5) Die Zutat endet am Satzteil.
        (
            "ich bin allergisch gegen Sesam und dann noch eine Cola",
            "WICHTIG !!! Allergie: Sesam",
        ),
        # (6) Andere Wortstellung.
        ("ich bin gegen Nüsse allergisch", "WICHTIG !!! Allergie: Nüsse"),
    ],
)
def test_zutat_der_allergie(gesagt, hinweis):
    assert wish_of(gesagt, BEILAGE).text == hinweis


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
    wish = wish_of("mit Reis", groups)
    assert wish.kind == "open"
    assert wish.groups == ["Beilage", "Extra"]


RICE_IN_TWO_GROUPS = [
    *BEILAGE,
    OptionGroup(
        group="Extra",
        required=False,
        options=[OptionOut(name="Reis", price_delta_cents=200, default=False)],
    ),
]


@pytest.mark.parametrize(
    ("said", "group", "delta"),
    [
        ("mit Reis als Beilage", "Beilage", 0),
        ("mit Reis als Extra", "Extra", 200),
        ("Reis als Extra bitte", "Extra", 200),
    ],
)
def test_a_named_group_settles_an_option_in_two_groups(said, group, delta):
    """Codex PR #139 (docs/24): the guest named the group, so no question."""
    wish = wish_of(said, RICE_IN_TWO_GROUPS)
    assert (wish.kind, wish.group, wish.option) == ("option", group, "Reis")
    assert wish.price_delta_cents == delta


def test_both_groups_named_is_still_asked():
    wish = wish_of("mit Reis als Beilage und als Extra", RICE_IN_TWO_GROUPS)
    assert (wish.kind, wish.groups) == ("open", ["Beilage", "Extra"])


def test_the_group_of_an_option_in_one_group_may_be_named():
    """ "als Beilage" next to an option of that group is no unknown word."""
    wish = wish_of("mit Nudeln als Beilage", BEILAGE)
    assert (wish.kind, wish.group, wish.option) == ("option", "Beilage", "Nudeln")


def test_another_group_named_is_not_understood():
    """The card has no Nudeln among the Fleisch: never a guess."""
    assert wish_of("mit Nudeln als Fleisch", BEILAGE).kind == "unknown"


def test_two_options_from_different_groups():
    """Codex PR #139 (docs/24): "mit Huhn und Nudeln" was declined as unknown."""
    wish = wish_of("mit Huhn und Nudeln", BEILAGE)
    assert wish.kind == "option"
    assert wish.options is not None
    assert [(o.group, o.option, o.price_delta_cents) for o in wish.options] == [
        ("Fleisch", "Huhn", -100),
        ("Beilage", "Nudeln", 300),
    ]
    assert wish.options[1].reason == GRUND
    # Read back as one, each with its own delta: a surcharge is never hidden
    # in a sum (deep review of #1076). No delta of the whole.
    assert (wish.option, wish.price_delta_cents, wish.group) == (
        "Huhn, ein Euro günstiger, und Nudeln, 3 Euro Aufpreis",
        None,
        None,
    )


def test_two_options_without_a_delta_are_read_back_by_name():
    wish = wish_of("mit Ente und Reis", BEILAGE)
    assert (wish.option, wish.price_delta_cents) == ("Ente und Reis", None)


def card(*groups: tuple[str, list[tuple[str, int]]]) -> list[OptionGroup]:
    """A card of option groups from (group, [(option, delta), ...])."""
    return [
        OptionGroup(
            group=group,
            required=False,
            options=[
                OptionOut(name=name, price_delta_cents=delta, default=False)
                for name, delta in options
            ],
        )
        for group, options in groups
    ]


FLEISCH = ("Fleisch", [("Ente", 0), ("Huhn", -100)])
EXTRA_MEAT = card(FLEISCH, ("Extra Fleisch", [("Garnelen", 250)]))
SAUCE_AND_EXTRAS = card(
    FLEISCH, ("Sauce", [("süßsauer", 0), ("Erdnuss", 50)]), ("Extras", [("Sauce", 50)])
)
WARM_SIDE = card(
    FLEISCH, ("Beilage", [("Reis", 0)]), ("Warme Beilage", [("Reis", 200)])
)


@pytest.mark.parametrize(
    ("said", "groups"),
    [
        # Codex PR #1076: each option named with the other's group.
        ("mit Huhn als Beilage und Nudeln als Fleisch", BEILAGE),
        ("mit Nudeln als Fleisch und Huhn", BEILAGE),
        # The group settles Reis only when it stands with Reis.
        ("mit Huhn als Beilage und Reis", RICE_IN_TWO_GROUPS),
        # Deep review of #1076: "extra" adds; it names the group Extra only
        # after "als", and there next to Huhn.
        ("mit extra Huhn und Reis", RICE_IN_TWO_GROUPS),
        ("mit Huhn als Extra und Reis", RICE_IN_TWO_GROUPS),
        ("mit Reis und Nudeln als Extra", RICE_IN_TWO_GROUPS),
        # A group word that names no group here is not understood.
        ("mit Huhn und extra Fleisch", BEILAGE),
        ("mit Huhn und Fleisch", BEILAGE),
        # One word of a longer group name is not that group.
        ("mit Garnelen als Fleisch", EXTRA_MEAT),
        # The words nothing explains decline an option in two groups too.
        ("mit Reis und Pommes", RICE_IN_TWO_GROUPS),
    ],
)
def test_a_group_named_with_another_option_is_not_understood(said, groups):
    assert wish_of(said, groups).kind == "unknown"


@pytest.mark.parametrize(
    "said", ["mit Erdnuss als Sauce", "mit Erdnuss-Sauce", "mit Erdnuss Sauce"]
)
def test_a_word_that_names_the_group_is_no_second_option(said):
    """Deep review of #1076: "Sauce" names the group of Erdnuss here, not the
    extra called Sauce."""
    wish = wish_of(said, SAUCE_AND_EXTRAS)
    assert (wish.kind, wish.group, wish.option) == ("option", "Sauce", "Erdnuss")
    assert wish.options is None


def test_the_words_of_an_option_stand_together():
    """Sweep of the #1076 deep review: "gebratener" belongs to the duck here,
    so the fried rice is not asked for."""
    groups = card(FLEISCH, ("Beilage", [("Jasminreis", 0), ("gebratener Reis", 250)]))
    assert wish_of("mit gebratener Ente und Reis", groups).kind == "unknown"
    wish = wish_of("mit Ente und gebratener Reis", groups)
    assert [(o.group, o.option) for o in wish.options or []] == [
        ("Fleisch", "Ente"),
        ("Beilage", "gebratener Reis"),
    ]


def test_the_longest_group_name_said_counts():
    wish = wish_of("mit Huhn und Reis als warme Beilage", WARM_SIDE)
    assert [(o.group, o.option) for o in wish.options or []] == [
        ("Fleisch", "Huhn"),
        ("Warme Beilage", "Reis"),
    ]


@pytest.mark.parametrize(
    ("said", "groups"),
    [
        # "extra" before the option adds; which group is still asked.
        ("extra Reis", RICE_IN_TWO_GROUPS),
        # A group without a word is never named.
        ("mit Reis", card(("Beilage", [("Reis", 0)]), ("-", [("Reis", 200)]))),
    ],
)
def test_no_group_named_is_still_asked(said, groups):
    assert wish_of(said, groups).kind == "open"


def test_two_options_each_with_its_own_group():
    wish = wish_of("mit Huhn als Fleisch und Reis als Extra", RICE_IN_TWO_GROUPS)
    assert [(o.group, o.option) for o in wish.options or []] == [
        ("Fleisch", "Huhn"),
        ("Extra", "Reis"),
    ]


def test_two_options_keep_a_removal_next_to_them():
    wish = wish_of("mit Huhn und Nudeln, aber ohne Zwiebeln", BEILAGE)
    assert (wish.kind, wish.note) == ("option", "ohne Zwiebeln")
    assert [o.option for o in wish.options or []] == ["Huhn", "Nudeln"]


@pytest.mark.parametrize(
    "said",
    [
        # Two options of one group: one dish takes one of them.
        "mit Reis und Nudeln",
        "mit Ente und Huhn",
        # One of the two is not on the card (D8).
        "mit Huhn und Pommes",
        # One of the two is in two groups: asked as a whole, never half kept.
        "mit Huhn und Reis",
    ],
)
def test_two_options_that_are_not_definite_stay_declined(said):
    groups = RICE_IN_TWO_GROUPS if said == "mit Huhn und Reis" else BEILAGE
    wish = wish_of(said, groups)
    assert wish.kind == "unknown"
    assert wish.options is None


def test_options_that_share_a_word_are_not_two():
    """ "gebratener Reis" names one option, not Reis next to it."""
    groups = [
        OptionGroup(
            group="Beilage",
            required=False,
            options=[OptionOut(name="Reis", price_delta_cents=0, default=True)],
        ),
        OptionGroup(
            group="Extra",
            required=False,
            options=[
                OptionOut(name="gebratener Reis", price_delta_cents=150, default=False)
            ],
        ),
    ]
    assert wish_of("mit gebratener Reis", groups).kind == "unknown"


def test_a_single_option_carries_no_list():
    assert wish_of("mit Huhn", BEILAGE).options is None


def test_leere_felder_gehen_nicht_ans_modell():
    """(11) CLAUDE.md §2 Regel 6: keine "reason": null in jeder Option."""
    assert (
        "reason"
        not in OptionOut(name="Reis", price_delta_cents=0, default=True).model_dump()
    )
    note = wish_of("ohne Karotten", BEILAGE).model_dump()
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
    assert wish_of("mit Süß-Sauer", groups).kind == "option"


@pytest.mark.parametrize(
    ("gesagt", "hinweis"),
    [
        (
            "ich bin allergisch gegen Erdnüsse und Sesam",
            "WICHTIG !!! Allergie: Erdnüsse und Sesam",
        ),
        (
            "ich vertrage keine Erdnüsse, Sesam und Soja",
            "WICHTIG !!! Allergie: Erdnüsse, Sesam und Soja",
        ),
        (
            "allergisch gegen Sesam und dann noch eine Cola",
            "WICHTIG !!! Allergie: Sesam",
        ),
    ],
)
def test_jede_zutat_einer_allergie_bleibt(gesagt, hinweis):
    """Codex PR #139, P1: eine Aufzaehlung von Zutaten endet nicht am ersten
    "und" - sonst fehlte eine Allergie im Hinweis an die Kueche. Erst ein neuer
    Satzteil ("und dann noch eine Cola") beendet sie."""
    assert wish_of(gesagt, BEILAGE).text == hinweis


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
    wish = wish_of(gesagt, BEILAGE)
    assert (wish.kind, wish.text, wish.option) == ("note", gesagt, None)
    assert first_split(f"die 47 {gesagt}") == ("die 47", gesagt)


def test_extra_nach_dem_weglassen_bleibt_zugabe():
    wish = wish_of("ohne Zwiebeln, extra Nudeln", BEILAGE)
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
    wish = wish_of(gesagt, [])
    assert (wish.kind, wish.ingredient) == ("allergy", None)


@pytest.mark.parametrize(
    "gesagt", ["mit Nudeln ohne Zwiebeln", "mit Nudeln, aber ohne Zwiebeln"]
)
def test_weglassen_nach_der_option_bleibt(gesagt):
    """Codex PR #139, P2: die Option zuerst, das Weglassen danach - beides zaehlt,
    wie in der umgekehrten Reihenfolge."""
    wish = wish_of(gesagt, BEILAGE)
    assert (wish.kind, wish.option, wish.note) == ("option", "Nudeln", "ohne Zwiebeln")


def test_unbekannte_zugabe_mit_weglassen_bleibt_unbekannt():
    assert wish_of("mit Pommes ohne Zwiebeln", BEILAGE).kind == "unknown"


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
            "WICHTIG !!! Allergie: Nuss und Sesam",
        ),
        (
            "ich habe eine Milchallergie und eine Nussallergie",
            "WICHTIG !!! Allergie: Milch und Nuss",
        ),
        (
            "Erdnussallergie und allergisch gegen Sesam",
            "WICHTIG !!! Allergie: Erdnuss und Sesam",
        ),
        ("ich habe eine Erdnuss-Allergie", "WICHTIG !!! Allergie: Erdnuss"),
    ],
)
def test_jede_allergie_im_zusammengesetzten_namen(gesagt, hinweis):
    """Codex PR #139, P1: "Nuss- und Sesamallergie" sind zwei Allergien. Fehlt
    eine im Hinweis, erfaehrt die Kueche nichts davon."""
    assert wish_of(gesagt, BEILAGE).text == hinweis


@pytest.mark.parametrize(
    ("gesagt", "hinweis"),
    [
        ("mit Laktoseintoleranz", "WICHTIG !!! Allergie: Laktose"),
        (
            "ich habe eine Glutenunverträglichkeit",
            "WICHTIG !!! Allergie: Gluten",
        ),
        (
            "Laktose- und Glutenintoleranz",
            "WICHTIG !!! Allergie: Laktose und Gluten",
        ),
        ("ich bin intolerant gegen Laktose", "WICHTIG !!! Allergie: Laktose"),
    ],
)
def test_intoleranz_ist_allergie(gesagt, hinweis):
    """Codex PR #139, P1: "Intoleranz" und "Unvertraeglichkeit" sind Allergie-
    Rede (docs/17), kein unbekannter Wunsch."""
    wish = wish_of(gesagt, BEILAGE)
    assert (wish.kind, wish.text) == ("allergy", hinweis)


def test_intoleranz_in_der_suche(session, tenant_id):
    result = suche(session, tenant_id, "die 23 mit Laktoseintoleranz")
    assert [h.number for h in result.results] == ["23"]
    assert result.wish.text == "WICHTIG !!! Allergie: Laktose"


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
    wish = wish_of(gesagt, BEILAGE)
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
    assert wish_of(gesagt, BEILAGE).ingredient == zutaten


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
    assert wish_of(gesagt, BEILAGE).kind != "allergy"


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
    assert wish_of(gesagt, BEILAGE).ingredient == zutaten


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
    wish = wish_of(gesagt, [])
    assert (wish.kind, wish.ingredient) == ("allergy", None)


# --- Allergy clause next to a wish, denied allergy (issue #252) ----------------------


@pytest.mark.parametrize(
    ("said", "note", "ingredient"),
    [
        (
            "ohne Zwiebeln, ich habe eine Erdnussallergie",
            "WICHTIG !!! Allergie: Erdnuss. Ohne Zwiebeln",
            "Erdnuss",
        ),
        (
            "ohne Zwiebeln ich habe eine Erdnussallergie",
            "WICHTIG !!! Allergie: Erdnuss. Ohne Zwiebeln",
            "Erdnuss",
        ),
        (
            "ohne Zwiebeln und ich bin allergisch gegen Sesam",
            "WICHTIG !!! Allergie: Sesam. Ohne Zwiebeln",
            "Sesam",
        ),
        (
            "keine Zwiebeln, ich vertrage keine Erdnüsse",
            "WICHTIG !!! Allergie: Erdnüsse. Keine Zwiebeln",
            "Erdnüsse",
        ),
        (
            "ohne Zwiebeln, weil ich eine Erdnussallergie habe",
            "WICHTIG !!! Allergie: Erdnuss. Ohne Zwiebeln",
            "Erdnuss",
        ),
        # Without a comma or "ich" the phrase still begins at its own words
        # (own review): none of them goes into the removal.
        (
            "ohne Zwiebeln hab eine Erdnussallergie",
            "WICHTIG !!! Allergie: Erdnuss. Ohne Zwiebeln",
            "Erdnuss",
        ),
        (
            "ohne Zwiebeln und eine Erdnussallergie",
            "WICHTIG !!! Allergie: Erdnuss. Ohne Zwiebeln",
            "Erdnuss",
        ),
        (
            "ohne Zwiebeln bin allergisch gegen Nüsse",
            "WICHTIG !!! Allergie: Nüsse. Ohne Zwiebeln",
            "Nüsse",
        ),
        (
            "ohne Zwiebeln gegen Nüsse allergisch",
            "WICHTIG !!! Allergie: Nüsse. Ohne Zwiebeln",
            "Nüsse",
        ),
        # The removal that comes with an addition stays too; the addition
        # itself is not taken next to an allergy (docs/01_STATUS.md, open point).
        (
            "ohne Zwiebeln, dafür mit Nudeln, ich habe eine Erdnussallergie",
            "WICHTIG !!! Allergie: Erdnuss. Ohne Zwiebeln",
            "Erdnuss",
        ),
    ],
)
def test_removal_before_the_allergy_is_kept(said, note, ingredient):
    """Codex PR #139 (P2): "ohne Zwiebeln, ich habe eine Erdnussallergie" gave
    only the allergy note, the removal was dropped. The kitchen note now names
    both, the allergy first in the fixed wording of E14, then the removal
    as the guest said it."""
    wish = wish_of(said, BEILAGE)
    assert (wish.kind, wish.text, wish.ingredient, wish.note) == (
        "allergy",
        note,
        ingredient,
        None,
    )


def test_removal_before_an_allergy_without_ingredient_waits_in_note():
    """Without an ingredient the agent asks "Wogegen?" first; the removal waits
    in `note` and goes after the note made from the answer."""
    wish = wish_of("ohne Zwiebeln, ich habe eine Allergie", BEILAGE)
    assert (wish.kind, wish.ingredient, wish.note) == (
        "allergy",
        None,
        "ohne Zwiebeln",
    )
    assert wish.text == "ich habe eine Allergie"


@pytest.mark.parametrize(
    ("removal", "allergy", "note"),
    [
        ("ohne Zwiebeln", "WICHTIG !!! Allergie: Erdnuss", None),
        (None, "WICHTIG !!! Allergie: Erdnuss", None),
        # Too long for an order line: the allergy stays, the removal goes.
        (
            "ohne " + "Zwiebeln, " * 20 + "Koriander",
            "WICHTIG !!! Allergie: Erdnuss",
            "WICHTIG !!! Allergie: Erdnuss",
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
    wish = wish_of(said, BEILAGE)
    assert (wish.kind, wish.text if text else None) == (kind, text)


def test_denied_allergy_keeps_an_option_in_two_groups():
    """Own review: the option to ask about turned into "Wogegen?"."""
    reis = OptionOut(name="Reis", price_delta_cents=0, default=False)
    groups = [
        OptionGroup(group="Beilage", required=False, options=[reis]),
        OptionGroup(group="Extra", required=False, options=[reis]),
    ]
    wish = wish_of("mit Reis, ich habe keine Allergie", groups)
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
    wish = wish_of(said, BEILAGE)
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
    wish = wish_of(said, BEILAGE)
    assert (wish.kind, wish.ingredient) == ("allergy", ingredient)


def test_removal_next_to_a_denial_that_asks_stays():
    """Own review, round 5: when the denial falls through to "Wogegen?", the
    removal said with it stays."""
    wish = wish_of(
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
    assert wish is not None
    assert wish.kind == "open"
    assert wish_of(wish.text, BEILAGE).kind == "option"


def test_removal_without_a_tab():
    wish = wish_of("ohne Zwiebeln\tich habe eine Erdnussallergie", [])
    assert wish.text == "WICHTIG !!! Allergie: Erdnuss. Ohne Zwiebeln"


@pytest.mark.parametrize(
    ("said", "text", "note"),
    [
        (
            "ich bin allergisch gegen Erdnüsse, aber ohne Zwiebeln",
            "WICHTIG !!! Allergie: Erdnüsse. Ohne Zwiebeln",
            None,
        ),
        (
            "ich habe eine Erdnussallergie, ohne Zwiebeln bitte",
            "WICHTIG !!! Allergie: Erdnuss. Ohne Zwiebeln",
            None,
        ),
        (
            "ohne Koriander, ich habe eine Erdnussallergie, ohne Zwiebeln",
            "WICHTIG !!! Allergie: Erdnuss. Ohne Koriander, ohne Zwiebeln",
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
            "WICHTIG !!! Allergie: Erdnuss. Keine Zwiebeln",
            None,
        ),
        # Between two allergies, and up to a new dish (own review).
        (
            "ich habe eine Erdnussallergie, ohne Zwiebeln, und eine Sesamallergie",
            "WICHTIG !!! Allergie: Erdnuss und Sesam. Ohne Zwiebeln",
            None,
        ),
        # Codex PR #330, round 3: before every later allergy.
        (
            "ich habe eine Erdnussallergie, aber ohne Zwiebeln sowie eine Sesamallergie",
            "WICHTIG !!! Allergie: Erdnuss und Sesam. Ohne Zwiebeln",
            None,
        ),
        (
            "ich habe eine Erdnussallergie, ohne Zwiebeln oder gegen Sesam bin ich auch allergisch",
            "WICHTIG !!! Allergie: Erdnuss und Sesam. Ohne Zwiebeln",
            None,
        ),
        (
            "ich habe eine Erdnussallergie, ohne Zwiebeln. Ich habe auch eine Sesamallergie",
            "WICHTIG !!! Allergie: Erdnuss und Sesam. Ohne Zwiebeln",
            None,
        ),
        (
            "ich habe eine Erdnussallergie, ohne Zwiebeln, und die Sommerrollen",
            "WICHTIG !!! Allergie: Erdnuss. Ohne Zwiebeln",
            None,
        ),
        (
            "ich habe eine Allergie gegen Erdnüsse, also ohne Erdnusssauce",
            "WICHTIG !!! Allergie: Erdnüsse. Ohne Erdnusssauce",
            None,
        ),
    ],
)
def test_removal_after_the_allergy_is_kept(said, text, note):
    """Codex PR #330: "allergisch gegen Erdnuesse, aber ohne Zwiebeln" lost the
    removal behind the allergy."""
    wish = wish_of(said, BEILAGE)
    assert (wish.kind, wish.text, wish.note) == ("allergy", text, note)


def test_addition_after_the_ingredient_is_no_ingredient():
    """Own review, round 7: "mit Nudeln" went into the allergy note unpriced."""
    wish = wish_of("ich bin allergisch gegen Erdnüsse, mit Nudeln", BEILAGE)
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
    wish = wish_of(said, BEILAGE)
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
    assert wish_of(said, []).ingredient == "Erdnüsse und Sesam"


def test_sonst_does_not_end_the_ingredients():
    """Own review: "sonst" ended the list and the sesame was lost."""
    wish = wish_of("ich bin allergisch gegen Erdnüsse, sonst auch gegen Sesam", [])
    assert wish.ingredient is not None
    assert "Sesam" in wish.ingredient


def test_negation_before_the_phrase_keeps_its_ingredient():
    """Own review: a "nicht auf" in the wish before the allergy cancelled the
    ingredient the guest said clearly; only the phrase itself is read then."""
    wish = wish_of("die Soße nicht auf den Reis, ich habe eine Erdnussallergie", [])
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
    assert removal_with(wish_of(said, [])) == removal


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
            "WICHTIG !!! Allergie: Erdnuss. Ohne Zwiebeln für uns",
        ),
    ],
)
def test_words_of_the_allergy_phrase_stay_out_of_the_removal(said, note):
    assert wish_of(said, []).text == note


def test_auf_in_a_removal_stays_with_it():
    """Own review: the "auf" of "ohne Zwiebeln auf Reis" was moved into the
    allergy clause."""
    wish = wish_of("ohne Zwiebeln auf Reis ich habe eine Erdnussallergie", [])
    assert wish.text == "WICHTIG !!! Allergie: Erdnuss. Ohne Zwiebeln auf Reis"


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
            "WICHTIG !!! Allergie: Erdnüsse. Ohne Zwiebeln",
        ),
        # Round 6: the verb at the end of the clause is no ingredient.
        (
            "ohne Zwiebeln, weil ich allergisch gegen Erdnüsse bin",
            "WICHTIG !!! Allergie: Erdnüsse. Ohne Zwiebeln",
        ),
        (
            "ohne Zwiebeln für meine Frau und ich habe eine Erdnussallergie",
            "WICHTIG !!! Allergie: Erdnuss. Ohne Zwiebeln für meine Frau",
        ),
    ],
)
def test_removal_is_not_cut_by_the_phrase(said, note):
    """Own review: the phrase swallowed the removal or cut it in half."""
    assert wish_of(said, []).text == note


def test_vertrage_alles_ist_keine_allergie():
    """Review PR #139: "ich vertrage alles" ist keine Allergie."""
    assert wish_of("ich vertrage alles", []).kind != "allergy"
    assert wish_candidates("Pho Bo, ich vertrage alles") == []
    assert wish_of("ich vertrage keine Erdnüsse", []).ingredient == "Erdnüsse"


def test_weglassen_nach_unbekannter_zugabe_bleibt_notiert():
    """Review PR #139: "mit Pommes ohne Zwiebeln" - die Zugabe wird abgelehnt, das
    Weglassen bleibt, wie in der umgekehrten Reihenfolge."""
    wish = wish_of("mit Pommes ohne Zwiebeln", BEILAGE)
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
    wish = wish_of(said, BEILAGE)
    assert (wish.kind, wish.text, wish.note) == ("unknown", text, note)


@pytest.mark.parametrize(
    "gesagt", ["allergisch gegen Sesam, ja genau", "allergisch gegen Sesam, richtig"]
)
def test_bestaetigung_nach_der_zutat(gesagt):
    """Review PR #139: ein "ja genau" nach der Zutat macht sie nicht ungueltig."""
    assert wish_of(gesagt, []).ingredient == "Sesam"


def test_allergie_vor_dem_gericht_gehoert_zum_gericht(session, tenant_id):
    """Review PR #139: die Allergie vorn im Satz ist keine eigene Position."""
    gesagt = "Ich habe eine Nussallergie, die 23"
    assert position_parts(session, tenant_id, gesagt, now=NOW) == [
        "die 23, Ich habe eine Nussallergie"
    ]


def test_schema_nennt_die_felder():
    """Review PR #139: ohne leere Felder im Ergebnis, aber mit Schema."""

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
    wish = wish_of(said, BEILAGE)
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
    assert wish_of(said, BEILAGE).ingredient == ingredient


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
        # Codex PR #338: the order goes on after the list.
        "ich bin allergisch gegen Erdnüsse und möchte bestellen",
        "ich bin allergisch gegen Erdnüsse, und möchte zwei Pho Bo",
    ],
)
def test_a_gegen_that_lists_nothing_stays_out(said):
    """A time, "auf jeden Fall" or a clause of its own is no second
    ingredient, and a "gegen" without one before it begins the list."""
    assert wish_of(said, BEILAGE).ingredient == "Erdnüsse"


@pytest.mark.parametrize(
    "said",
    [
        "Ich habe eine Erdnussallergie und meine Frau Sesam",
        "Ich habe eine Erdnussallergie und mein Sohn auch Sesam",
    ],
)
def test_another_persons_allergy_without_gegen_leaves_the_ingredient_open(said):
    """Probe of PR #413 (issue #207), fixed by #670: nothing is noted, so the
    guest is asked "Wogegen?" and no allergen is lost behind the filler."""
    assert wish_of(said, BEILAGE).ingredient is None


@pytest.mark.parametrize(
    "said",
    [
        "ich bin allergisch gegen Nüsse, deshalb die 5",
        "ich bin allergisch gegen Nüsse, deswegen nehme ich die 5",
        "ich bin allergisch gegen Nüsse und deshalb die 5",
        "ich bin allergisch gegen Nüsse, daher die 5",
    ],
)
def test_a_reason_filler_after_the_list_stays_out(said):
    """Probe of PR #413 (issue #207): "deshalb die 5" is the order going on,
    not part of the ingredient on the kitchen note."""
    assert wish_of(said, BEILAGE).ingredient == "Nüsse"


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
    assert kept in wish_of(said, BEILAGE).text


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
        # The same behind a comma, a negation, a hesitation or "alle".
        "Ich habe eine Erdnussallergie, und dann noch Sesam",
        "Ich habe eine Laktoseintoleranz, und dann noch Gluten",
        "Ich vertrage keine Erdnüsse und dann auch kein Sesam",
        "Ich vertrage kein Gluten, und dann auch keine Laktose",
        "Ich habe eine Erdnussallergie und noch dazu Sesam",
        "Ich habe eine Erdnussallergie und, ähm, Sesam",
        "Ich habe eine Erdnussallergie und alle anderen Nüsse",
        "Ich habe eine Erdnussallergie, ja, und dann Sesam",
    ],
)
def test_more_than_the_parser_reads_is_asked_about(said):
    """Issue #207: only the first allergen reached the kitchen note and the
    agent did not ask. What the parser does not read is never noted in part:
    the ingredient stays open, and the agent asks "Wogegen?" (PR #361)."""
    wish = wish_of(said, BEILAGE)
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
        ("Ich habe eine Erdnussallergie, ja, und dann die 13", "Erdnuss"),
        ("Ich habe eine Erdnussallergie und alle anderen sind gesund", "Erdnuss"),
        # A word for many things is no ingredient (#207).
        ("Ich bin gegen vieles allergisch", None),
        ("Ich bin allergisch gegen alles Mögliche", None),
    ],
)
def test_what_goes_on_with_the_call_reads_as_on_main(said, ingredient):
    wish = wish_of(said, BEILAGE)
    assert (wish.kind, wish.ingredient) == ("allergy", ingredient)


@pytest.mark.parametrize(
    ("said", "text", "note"),
    [
        # Issue #331 (Codex PR #330): "aber bitte keine".
        (
            "ich bin allergisch gegen Erdnüsse, aber bitte keine Zwiebeln",
            "WICHTIG !!! Allergie: Erdnüsse. Keine Zwiebeln",
            None,
        ),
        (
            "ich habe eine Erdnussallergie, aber auch keine Zwiebeln",
            "WICHTIG !!! Allergie: Erdnuss. Keine Zwiebeln",
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
            "WICHTIG !!! Allergie: Erdnüsse. Keine Mandeln",
            None,
        ),
    ],
)
def test_a_removal_after_aber_or_bitte_is_kept(said, text, note):
    """Issue #331: only a "keine" right after "aber" opened a removal, so
    "aber bitte keine Zwiebeln" lost the onions."""
    wish = wish_of(said, BEILAGE)
    assert (wish.kind, wish.text, wish.note) == ("allergy", text, note)


def test_keine_without_aber_or_bitte_still_belongs_to_the_allergy():
    """A "keine" that no "aber" or "bitte" leads to may still name an
    allergen: it is not taken as a removal (Codex PR #330)."""
    wish = wish_of("ich vertrage keine Erdnüsse, keine Mandeln", BEILAGE)
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
    wish = wish_of(f"{removal}, ich habe eine Allergie", BEILAGE)
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
    wish = wish_of(said, BEILAGE)
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
    wish = wish_of(said, BEILAGE)
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
    wish = wish_of(said, BEILAGE)
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
    wish = wish_of(said, BEILAGE)
    assert (wish.kind, wish.ingredient) == ("allergy", None)


def test_a_denial_after_aber_keeps_the_question():
    """Codex PR #361, round 4: "aber ich habe keine Allergie" contradicts the
    allergy before it; it is asked about, never noted."""
    wish = wish_of(
        "ich bin allergisch gegen Erdnüsse, aber ich habe keine Allergie", BEILAGE
    )
    assert (wish.kind, wish.ingredient) == ("allergy", None)


# --- #670: allergy sentences the parser misread (L13) ---------------------------


@pytest.mark.parametrize(
    "said",
    [
        "keine Allergie gegen Erdnüsse, aber Sesam",
        "Ich habe keine Allergie, nur Laktose",
        "Ich habe keine Allergie gegen Nüsse, sondern gegen Sesam",
    ],
)
def test_a_denial_with_an_allergen_behind_it_does_not_only_deny(said):
    """The allergen behind the denial is noted or asked for (#670, P1)."""
    from api.domain.menu.wishes import only_denies

    assert not only_denies(said)
    assert wish_of(said, BEILAGE).kind == "allergy"


@pytest.mark.parametrize(
    "said",
    [
        "Ich habe keine Allergie, aber bitte ohne Zwiebeln",
        "Ich habe keine Allergie, aber die 13 bitte",
    ],
)
def test_a_denial_with_a_wish_behind_it_still_only_denies(said):
    from api.domain.menu.wishes import only_denies

    assert only_denies(said)


@pytest.mark.parametrize(
    "said",
    [
        "Ich bin allergisch gegen Nüsse, meine Frau gegen Sesam",
        "Erdnussallergie, mein Mann Sesam",
        # A list repeats no other preposition: "auf" after "gegen" (Codex PR
        # #338's trade-off, reversed by #670: one question, no lost allergen).
        "Ich bin allergisch gegen Erdnüsse und auf Sesam",
        "ich bin allergisch gegen Erdnüsse und auf Diät",
    ],
)
def test_a_second_person_or_preposition_leaves_the_ingredient_open(said):
    """Never the wife's allergy as the caller's, never a dropped sesame
    (#670, P1): the ingredient stays open and the guest is asked."""
    wish = wish_of(said, BEILAGE)
    assert wish.kind == "allergy" and wish.ingredient is None


@pytest.mark.parametrize(
    "said",
    [
        "ich vertrage Erdnüsse gar nicht",
        "ich vertrage Nüsse leider nicht",
        "ich vertrage Kuhmilch und Sahne nicht",
    ],
)
def test_vertrage_with_its_negation_at_the_clause_end_is_an_allergy(said):
    """(#670, P1): noted after the question "Wogegen?", as "ich vertrage
    Milch nicht" already was."""
    assert mentions_allergy(said)
    wish = wish_of(said, BEILAGE)
    assert wish.kind == "allergy" and wish.ingredient is None


@pytest.mark.parametrize(
    "said",
    [
        "ich vertrage Erdnüsse sehr gut und nehme die 13",
        # Codex PR #718: the "nicht" of another clause.
        "Ich vertrage Erdnüsse gut und möchte nicht scharf essen",
        "Ich vertrage alles, nur nicht zu scharf",
    ],
)
def test_vertrage_without_its_own_negation_is_no_allergy(said):
    assert not mentions_allergy(said)


def test_another_persons_allergy_behind_a_denial_is_never_the_callers():
    """Codex PR #718: "nur meine Frau gegen Sesam" behind the caller's denial
    must not become the caller's allergy, which the caller's consent would
    note with its reason."""
    from api.domain.menu.wishes import only_denies

    assert only_denies("Ich habe keine Allergie, nur meine Frau gegen Sesam")


# --- #671: a priced addition never goes out as a free note --------------------------

EXTRA = [
    *BEILAGE,
    OptionGroup(
        group="Extra",
        required=False,
        options=[
            OptionOut(name="Ei", price_delta_cents=100, default=False),
            OptionOut(name="Tofu", price_delta_cents=150, default=False),
        ],
    ),
]


@pytest.mark.parametrize(
    "said",
    [
        "mit Nudeln, ohne Zwiebeln, mit Tofu",
        "mit Nudeln, ohne Zwiebeln, extra Ei",
        "mit Nudeln, ohne Zwiebeln statt Reis",
        "mit Nudeln, ohne Huhn, dafür Tofu",
    ],
)
def test_addition_behind_a_removal_is_not_a_note(said):
    """#671 P1: what follows the first "ohne" was kept whole as the note, so
    a second priced addition there went out free next to the first option."""
    wish = wish_of(said, EXTRA)
    assert wish.kind == "unknown"
    assert "Tofu" not in (wish.note or "") and "Ei" not in (wish.note or "")


def test_option_keeps_a_plain_removal_behind_it():
    wish = wish_of("mit Nudeln, ohne Zwiebeln, ohne Koriander", EXTRA)
    assert (wish.kind, wish.option, wish.price_delta_cents) == ("option", "Nudeln", 300)
    assert wish.note == "ohne Zwiebeln, ohne Koriander"


@pytest.mark.parametrize(
    "said",
    [
        "ohne Huhn, dafür Tofu",
        "kein Huhn, sondern Tofu",
        "ohne Huhn, lieber Tofu",
        "ohne Huhn, dafür mit Tofu",
    ],
)
def test_swap_after_a_removal_is_priced(said):
    """#671: "dafür", "sondern" and "lieber" open an addition like "mit"."""
    wish = wish_of(said, EXTRA)
    assert (wish.kind, wish.option, wish.price_delta_cents) == ("option", "Tofu", 150)
    assert wish.note in ("ohne Huhn", "kein Huhn")


@pytest.mark.parametrize(
    "said", ["ohne Zwiebeln, lieber scharf", "kein Huhn, sondern was anderes"]
)
def test_unknown_swap_after_a_removal_stays_open(said):
    """Not a free note either: the agent asks about what the menu does not know."""
    assert wish_of(said, EXTRA).kind == "unknown"


@pytest.mark.parametrize(
    "said",
    ["ohne Huhn, dafür Tofu, ohne Zwiebeln", "ohne Huhn, mit Tofu, ohne Zwiebeln"],
)
def test_a_removal_behind_the_addition_is_kept(said):
    """Codex PR #720: the removal before the addition overwrote the one after it."""
    wish = wish_of(said, EXTRA)
    assert (wish.kind, wish.option) == ("option", "Tofu")
    assert wish.note == "ohne Huhn, ohne Zwiebeln"


def test_removal_ending_in_dafuer_stays_a_note():
    assert wish_of("ohne Zwiebeln, dafür", EXTRA).kind == "note"


@pytest.mark.parametrize(
    "said", ["die 23 mit 2x Ei", "die 23 mit 2 Stück Ei", "die 23 mit zweimal Ei"]
)
def test_count_inside_an_addition_reaches_the_ask_back(said):
    """#671: the count of an addition was dropped as a dish quantity, so one
    egg was charged where two were asked for."""
    from api.domain.menu.wishes import has_number

    assert has_number(first_split(said)[1])
    assert wish_of("mit 2x Ei", EXTRA).kind == "unknown"


def test_count_at_the_end_still_belongs_to_the_dish():
    assert not first_split("die 23 mit Ei, zweimal")[1].endswith("zweimal")


@pytest.mark.parametrize("said", ["Nummer 23 mit 2x Ei", "Nummer 23 mit zweimal Ei"])
def test_search_asks_about_a_counted_addition(session, tenant_id, said):
    """Rule A, as for "Nummer 23 mit 2 Soßen": the second number is asked about."""
    with pytest.raises(Ambiguous) as err:
        suche(session, tenant_id, said)
    assert "Welche Nummer" in (err.value.say or "")


@pytest.mark.parametrize(
    ("said", "ingredient"),
    [
        ("Ich habe keine Allergie, nur meine Frau gegen Sesam", "Sesam"),
        ("Ich habe keine Allergie, aber meine Frau hat eine Sesamallergie", "Sesam"),
        ("Ich habe keine Allergie, mein Mann aber gegen Erdnuss", "Erdnuss"),
        ("Ich habe keine Allergie, nur meine Tochter gegen Nüsse", "Nüsse"),
        # An answer word in front says nothing of its own (review of #911).
        ("Nein, ich habe keine Allergie, nur meine Frau gegen Sesam", "Sesam"),
        ("Ja, ich habe keine Allergie, nur meine Frau gegen Sesam", "Sesam"),
    ],
)
def test_an_allergy_behind_the_callers_denial_is_read(said, ingredient):
    """#719: the allergy of the person named behind the denial is read."""
    wish = wish_of(said, [])
    assert (wish.kind, wish.ingredient) == ("allergy", ingredient)


@pytest.mark.parametrize(
    "said",
    [
        # Codex PR #735: the wife denies too.
        "Ich habe keine Allergie, aber meine Frau hat keine Sesamallergie",
        # Codex PR #735: two people behind the denial, one "gegen" each.
        "Ich habe keine Allergie, nur meine Frau gegen Sesam und ich gegen Nüsse",
        # Codex PR #735: the list or a second person goes on behind a comma.
        "Ich habe keine Allergie, nur meine Frau gegen Sesam, Nüsse und Milch",
        "Ich habe keine Allergie, nur meine Frau gegen Sesam, und mein Mann gegen Nüsse",
        # Codex PR #735: one elided, one compound allergy of two people.
        "Ich habe keine Allergie, nur meine Frau gegen Sesam und mein Mann eine Nussallergie",
        # Codex PR #735: an intolerance compound beside the elided allergy.
        "Ich habe keine Allergie, nur meine Frau gegen Sesam und eine Laktoseintoleranz",
        "Ich habe keine Allergie, nur meine Frau gegen Sesam und eine Milchunverträglichkeit",
        # Codex PR #735: a verbal intolerance, and an intolerance behind "ohne".
        "Ich habe keine Allergie, nur meine Frau gegen Sesam und verträgt Milch nicht",
        "Ich habe keine Allergie, nur meine Frau gegen Sesam, ohne Milch wegen Laktoseintoleranz",
        # Codex PR #735: an elided "gegen" inside a later removal clause.
        "Ich habe keine Allergie, nur meine Frau gegen Sesam, ohne Milch und gegen Nüsse",
        # Review #207, regression of #735: the caller's own allergy, or words
        # no health reader knows, in front of the denial; and an option there
        # that the reading of the other one's allergy would drop.
        "Ich habe eine Nussallergie! Ich habe keine andere Allergie, nur meine Frau gegen Sesam",
        "Ich meide Gluten. Ich habe keine Allergie, nur meine Frau gegen Sesam",
        "Ohne Zwiebeln, ich meide Gluten. Ich habe keine Allergie, nur meine Frau gegen Sesam",
        "Ohne Zwiebeln mit Nudeln. Ich habe keine Allergie, nur meine Frau gegen Sesam",
        "Mit Nudeln, ich habe keine Allergie, nur meine Frau gegen Sesam",
    ],
)
def test_a_doubtful_clause_behind_a_denial_gives_no_ingredient(said):
    wish = classify_wish(said, [])
    assert wish is None or wish.ingredient is None


@pytest.mark.parametrize(
    "said",
    [
        "Ohne Zwiebeln, ich habe keine Allergie, nur meine Frau gegen Sesam",
        "Ich habe keine Allergie, nur meine Frau gegen Sesam, und ohne Zwiebeln",
        # Codex PR #735: the removal shares the denial's clause.
        "Ohne Zwiebeln und ich habe keine Allergie, nur meine Frau gegen Sesam",
    ],
)
def test_a_removal_beside_the_other_adults_allergy_stays(said):
    """Codex PR #735: the removal was dropped by the new reading."""
    wish = wish_of(said, [])
    assert wish.ingredient == "Sesam"
    assert removal_with(wish) == "ohne Zwiebeln"


def test_a_comma_chain_is_read_in_linear_time():
    """Codex PR #735: every part in front of a comma was classified again."""
    import time

    start = time.perf_counter()
    classify_wish("Ich habe eine Allergie" + ", x" * 30, [])
    assert time.perf_counter() - start < 0.5


@pytest.mark.parametrize(
    ("text", "health"),
    [
        ("Ich darf kein Gluten.", True),
        ("Zöliakie, ohne Gluten", True),
        # "bedarf" is no "darf" (#763 review).
        ("Das bedarf keiner Reservierung.", False),
        ("Das darf keiner wissen, es ist eine Überraschung.", False),
        ("Ich darf keine Nüsse.", True),
        # Codex PR #767, P1: "keinerlei" is the strong "kein".
        ("Ich darf keinerlei Gluten.", True),
        ("ohne Gluten", False),
    ],
)
def test_health_words_need_the_whole_word(text, health):
    assert speaks_of_health(text) is health


# --- Everything after the first "statt" was cut (deep review of #1076) -------------

# The register names a swap as one option of its own (eval card dishes 32, 33).
SWAP_OPTION = [
    OptionGroup(
        group="Fleisch",
        required=True,
        options=[
            OptionOut(name="Huhn", price_delta_cents=0, default=True),
            OptionOut(name="Tofu", price_delta_cents=0, default=False),
        ],
    ),
    OptionGroup(
        group="Extras",
        required=False,
        options=[
            OptionOut(name="Nudeln statt Reis", price_delta_cents=350, default=False),
            OptionOut(name="extra Gemüse", price_delta_cents=150, default=False),
        ],
    ),
]


@pytest.mark.parametrize("said", ["Nudeln statt Reis", "mit Nudeln statt Reis bitte"])
def test_an_option_named_with_statt_matches_as_a_whole(said):
    wish = wish_of(said, SWAP_OPTION)
    assert (wish.kind, wish.group, wish.option) == (
        "option",
        "Extras",
        "Nudeln statt Reis",
    )
    assert wish.price_delta_cents == 350


def test_a_second_option_next_to_a_statt_option_is_kept():
    """abholung_0064: "mit Tofu und Nudeln statt Reis" was declined."""
    wish = wish_of("mit Tofu und Nudeln statt Reis", SWAP_OPTION)
    assert wish.kind == "option"
    assert [(o.group, o.option, o.price_delta_cents) for o in wish.options or []] == [
        ("Fleisch", "Tofu", 0),
        ("Extras", "Nudeln statt Reis", 350),
    ]


@pytest.mark.parametrize(
    "said",
    [
        "Nudeln statt Reis, Huhn",
        "Nudeln statt Reis, mit Huhn",
        "Nudeln statt Reis, und Huhn",
        "Huhn statt Ente, Nudeln statt Reis",
        "Nudeln statt dem Reis, Huhn",
    ],
)
def test_words_behind_a_comma_after_the_swap_still_count(said):
    """ "Nudeln statt Reis, Huhn" dropped Huhn without a word."""
    wish = wish_of(said, BEILAGE)
    assert wish.kind == "option"
    assert sorted((o.group, o.option) for o in wish.options or []) == [
        ("Beilage", "Nudeln"),
        ("Fleisch", "Huhn"),
    ]


@pytest.mark.parametrize(
    ("said", "option"),
    [
        # What follows "statt mit" is turned down, never wished.
        ("mit Huhn statt mit Nudeln", "Huhn"),
        ("mit Nudeln statt mit Reis", "Nudeln"),
        ("Huhn statt der Ente", "Huhn"),
    ],
)
def test_the_word_after_statt_mit_is_swapped_out(said, option):
    wish = wish_of(said, BEILAGE)
    assert (wish.kind, wish.option, wish.options) == ("option", option, None)


@pytest.mark.parametrize(
    "said", ["Tofu statt Huhn, Nudeln statt Reis", "Tofu statt Huhn Nudeln statt Reis"]
)
def test_a_statt_option_behind_another_swap_is_kept(said):
    """The swap in front ended only at a joining word and ran over the card's
    "Nudeln statt Reis" (deep review of chain round 4 item 26)."""
    wish = wish_of(said, SWAP_OPTION)
    assert [(o.group, o.option) for o in wish.options or []] == [
        ("Fleisch", "Tofu"),
        ("Extras", "Nudeln statt Reis"),
    ]


@pytest.mark.parametrize(
    "said",
    [
        # "und" or "oder" behind the swapped word may join a second one.
        "Nudeln statt Reis und Huhn",
        "Nudeln statt Reis und Pommes",
        "Nudeln statt Reis oder Pommes",
        "statt Reis und Nudeln",
        # A second word behind the swapped one may be the wished one.
        "Nudeln statt gebratenem Reis",
        "statt Reis Nudeln und Huhn",
        "Nudeln statt Reis mit Huhn",
        "Nudeln statt Reis dazu Huhn",
        # Nothing named as swapped out.
        "Nudeln statt",
    ],
)
def test_what_the_swap_leaves_unclear_is_asked(said):
    assert wish_of(said, BEILAGE).kind == "unknown"


@pytest.mark.parametrize(
    ("said", "groups"),
    [
        # The swap is turned down, never booked.
        ("keine Nudeln statt Reis", SWAP_OPTION),
        ("ohne Nudeln statt Reis", SWAP_OPTION),
        ("keine Nudeln statt Reis", BEILAGE),
        ("ohne Reis statt Nudeln", BEILAGE),
        # Tofu would stand in the removal ("ohne Zwiebeln, Tofu").
        ("ohne Zwiebeln, Tofu und Nudeln statt Reis", SWAP_OPTION),
        ("ohne Zwiebeln, Tofu, Nudeln statt Reis", SWAP_OPTION),
    ],
)
def test_a_removal_in_front_of_a_swap_that_is_not_clear_is_asked(said, groups):
    wish = wish_of(said, groups)
    assert wish.kind == "unknown"
    assert wish.price_delta_cents is None and wish.options is None


def test_a_plain_removal_in_front_of_a_statt_option_stays_a_note():
    wish = wish_of("ohne Zwiebeln, Nudeln statt Reis", SWAP_OPTION)
    assert (wish.kind, wish.option, wish.price_delta_cents) == (
        "option",
        "Nudeln statt Reis",
        350,
    )
    assert wish.note == "ohne Zwiebeln"


def test_stattdessen_takes_the_addition_with_its_price():
    """ "ohne Reis, stattdessen Nudeln" went out as a free note."""
    wish = wish_of("ohne Reis, stattdessen Nudeln", BEILAGE)
    assert (wish.kind, wish.option, wish.price_delta_cents) == ("option", "Nudeln", 300)
    assert wish.note == "ohne Reis"


def test_search_keeps_the_option_after_the_swap(session, tenant_id):
    result = suche(session, tenant_id, "die 47 Nudeln statt Reis, Huhn")
    assert [h.number for h in result.results] == ["47"]
    assert sorted((o.group, o.option) for o in result.wish.options or []) == [
        ("Beilage", "Nudeln"),
        ("Fleisch", "Huhn"),
    ]
