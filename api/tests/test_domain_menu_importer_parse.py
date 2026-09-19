"""domain/menu/importer.parse und normalize: CSV nach docs/14 lesen und prüfen, ohne DB (T-4.2)."""

import pytest

from api.domain.menu.importer import (
    ALIASES_FILE,
    ALLERGENS_FILE,
    MENU_FILE,
    OPTIONS_FILE,
    parse,
    parse_eur,
)
from api.domain.menu.normalize import normalize_alias

MENU = """number;name;category;price_eur;description;active
23;Frühlingsrollen (4 Stück);Vorspeisen;6,90;mit Gemüsefüllung;ja
47;Ente knusprig;Hauptgerichte;15,50;;
12;Wan-Tan-Suppe;Suppen;5;;nein
"""
OPTIONS = """number;group_name;option_name;price_delta_eur;is_default;required
47;Fleisch;Huhn;0,00;ja;ja
47;Fleisch;Ente;2,50;nein;ja
47;Größe;klein;-1,00;nein;nein
"""
ALLERGENS = """number;allergen_codes;confirmed_by
23;A,F;Maxi
47;;
"""
ALIASES = """number;alias
23;Frühlingsrollen
23;Die knusprigen Rollen!
47;die knusprige Ente
12;Suppe
"""


def files(**over):
    base = {
        MENU_FILE: MENU,
        OPTIONS_FILE: OPTIONS,
        ALLERGENS_FILE: ALLERGENS,
        ALIASES_FILE: ALIASES,
    }
    base.update(over)
    return base


# --- normalize ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("roh", "erwartet"),
    [
        ("Die knusprigen Rollen!", "die knusprigen rollen"),
        ("  Wan-Tan   Suppe ", "wan-tan suppe"),
        ("„Bun Bo“", "bun bo"),
        ("Soße", "soße"),
        # Zerlegtes u + Trema (NFD) wird ein Zeichen.
        ("Fu\u0308llung", "f\u00fcllung"),
        ("?!", ""),
    ],
)
def test_normalize_alias(roh, erwartet):
    assert normalize_alias(roh) == erwartet


# --- Preise --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "cent"),
    [
        ("6,90", 690),
        ("6,9", 690),
        ("6", 600),
        ("0,05", 5),
        ("-1,00", -100),
        (" 15,50 ", 1550),
    ],
)
def test_preis_in_cent(text, cent):
    assert parse_eur(text) == cent


@pytest.mark.parametrize(
    "text", ["6.90", "1.200,00", "6,905", "", "sechs", "6,-", "€6,90"]
)
def test_mehrdeutiger_preis_wird_nicht_geraten(text):
    assert parse_eur(text) is None


# --- gueltige Karte ------------------------------------------------------------


def test_gueltige_karte():
    plan = parse(files())

    assert plan.ok, plan.errors
    assert plan.items["23"].price_cents == 690
    assert plan.items["47"].active is True  # leer heisst ja
    assert plan.items["12"].active is False
    assert plan.items["47"].description is None
    assert [o.option_name for o in plan.options["47"]] == ["Huhn", "Ente", "klein"]
    assert plan.options["47"][2].price_delta_cents == -100
    assert plan.allergens["23"].codes == ("A", "F")
    # Leere Zeile: keine Auskunft, nicht "keine Allergene".
    assert plan.allergens["47"].codes == ()
    assert plan.aliases["23"] == {"frühlingsrollen", "die knusprigen rollen"}


def test_nur_die_karte_reicht():
    plan = parse({MENU_FILE: MENU})

    assert plan.ok and plan.options == {} and plan.allergens == {}
    assert any("ohne Alias" in w for w in plan.warnings)


def test_bom_und_leerzeilen_stoeren_nicht():
    plan = parse(files(**{MENU_FILE: "\ufeff" + MENU + ";;;;;\n\n"}))

    assert plan.ok and set(plan.items) == {"23", "47", "12"}


# --- Pruefregeln docs/14 ---------------------------------------------------------


def fehler(**over) -> str:
    plan = parse(files(**over))
    assert not plan.ok
    return " | ".join(plan.errors)


def test_ohne_kartendatei_kein_import():
    plan = parse({})
    assert plan.errors == [f"{MENU_FILE} fehlt"]


def test_doppelte_nummer():
    assert "Nummer 23 doppelt (zuerst in Zeile 2)" in fehler(
        **{MENU_FILE: MENU + "23;Nochmal;Vorspeisen;1,00;;\n"}
    )


def test_preis_nicht_lesbar_mit_zeile():
    text = fehler(**{MENU_FILE: MENU.replace("6,90", "6.90")})
    assert "menu_items.csv Zeile 2" in text and "6.90" in text


def test_negativer_gerichtspreis():
    assert "nicht lesbar" in fehler(**{MENU_FILE: MENU.replace("6,90", "-6,90")})


def test_pflichtfelder():
    text = fehler(**{MENU_FILE: MENU + "99;;;1,00;;\n"})
    assert "Name fehlt" in text and "Kategorie fehlt" in text


def test_active_nur_ja_oder_nein():
    assert "weder ja noch nein" in fehler(
        **{MENU_FILE: MENU.replace("5;;nein", "5;;vielleicht")}
    )


def test_spalte_fehlt():
    assert "Spalte fehlt: price_eur" in fehler(
        **{MENU_FILE: "number;name;category\n23;Rollen;Vorspeisen\n"}
    )


@pytest.mark.parametrize("datei", [OPTIONS_FILE, ALLERGENS_FILE, ALIASES_FILE])
def test_unbekannte_nummer(datei):
    zeile = {
        OPTIONS_FILE: "99;Größe;groß;1,00;nein;nein\n",
        ALLERGENS_FILE: "99;A;Maxi\n",
        ALIASES_FILE: "99;irgendwas\n",
    }[datei]
    original = files()[datei]
    assert "Nummer 99 gibt es nicht" in fehler(**{datei: original + zeile})


def test_pflichtgruppe_ohne_default():
    text = fehler(**{OPTIONS_FILE: OPTIONS.replace("Huhn;0,00;ja", "Huhn;0,00;nein")})
    assert "47/Fleisch" in text and "genau einen Default, hat 0" in text


def test_pflichtgruppe_mit_zwei_defaults():
    text = fehler(**{OPTIONS_FILE: OPTIONS.replace("Ente;2,50;nein", "Ente;2,50;ja")})
    assert "hat 2" in text


def test_required_uneinheitlich():
    text = fehler(
        **{OPTIONS_FILE: OPTIONS.replace("Ente;2,50;nein;ja", "Ente;2,50;nein;nein")}
    )
    assert "nicht einheitlich" in text


def test_doppelte_option():
    assert "Option doppelt" in fehler(
        **{OPTIONS_FILE: OPTIONS + "47;Größe;klein;-1,00;nein;nein\n"}
    )


@pytest.mark.parametrize("code", ["I", "X", "Q", "AB"])
def test_unbekannter_allergen_code(code):
    text = fehler(
        **{ALLERGENS_FILE: f"number;allergen_codes;confirmed_by\n23;A,{code};Maxi\n"}
    )
    assert f"unbekannter LMIV-Code {code}" in text


def test_allergen_kleinbuchstabe_wird_gross():
    plan = parse(
        files(
            **{ALLERGENS_FILE: "number;allergen_codes;confirmed_by\n23; a , g ;Maxi\n"}
        )
    )
    assert plan.ok and plan.allergens["23"].codes == ("A", "G")


def test_allergen_ohne_pruefer():
    assert "confirmed_by fehlt" in fehler(
        **{ALLERGENS_FILE: "number;allergen_codes;confirmed_by\n23;A;\n"}
    )


def test_allergen_nummer_doppelt():
    assert "Nummer 23 doppelt" in fehler(**{ALLERGENS_FILE: ALLERGENS + "23;C;Maxi\n"})


def test_leerer_alias():
    assert "Alias leer" in fehler(**{ALIASES_FILE: ALIASES + "23;?!\n"})


# --- Warnungen -------------------------------------------------------------------


def test_alias_zu_zwei_gerichten_warnt():
    plan = parse(files(**{ALIASES_FILE: ALIASES + "47;Suppe\n"}))

    assert plan.ok
    assert "Alias „suppe“ führt zu mehreren Gerichten: 12, 47" in plan.warnings


def test_gericht_ohne_alias_warnt():
    plan = parse(files(**{ALIASES_FILE: "number;alias\n23;Rollen\n"}))

    assert plan.ok
    assert "Gericht ohne Alias: 12, 47" in plan.warnings


def test_alias_doppelt_im_gleichen_gericht_zaehlt_einmal():
    plan = parse(files(**{ALIASES_FILE: ALIASES + "23;frühlingsrollen\n"}))

    assert plan.ok and len(plan.aliases["23"]) == 2


# --- Kartennummern, die die Suche eindeutig aufloesen kann (Codex PR #117, P1) ----


@pytest.mark.parametrize("nummer", ["23", "23a", "23F", "007", "12c"])
def test_kartennummer_im_suchformat(nummer):
    plan = parse(
        {MENU_FILE: f"number;name;category;price_eur\n{nummer};Gericht;Test;1,00\n"}
    )
    assert plan.ok, plan.errors


@pytest.mark.parametrize("nummer", ["23g", "A12", "23x", "23ab", "12-3", "Nr. 5", "V2"])
def test_kartennummer_ausserhalb_des_suchformats(nummer):
    """Sonst sucht "Nummer 23g" still die 23: lieber beim Import scheitern."""
    plan = parse(
        {MENU_FILE: f"number;name;category;price_eur\n{nummer};Gericht;Test;1,00\n"}
    )
    assert not plan.ok
    assert "Kartennummer" in plan.errors[0] and nummer in plan.errors[0]


@pytest.mark.parametrize("nummer", ["1000", "1000a", "12345"])
def test_kartennummer_ueber_dem_zahlbereich_der_suche(nummer):
    """numberwords liest bis 999: groessere Nummern waeren per Nummer nie findbar."""
    plan = parse(
        {MENU_FILE: f"number;name;category;price_eur\n{nummer};Gericht;Test;1,00\n"}
    )
    assert not plan.ok and "Kartennummer" in plan.errors[0]


@pytest.mark.parametrize("nummer", ["999", "0999", "0007"])
def test_kartennummer_bis_999_auch_mit_nullen(nummer):
    plan = parse(
        {MENU_FILE: f"number;name;category;price_eur\n{nummer};Gericht;Test;1,00\n"}
    )
    assert plan.ok, plan.errors


def test_buchstabe_wird_klein_gespeichert():
    plan = parse({MENU_FILE: "number;name;category;price_eur\n23A;Gericht;Test;1,00\n"})
    assert plan.ok and set(plan.items) == {"23a"}


def test_gross_und_klein_sind_dieselbe_nummer():
    """Sonst entstehen 23a und 23A, und jede Suche nach "Nummer 23a" fragt nach."""
    plan = parse(
        {
            MENU_FILE: "number;name;category;price_eur\n23a;Eins;Test;1,00\n23A;Zwei;Test;1,00\n"
        }
    )
    assert not plan.ok and "Nummer 23a doppelt" in plan.errors[0]


def test_optionen_finden_die_nummer_unabhaengig_von_der_schreibweise():
    plan = parse(
        {
            MENU_FILE: "number;name;category;price_eur\n23A;Gericht;Test;1,00\n",
            OPTIONS_FILE: "number;group_name;option_name;price_delta_eur;is_default;required\n"
            "23a;Größe;groß;1,00;nein;nein\n",
        }
    )
    assert plan.ok, plan.errors and "23a" in plan.options


@pytest.mark.parametrize(("a", "b"), [("7", "07"), ("7a", "07a"), ("007", "7")])
def test_fuehrende_null_ist_dieselbe_nummer(a, b):
    """Codex PR #117: so vergleicht auch die Suche - sonst dauerhaft mehrdeutig."""
    plan = parse(
        {
            MENU_FILE: f"number;name;category;price_eur\n{a};Eins;Test;1,00\n{b};Zwei;Test;1,00\n"
        }
    )
    assert not plan.ok and "doppelt" in plan.errors[0]


def test_optionen_finden_die_nummer_auch_ohne_null():
    plan = parse(
        {
            MENU_FILE: "number;name;category;price_eur\n07;Misosuppe;Suppen;4,50\n",
            OPTIONS_FILE: "number;group_name;option_name;price_delta_eur;is_default;required\n"
            "7;Größe;groß;1,00;nein;nein\n",
            ALIASES_FILE: "number;alias\n7;miso\n",
        }
    )
    assert plan.ok, plan.errors
    assert set(plan.items) == {"07"} and "07" in plan.options and "07" in plan.aliases
