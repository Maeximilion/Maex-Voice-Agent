"""domain/menu/pos_convert: Kasse -> CSV nach docs/14 (T-4.11). Nur synthetische Daten."""

import pytest

from api.domain.menu.importer import ALLERGENS_FILE, MENU_FILE, OPTIONS_FILE, parse
from api.domain.menu.pos_convert import ALLERGEN_MAP, cents, convert, extra_name
from api.domain.menu.pos_dbf import Field, Table


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
        },
        # überall, in jeder Größe gleich
        {
            "ZBEZEICH": "Extra_Garnelen",
            "WRGSHOWALL": "T",
            "WRGSHOW": "",
            "ZPREIGRP3": "C",
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
    result = run([artikel("40", "Curry", groesse="+-23", GRPREIS2="4.00")])

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
            artikel("35b", "A"),
            artikel("35B", "B"),
            artikel("36", "C"),
        ]
    )

    assert [i["number"] for i in result.menu] == ["36"]
    assert any("S12, 25G" in e and "2 Gerichte" in e for e in result.errors)
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


def test_zutat_mit_unbekannter_preisstufe_und_abgeschnittenem_namen():
    zutaten = table(
        [
            {"ZBEZEICH": "Nudeln_statt_Rei", "WRGSHOWALL": "T", "ZPREIGRP3": "U"},
            {"ZBEZEICH": "Tofu", "WRGSHOWALL": "T", "ZPREIGRP3": "X"},
        ]
    )

    result = run([artikel("50", "Reis")], zutaten=zutaten)

    assert [o["option_name"] for o in result.options] == ["Nudeln statt Rei"]
    assert any("Tofu" in e and "ZPREIGRP3" in e for e in result.errors)
    assert any("abgeschnitten" in w for w in result.warnings)


def test_ausgabe_besteht_die_pruefung_des_imports():
    result = run(
        [
            artikel("3B", "Scharfe Suppe", "001", "7.20", "+-23", GRPREIS2="5.00"),
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
