"""domain/menu/pos_convert: Kasse -> CSV nach docs/14 (T-4.11). Nur synthetische Daten."""

import pytest

from api.domain.menu.importer import ALLERGENS_FILE, MENU_FILE, OPTIONS_FILE, parse
from api.domain.menu.pos_convert import (
    ALLERGEN_KINDS,
    ALLERGEN_MAP,
    ALLERGEN_NAMES,
    cents,
    convert,
    extra_name,
)
from api.domain.menu.pos_dbf import DbfError, Field, Table


def table(rows, deleted=()):
    names = sorted({k for r in rows for k in r})
    fields = tuple(Field(n, "C", 10, 0) for n in names)
    full = tuple({n: r.get(n, "") for n in names} for r in rows)
    return Table(fields, "cp437", full, tuple(i in deleted for i in range(len(rows))))


def artikel(artnr, name, wrg="006", vk1="13.50", groesse="+-1", **extra):
    row = {
        "ARTNR": artnr,
        "BEZEICH": name,
        "WRG": wrg,
        "VK1_PREIS": vk1,
        "VK2_PREIS": vk1,
        "GROESSE": groesse,
    }
    row.update(extra)
    return row


WARENGRP = table(
    [
        {"W_WRG": "001", "W_BEZEICH": "Suppe"},
        {"W_WRG": "006", "W_BEZEICH": "Hauptspeisen"},
        {"W_WRG": "015", "W_BEZEICH": "Alkoholfreie Get"},
    ]
)
ZUTGRP = table(
    [
        {"ZGRP": "C", "ZPREIS": "1.20", "ZGRP3": "C"},
        {"ZGRP": "U", "ZPREIS": "3.50", "ZGRP3": "U"},
    ]
)
ZUTATEN = table(
    [
        # Soßenwechsel bei Hauptspeisen, bei Größe 2 kostenlos
        {
            "ZBEZEICH": "B.Mango_Curry",
            "WRGSHOWALL": "F",
            "WRGSHOW": "006\r\n008",
            "ZPREIGRP3": "U",
            "ZGRPREIS1": "-3.50",
            "ZGRPREIS2": "0.00",
        },
        # überall, in jeder Größe gleich
        {
            "ZBEZEICH": "Extra_Garnelen",
            "WRGSHOWALL": "T",
            "WRGSHOW": "",
            "ZPREIGRP3": "C",
            "ZGRPREIS1": "0.00",
            "ZGRPREIS2": "0.00",
        },
        # gelöscht: nie angeboten
        {"ZBEZEICH": "Ananas", "WRGSHOWALL": "T", "ZPREIGRP3": "C"},
    ],
    deleted=(2,),
)


def run(rows, deleted=(), zutaten=ZUTATEN, **kw):
    kw.setdefault("skip_groups", ("015",))
    return convert(table(rows, deleted), WARENGRP, zutaten, ZUTGRP, **kw)


def by_number(rows, number):
    return [r for r in rows if r["number"] == number]


def test_suppe_klein_und_gross_als_pflichtgruppe():
    """Maske der Kasse: klein 6,50 = VK1, groß 11,00 = VK1 + GRPREIS2."""
    result = run(
        [
            artikel(
                "3B",
                "Scharfe Suppe",
                "001",
                "7.20",
                "+-23",
                GRPREIS1="0.00",
                GRPREIS2="5.00",
            )
        ]
    )

    assert result.errors == []
    [item] = result.menu
    assert item == {
        "number": "3b",
        "pos_code": "3B",
        "name": "Scharfe Suppe",
        "category": "Suppe",
        "price_eur": "7,20",
        "active": "ja",
    }
    sizes = [o for o in result.options if o["group_name"] == "Größe"]
    assert [
        (o["option_name"], o["price_delta_eur"], o["is_default"]) for o in sizes
    ] == [
        ("klein", "0,00", "ja"),
        ("groß", "5,00", "nein"),
    ]
    assert {o["required"] for o in sizes} == {"ja"}


def test_eine_groesse_keine_gruppe_extras_nach_warengruppe():
    result = run([artikel("25A", "Ente Thai Curry")])

    options = by_number(result.options, "25a")
    assert [
        (o["group_name"], o["option_name"], o["price_delta_eur"]) for o in options
    ] == [
        ("Extras", "Mango Curry", "3,50"),
        ("Extras", "Extra Garnelen", "1,20"),
    ]
    assert {(o["required"], o["is_default"]) for o in options} == {("nein", "nein")}


def test_ohne_plus_keine_extras_und_andere_warengruppe_ohne_sosse():
    result = run([artikel("7", "Reis", groesse="1"), artikel("1", "Miso", "001")])

    assert by_number(result.options, "7") == []
    assert [o["option_name"] for o in by_number(result.options, "1")] == [
        "Extra Garnelen"
    ]


def test_extra_mit_preis_je_groesse_verschieden_wird_nicht_uebernommen():
    """Unsere Option hat einen Preis je Gericht; Mango kostet in klein 0, in groß 3,50."""
    result = run(
        [artikel("40", "Curry", groesse="+-23", GRPREIS1="0.00", GRPREIS2="4.00")]
    )

    names = [o["option_name"] for o in by_number(result.options, "40")]
    assert "Mango Curry" not in names and "Extra Garnelen" in names
    assert any("Mango Curry" in w and "je Größe" in w for w in result.warnings)


def test_gesperrt_platzhalter_und_getraenke_fallen_raus():
    rows = [
        artikel("000", "", "", ""),
        artikel("35B", "Nudeln Huhn"),
        artikel("35C", "Gestrichen"),
        artikel("65", "Wasser", "015", "3.80"),
    ]

    result = run(rows, deleted=(2,))

    assert [i["pos_code"] for i in result.menu] == ["35B"]
    assert (
        result.skipped_placeholder,
        result.skipped_deleted,
        result.skipped_groups,
    ) == (1, 1, 1)
    assert result.errors == []


def test_vk2_abweichend_ist_fehler_kein_stiller_import():
    row = artikel("30", "Ente", vk1="15.50")
    row["VK2_PREIS"] = "14.50"

    result = run([row, artikel("31", "Huhn")])

    assert [i["number"] for i in result.menu] == ["31"]
    assert any("30" in e and "VK2_PREIS" in e for e in result.errors)


def test_nummern_die_die_suche_nicht_versteht_und_dubletten():
    result = run(
        [
            artikel("S12", "Maki"),
            artikel("25G", "Chop Suey"),
            artikel("35AE", "Kombination"),
            artikel("X12", "Praefix wie Menge"),
            artikel("35b", "A"),
            artikel("35B", "B"),
            artikel("36", "C"),
        ]
    )

    # The search understands S12 and 25G since T-4.12, but not 35AE and X12.
    assert [i["number"] for i in result.menu] == ["s12", "25g", "36"]
    assert any("35AE, X12" in e and "2 Gerichte" in e for e in result.errors)
    assert sum("doppelt" in e for e in result.errors) == 2


def test_groesse_ohne_namen_wird_weggelassen_oder_ist_fehler():
    result = run(
        [
            artikel("20", "Pho", groesse="+-16", GRPREIS5="-3.60"),
            artikel("21", "Nur Größe sechs", groesse="6"),
        ]
    )

    assert [(i["number"], i["price_eur"]) for i in result.menu] == [("20", "13,50")]
    assert not any(o["group_name"] == "Größe" for o in result.options)
    assert any("Größe 6" in w and "20" in w for w in result.warnings)
    assert any("21" in e and "keine Größe mit Namen" in e for e in result.errors)


def test_allergene_nur_ueber_umsetztabelle():
    """l ist Sulfite (O), nicht Sellerie (L); n ist Weichtiere (R), nicht Sesam."""
    result = run(
        [artikel("1", "A", ALLERGENE="ILN"), artikel("2", "B")],
        allergens_confirmed_by="Maxi",
    )

    assert result.allergens == [
        {"number": "1", "allergen_codes": "L,O,R", "confirmed_by": "Maxi"},
        {"number": "2", "allergen_codes": "", "confirmed_by": ""},
    ]
    assert len(ALLERGEN_MAP) == 14 and set(ALLERGEN_MAP.values()) == set(
        "ABCDEFGHLMNOPR"
    )
    assert result.errors == []


def test_allergene_ohne_pruefer_oder_unbekannt_heisst_keine_auskunft():
    result = run([artikel("1", "A", ALLERGENE="AG"), artikel("2", "B", ALLERGENE="AZ")])

    assert {r["allergen_codes"] for r in result.allergens} == {""}
    assert any("1" in e and "geprüft" in e for e in result.errors)
    assert any(
        "2" in e and "unbekannter Allergen-Buchstabe Z" in e for e in result.errors
    )


def test_warnungen_fuer_menschen():
    result = run(
        [
            artikel("32B", "X" * 40, A_PREIS1="7,90"),
            artikel("33", "Nudeln", ZUTATEN="Gebratene_Nudeln, Ei, Sojasoße"),
        ]
    )

    text = result.as_text()
    assert "40 Zeichen" in text and "32B" in text
    assert "Aktionspreis" in text
    assert "Allergenträger" in text and "33" in text
    assert "Gerichte übernommen: 2" in text


def free_from_warnings(result):
    return [w for w in result.warnings if "free from" in w]


def test_free_from_claim_in_a_name_without_allergens_is_listed():
    """The agent reads the name aloud: the claim would be an allergen fact
    that is not a database value (CLAUDE.md rule 1)."""
    result = run(
        [
            artikel("11", "Glutenfreie Nudeln"),
            artikel("12", "Reis ohne Erdnüsse"),
            artikel("13", "Curry mit Tofu"),
            artikel("14", "Vegane Rollen"),
        ]
    )

    [warning] = free_from_warnings(result)
    assert warning.endswith(": 11, 12, 14")
    assert "Warnung: " + warning in result.as_text()
    assert result.errors == []
    # A warning only: the dish is taken over, the agent says "no information".
    assert [r["number"] for r in result.menu] == ["11", "12", "13", "14"]
    assert {r["allergen_codes"] for r in result.allergens} == {""}


@pytest.mark.parametrize(
    "name",
    [
        "Nudeln glutenfrei",
        "Gluten-freie Nudeln",
        "Nudeln Gluten frei",
        "Tofu laktosefrei",
        "Kuchen haselnussfrei",
        "Suppe frei von Sellerie",
        "Reis ohne Ei",
        "Suppe ohne Eiernudeln",
        "Mandelfreier Kuchen",  # LMIV Annex II names the nuts and cereals
        "Cashewfrei",
        "Kuchen ohne Pistazien",
        "Brot dinkelfrei",
        "Gluten Free Roll",  # the menu has English names too
        "Peanut-free roll",
        "Roll without peanuts",
        "Rolle ohne frische Erdnüsse",  # one word may stand in between
        "Suppe ohne Zusatz von Milch",
        "Rolle ohne Garnelen",  # a carrier of one allergen is a claim too
        "Rolle ohne ERDNUESSE",
        "Salat ohne Zwiebeln und Sesam",
        "Curry (vegan)",
        "Veganes Curry",
    ],
)
def test_free_from_claim_spellings(name):
    assert free_from_warnings(run([artikel("7", name)]))


@pytest.mark.parametrize(
    "name",
    [
        "Bohnen Erdnusssoße",  # "ohne" only inside "Bohnen"
        "Ei vom Freilandhuhn",  # "frei" only inside another word
        "Freilandei",
        "Alkoholfreies Bier",  # free from something that is no allergen
        "Ente ohne Knochen",
        "Reis ohne Eis",  # "Ei" counts as a whole word only
        "Kuchen ohne Feier",  # "Eier" counts at the start of a word only
        "Nudeln ohne Zwiebeln mit Erdnusssoße",  # the claim ends at "mit"
        "Erdnuss Curry mit Sesam",  # names an allergen, claims nothing
        "Reis mit Ei frei wählbar",  # "frei" in the middle of a name is no claim
        "Milchreis frei Haus",
        "Curry (vegan möglich)",  # a variant on offer, not a claim about the dish
        "Reis nicht vegan",
        "Nudeln mit oder ohne Ei",
        "Curry ohne Kokosmilch",  # looks like an allergen, is none
        "Curry ohne Kokosnuss",
        "Suppe ohne Buchweizen",
        "Ente ohne Knochen in Erdnusssoße",
        "Ente ohne Haut Knochen Erdnuss",  # more than one word in between
        "Tori no Karaage Sesam",  # "no" only counts directly before the allergen
        "Suppe laut Karte frei von",  # cut off at 40 characters by the register
        "Reis ohne",
        "Suppe ohne Zwiebeln und",
    ],
)
def test_word_in_another_sense_is_no_free_from_claim(name):
    assert free_from_warnings(run([artikel("7", name)])) == []


def test_free_from_claim_with_maintained_allergens_gives_no_warning():
    rows = [artikel("7", "Glutenfreie Nudeln", ALLERGENE="CF")]

    confirmed = run(rows, allergens_confirmed_by="Maxi")
    assert confirmed.allergens[0]["allergen_codes"] == "C,F"
    assert free_from_warnings(confirmed) == []

    # Letters in the register, but nobody confirmed them: the row stays empty,
    # so the name is still the only allergen statement the caller gets.
    assert free_from_warnings(run(rows))


def contradictions(result):
    return [w for w in result.warnings if "contradicts" in w]


@pytest.mark.parametrize(
    ("name", "register", "expected"),
    [
        ("Glutenfreie Nudeln", "AC", "7 (a -> A)"),
        # Register k is Sesam (N), register n would be Weichtiere: both letters.
        ("Rolle ohne Ei und Sesam", "CFK", "7 (c -> C, k -> N)"),
        ("Vegane Rolle", "AG", "7 (g -> G)"),
        # A hyphen left open shares the "-frei" of the last word.
        ("Gluten- und laktosefreie Nudeln", "A", "7 (a -> A)"),
        ("Gluten- und Laktose-frei", "A", "7 (a -> A)"),
        ("Gluten- und Laktose frei", "A", "7 (a -> A)"),
        ("Gluten-, ei- oder sojafrei", "CF", "7 (c -> C, f -> F)"),
        ("Rolle ohne Ei- und Milchprodukte", "C", "7 (c -> C)"),
        # A list goes on over a comma or a slash, also behind "frei von".
        ("Reis ohne Ei, Milch und Nüsse", "GH", "7 (g -> G, h -> H)"),
        ("Rolle ohne Ei/Milch", "G", "7 (g -> G)"),
        ("Suppe frei von Gluten und Milch", "G", "7 (g -> G)"),
        ("Gluten Free Roll", "A", "7 (a -> A)"),
        ("Roll without peanuts", "E", "7 (e -> E)"),
    ],
)
def test_free_from_claim_contradicting_the_register_is_listed(name, register, expected):
    """Codex PR #175: the agent would read "glutenfrei" aloud and
    get_item_details would say the dish contains gluten."""
    result = run([artikel("7", name, ALLERGENE=register)], allergens_confirmed_by="M")

    [warning] = free_from_warnings(result)
    assert "contradicts" in warning and warning.endswith(": " + expected)
    assert result.errors == []
    assert result.allergens[0]["allergen_codes"]


@pytest.mark.parametrize(
    ("name", "register"),
    [
        ("Glutenfreie Nudeln", "CF"),
        ("Erdnussfreie Rolle", "H"),  # "Erdnuss" is no "Nuss": E claimed, H kept
        ("Rolle ohne Nüsse", "E"),
        ("Vegane Rolle", "AF"),
        ("Reis mit Ei und laktosefreie Sosse", "C"),  # no open hyphen: Ei is in
        # "frei von": the word in front is the dish, not the claim.
        ("Fischsuppe frei von Gluten", "D"),
        ("Sesamsoße, frei von Gluten", "K"),
        ("Reis mit Ei frei wählbar", "C"),
        ("Milchreis frei Haus", "G"),
        ("Rolle ohne Tintenfisch", "D"),  # Weichtiere (R), not Fisch (D)
        ("Curry (vegan möglich)", "C"),
        ("Nudeln mit oder ohne Ei", "C"),
        ("Curry ohne Kokosmilch", "G"),
        ("Curry ohne Kokosnuss", "H"),
        ("Suppe ohne Buchweizen", "A"),
    ],
)
def test_free_from_claim_matching_confirmed_allergens_gives_no_warning(name, register):
    result = run([artikel("7", name, ALLERGENE=register)], allergens_confirmed_by="M")

    assert free_from_warnings(result) == []


@pytest.mark.parametrize(
    ("name", "register", "expected"),
    [
        ("Laktosefreier Käse", "G", "7 (g -> G)"),  # still carries the milk allergen
        ("Weizenfreies Brot", "A", "7 (a -> A)"),  # may hold barley
        ("Mandelfreier Kuchen", "H", "7 (h -> H)"),  # may hold cashews
        ("Rolle haselnussfrei", "H", "7 (h -> H)"),
        ("Rolle ohne Tintenfisch", "N", "7 (n -> R)"),  # may hold mussels
        ("Rolle ohne Garnelen", "B", "7 (b -> B)"),
    ],
)
def test_claim_for_one_kind_of_an_allergen_is_to_check_not_a_contradiction(
    name, register, expected
):
    """Both can be right, so nobody is told to correct the allergens."""
    result = run([artikel("7", name, ALLERGENE=register)], allergens_confirmed_by="M")

    [warning] = free_from_warnings(result)
    assert "one kind" in warning and warning.endswith(": " + expected)
    assert contradictions(result) == []


def test_free_from_claim_is_compared_with_unconfirmed_register_letters():
    """Without a checker the row stays empty, but the register already
    contradicts the name: both are reported."""
    result = run([artikel("7", "Glutenfreie Nudeln", ALLERGENE="A")])

    unbacked, contradicted = free_from_warnings(result)
    assert "no allergens are maintained" in unbacked and unbacked.endswith(": 7")
    assert "contradicts" in contradicted and contradicted.endswith(": 7 (a -> A)")


def test_free_from_claim_in_an_extra_or_a_category_is_listed():
    """The agent offers extras and names categories; no allergen row backs them."""
    warengrp = table(
        [
            {"W_WRG": "006", "W_BEZEICH": "Vegan"},
            {"W_WRG": "001", "W_BEZEICH": "Suppe"},
        ]
    )
    zutaten = table(
        [
            {
                "ZBEZEICH": "A.Ohne_Erdnuss",
                "WRGSHOWALL": "T",
                "WRGSHOW": "",
                "ZPREIGRP3": "C",
            },
            {"ZBEZEICH": "Ohne_Fleisch", "WRGSHOWALL": "T", "ZPREIGRP3": "C"},
        ]
    )
    rows = [artikel("7", "Curry"), artikel("8", "Pho", wrg="001")]

    result = convert(table(rows), warengrp, zutaten, ZUTGRP)

    assert sorted(w.split(":")[0] for w in free_from_warnings(result)) == [
        "Warengruppe „Vegan“",
        "Zutat „A.Ohne_Erdnuss“",
    ]


def test_allergen_carrier_in_a_recipe_with_umlaut():
    result = run([artikel("7", "Reis", ZUTATEN="Reis, Erdnüsse")])

    assert any("Allergenträger" in w and w.endswith(": 7") for w in result.warnings)


def test_free_from_words_cover_every_lmiv_allergen():
    assert set(ALLERGEN_NAMES) == set(ALLERGEN_MAP.values())
    assert all(ALLERGEN_NAMES.values())
    assert set(ALLERGEN_KINDS) <= set(ALLERGEN_NAMES)


def test_zutat_mit_unbekannter_preisstufe_und_abgeschnittenem_namen():
    zutaten = table(
        [
            {
                "ZBEZEICH": "Nudeln_statt_Rei",
                "WRGSHOWALL": "T",
                "WRGSHOW": "",
                "ZPREIGRP3": "U",
            },
            {"ZBEZEICH": "Tofu", "WRGSHOWALL": "T", "WRGSHOW": "", "ZPREIGRP3": "X"},
        ]
    )

    result = run([artikel("50", "Reis")], zutaten=zutaten)

    assert [o["option_name"] for o in result.options] == ["Nudeln statt Rei"]
    assert any("Tofu" in e and "ZPREIGRP3" in e for e in result.errors)
    assert any("abgeschnitten" in w for w in result.warnings)


def test_ausgabe_besteht_die_pruefung_des_imports():
    result = run(
        [
            artikel(
                "3B",
                "Scharfe Suppe",
                "001",
                "7.20",
                "+-23",
                GRPREIS1="0.00",
                GRPREIS2="5.00",
            ),
            artikel("25A", "Ente Thai Curry", ALLERGENE="K"),
        ],
        allergens_confirmed_by="Maxi",
    )

    files = result.csv_files()
    plan = parse({**files, "item_aliases.csv": None})

    assert plan.ok, plan.errors
    assert plan.items["3b"].pos_code == "3B" and plan.items["3b"].price_cents == 720
    assert plan.allergens["25a"].codes == ("N",)
    assert set(files) == {MENU_FILE, OPTIONS_FILE, ALLERGENS_FILE}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("6.50", 650),
        ("  13.5", 1350),
        ("-12.50", -1250),
        ("7,90", 790),
        ("", 0),
        ("0", 0),
        ("abc", None),
        ("-", None),
        ("1.234", None),
    ],
)
def test_cents(value, expected):
    assert cents(value) == expected


def test_extra_name():
    assert extra_name("F.Erdnuss_Sauce") == "Erdnuss Sauce"
    assert extra_name("Extra_Ente") == "Extra Ente"
    assert extra_name("E.Sweet-Sour") == "Sweet-Sour"


def test_preisstufe_ohne_preis_ist_fehler_nicht_gratis():
    """Review T-4.11: leerer ZPREIS wäre sonst ein kostenloses Extra (Regel 1)."""
    zutgrp = table([{"ZGRP": "V", "ZPREIS": "", "ZGRP3": "V"}])
    zutaten = table(
        [{"ZBEZEICH": "Tofu", "WRGSHOWALL": "T", "WRGSHOW": "", "ZPREIGRP3": "V"}]
    )

    result = convert(table([artikel("50", "Reis")]), WARENGRP, zutaten, zutgrp)

    assert result.options == []
    assert any("Tofu" in e and "Preis" in e for e in result.errors)


def test_fehlende_spalte_ist_formatfehler():
    """Codex PR #149: falsche Tabelle oder andere Kassenversion -> DbfError, kein KeyError."""
    ohne_preis = table([{"ARTNR": "1", "BEZEICH": "A", "WRG": "006", "GROESSE": "1"}])

    with pytest.raises(DbfError, match=r"artikel.*VK1_PREIS"):
        convert(ohne_preis, WARENGRP, ZUTATEN, ZUTGRP)
    with pytest.raises(DbfError, match=r"zutgrp.*ZPREIS"):
        convert(table([artikel("1", "A")]), WARENGRP, ZUTATEN, table([{"ZGRP": "C"}]))


def test_extras_doppelt_nach_dem_schluessel_des_imports():
    """Codex PR #149: "Extra__Ente" und "Extra_Ente" sind für den Import dieselbe Option."""
    zutaten = table(
        [
            {
                "ZBEZEICH": "Extra__Ente",
                "WRGSHOWALL": "T",
                "WRGSHOW": "",
                "ZPREIGRP3": "U",
            },
            {
                "ZBEZEICH": "Extra_Ente",
                "WRGSHOWALL": "T",
                "WRGSHOW": "",
                "ZPREIGRP3": "U",
            },
        ]
    )

    result = run([artikel("50", "Reis")], zutaten=zutaten)

    assert len(result.options) == 1
    assert any("doppelt" in w for w in result.warnings)
    assert parse({**result.csv_files(), "item_aliases.csv": None}).ok


def test_doppelte_preisstufe_oder_warengruppe_ist_formatfehler():
    """Codex PR #149: zwei aktive Zeilen mit demselben Schlüssel und anderem Wert
    sind kein Preis, den der Umwandler per Zeilenreihenfolge wählen darf."""
    zutgrp = table(
        [
            {"ZGRP": "U", "ZPREIS": "3.50", "ZGRP3": "U"},
            {"ZGRP": "U", "ZPREIS": "3.90", "ZGRP3": "U"},
        ]
    )
    with pytest.raises(DbfError, match=r"zutgrp.*U"):
        convert(table([artikel("1", "A")]), WARENGRP, ZUTATEN, zutgrp)

    warengrp = table(
        [
            {"W_WRG": "006", "W_BEZEICH": "Hauptspeisen"},
            {"W_WRG": "006", "W_BEZEICH": "Wok"},
        ]
    )
    with pytest.raises(DbfError, match=r"warengrp.*006"):
        convert(table([artikel("1", "A")]), warengrp, ZUTATEN, ZUTGRP)

    gleich = table([{"ZGRP": "C", "ZPREIS": "1.20", "ZGRP3": "C"}] * 2)
    assert convert(table([artikel("1", "A")]), WARENGRP, ZUTATEN, gleich).menu


def test_extra_mit_abzug_bleibt_erhalten():
    """Codex PR #149: die Kasse kennt Extras mit Abzug ("ohne Fleisch"); der
    negative Wert kommt in die Karte, nicht weg."""
    zutgrp = table([{"ZGRP": "1", "ZPREIS": "0.10", "ZGRP3": "1"}])
    zutaten = table(
        [{"ZBEZEICH": "Ohne_Fleisch", "WRGSHOWALL": "T", "WRGSHOW": "",
          "ZPREIGRP3": "1", "ZGRPREIS1": "-1.10"}]
    )  # fmt: skip

    result = convert(
        table([artikel("50", "Suppe", groesse="+-2", GRPREIS1="0.00")]),
        WARENGRP,
        zutaten,
        zutgrp,
    )

    assert [(o["option_name"], o["price_delta_eur"]) for o in result.options] == [
        ("Ohne Fleisch", "-1,00")
    ]
    assert parse({**result.csv_files(), "item_aliases.csv": None}).ok


def test_groesse_mit_nicht_ascii_ziffer():
    """Codex PR #149: "²" ist für str.isdigit() eine Ziffer, für int() nicht."""
    result = run([artikel("20", "Pho", groesse="+-1²")])

    assert [i["number"] for i in result.menu] == ["20"]


def test_extras_gleicher_name_verschiedener_preis_ist_fehler():
    """Codex PR #149: zwei Zutaten, die für den Import dieselbe Option sind,
    aber verschieden kosten - die Zeilenreihenfolge entscheidet keinen Preis."""
    zutaten = table(
        [
            {
                "ZBEZEICH": "Extra__Ente",
                "WRGSHOWALL": "T",
                "WRGSHOW": "",
                "ZPREIGRP3": "U",
            },
            {
                "ZBEZEICH": "Extra_Ente",
                "WRGSHOWALL": "T",
                "WRGSHOW": "",
                "ZPREIGRP3": "C",
            },
        ]
    )

    result = run([artikel("50", "Reis")], zutaten=zutaten)

    assert result.options == []
    assert any("Extra Ente" in e and "verschieden" in e for e in result.errors)


def test_pruefer_nur_aus_leerzeichen_zaehlt_nicht():
    """Codex PR #149: " " ist kein Prüfer; der Import würde die Zeile ablehnen."""
    result = run([artikel("1", "A", ALLERGENE="AG")], allergens_confirmed_by="  ")

    assert result.allergens[0]["allergen_codes"] == ""
    assert any("geprüft" in e for e in result.errors)


def test_groesse_ohne_aufschlag_ist_fehler_nicht_gratis():
    """Review T-4.11: leerer GRPREIS einer verkauften Größe ist kein Aufschlag 0."""
    result = run([artikel("12", "Suppe", groesse="13", GRPREIS1="0.00")])

    assert result.menu == []
    assert any("Größe groß" in e for e in result.errors)


def test_extra_mit_unlesbarem_aufschlag_ist_fehler():
    zutaten = table(
        [
            {
                "ZBEZEICH": "Extra_Ei",
                "WRGSHOWALL": "T",
                "WRGSHOW": "",
                "ZPREIGRP3": "C",
                "ZGRPREIS1": "1.5.0",
            }
        ]
    )
    rows = [artikel("12", "Suppe", groesse="+12", GRPREIS1="1.00")]

    result = run(rows, zutaten=zutaten)

    assert by_number(result.options, "12")[-1]["group_name"] == "Größe"
    assert any("Extra_Ei" in e or "Extra Ei" in e for e in result.errors)
    assert not any("kostet je Größe" in w for w in result.warnings)


@pytest.mark.parametrize("wert", ["T", "t", "Y", "y"])
def test_wrgshowall_alle_dbase_schreibweisen(wert):
    zutaten = table(
        [{"ZBEZEICH": "Extra_Ei", "WRGSHOWALL": wert, "WRGSHOW": "", "ZPREIGRP3": "C"}]
    )

    result = run([artikel("12", "Suppe", groesse="+1")], zutaten=zutaten)

    assert [o["option_name"] for o in by_number(result.options, "12")] == ["Extra Ei"]


def test_zutat_ohne_warengruppe_wird_gemeldet():
    zutaten = table(
        [{"ZBEZEICH": "Extra_Ei", "WRGSHOWALL": "F", "WRGSHOW": "", "ZPREIGRP3": "C"}]
    )

    result = run([artikel("12", "Suppe", groesse="+1")], zutaten=zutaten)

    assert any("Extra_Ei" in w and "Warengruppe" in w for w in result.warnings)


def test_vk2_null_heisst_nicht_gepflegt():
    result = run([artikel("12", "Suppe", VK2_PREIS="0.00")])

    assert [r["number"] for r in result.menu] == ["12"]


def test_allergene_ohne_pruefer_sagt_dass_der_import_loescht():
    result = run([artikel("12", "Suppe", ALLERGENE="ac")])

    assert any("löscht" in e for e in result.errors)
