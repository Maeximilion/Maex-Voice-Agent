"""domain/menu/numberwords.py: deutsche Zahlwörter und Mengen (docs/11 §menu).

Der Rundlauf über 0 bis 199 und hundert weitere Werte benutzt einen eigenen,
unabhängig geschriebenen Sprecher (`sprich`). Er ist bewusst eine zweite
Implementierung: zwei getrennte Wege müssen zum selben Ergebnis kommen, sonst
prüfte der Test nur sich selbst. Dazu kommen die Fälle, die am Telefon wirklich
schiefgehen -- auseinandergeschriebene Zahlen, Ziffernfolgen, Artikel, Mengen
ohne Marker.
"""

import pytest

from api.domain.menu.numberwords import (
    MAX_VALUE,
    find_item_number,
    find_numbers,
    find_quantity,
    parse_cardinal,
)

EINER = [
    "null",
    "eins",
    "zwei",
    "drei",
    "vier",
    "fuenf",
    "sechs",
    "sieben",
    "acht",
    "neun",
]
ZEHNER = {
    2: "zwanzig",
    3: "dreissig",
    4: "vierzig",
    5: "fuenfzig",
    6: "sechzig",
    7: "siebzig",
    8: "achtzig",
    9: "neunzig",
}
TEENS = {
    10: "zehn",
    11: "elf",
    12: "zwoelf",
    13: "dreizehn",
    14: "vierzehn",
    15: "fuenfzehn",
    16: "sechzehn",
    17: "siebzehn",
    18: "achtzehn",
    19: "neunzehn",
}


def sprich(n: int) -> str:
    """Deutsche Schreibweise einer Zahl bis 999, unabhängig vom Modul unter Test."""
    if n < 10:
        return EINER[n]
    if n < 20:
        return TEENS[n]
    if n < 100:
        zehner, einer = divmod(n, 10)
        if einer == 0:
            return ZEHNER[zehner]
        return f"{'ein' if einer == 1 else EINER[einer]}und{ZEHNER[zehner]}"
    hunderter, rest = divmod(n, 100)
    kopf = f"{'ein' if hunderter == 1 else EINER[hunderter]}hundert"
    return kopf if rest == 0 else kopf + sprich(rest)


@pytest.mark.parametrize("zahl", range(200))
def test_rundlauf_null_bis_neunundneunzig_und_darueber(zahl):
    assert parse_cardinal(sprich(zahl)) == zahl


@pytest.mark.parametrize("zahl", range(200, MAX_VALUE + 1, 7))
def test_rundlauf_hunderter(zahl):
    assert parse_cardinal(sprich(zahl)) == zahl


@pytest.mark.parametrize("zahl", [0, 1, 7, 23, 47, 99, 100, 250, MAX_VALUE])
def test_ziffern_gelten_genauso(zahl):
    assert parse_cardinal(str(zahl)) == zahl


@pytest.mark.parametrize(
    ("text", "erwartet"),
    [
        ("fünf", 5),
        ("zwölf", 12),
        ("dreißig", 30),
        ("fünfundfünfzig", 55),
        ("Fünf", 5),
        ("DREIUNDZWANZIG", 23),
    ],
)
def test_umlaute_in_beiden_schreibweisen(text, erwartet):
    """Ob die Erkennung "fünf" oder "fuenf" liefert, ist Zufall."""
    assert parse_cardinal(text) == erwartet


@pytest.mark.parametrize(
    ("text", "erwartet"),
    [
        ("drei und zwanzig", 23),
        ("ein und zwanzig", 21),
        ("vierzig sieben", 47),
        ("zwei hundert", 200),
        ("zweihundert dreiundzwanzig", 223),
        ("sechszehn", 16),
        ("siebenzehn", 17),
        ("zwo", 2),
    ],
)
def test_formen_die_die_erkennung_liefert(text, erwartet):
    assert parse_cardinal(text) == erwartet


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "bitte",
        "dreiundzwanzig bitte",
        "die dreiundzwanzig",
        "tausend",
        "zwei drei",
    ],
)
def test_kein_treffer_ergibt_none(text):
    """Nie raten (CLAUDE.md §2 Regel 2): "zwei drei" ist zwei und drei, nicht 23."""
    assert parse_cardinal(text) is None


def test_ueber_der_obergrenze_gilt_nicht_mehr_als_zahlwort():
    assert parse_cardinal(str(MAX_VALUE + 1)) is None


@pytest.mark.parametrize(
    ("text", "erwartet"),
    [
        ("Nummer vierzig sieben", 47),
        ("Nr. 23", 23),
        ("die Nummer dreiundzwanzig bitte", 23),
        ("ich haette gern die dreiundzwanzig", 23),
        ("einmal die 23 bitte", 23),
        ("zweimal Nummer 5", 5),
    ],
)
def test_gerichtnummer_im_satz(text, erwartet):
    assert find_item_number(text) == erwartet


@pytest.mark.parametrize(
    "text",
    ["ich haette gern etwas", "zwei und die fuenf", "die 12 oder die 14"],
)
def test_mehrdeutige_oder_fehlende_nummer_ergibt_none(text):
    """Zwei Zahlen ohne Marker: welche das Gericht ist, steht nicht fest."""
    assert find_item_number(text) is None


def test_marker_schlaegt_die_erste_zahl():
    assert find_item_number("zwei Portionen von Nummer dreiundzwanzig") == 23


@pytest.mark.parametrize(
    ("text", "erwartet"),
    [
        ("zweimal die Fruehlingsrollen", 2),
        ("einmal bitte", 1),
        ("2x die 23", 2),
        ("2 x Nummer 5", 2),
        ("drei Portionen", 3),
        ("zwei Stueck", 2),
        ("zwei Stück", 2),
        ("dreimal", 3),
        ("zwanzigmal", 20),
    ],
)
def test_menge_mit_marker(text, erwartet):
    assert find_quantity(text) == erwartet


@pytest.mark.parametrize(
    "text",
    ["die dreiundzwanzig", "Nummer 23", "ich haette gern die 5", ""],
)
def test_menge_ohne_marker_ergibt_none(text):
    """Eine nackte Zahl ist ein Gericht, keine Menge. Die Voreinstellung setzt
    der Aufrufer, nicht dieses Modul."""
    assert find_quantity(text) is None


@pytest.mark.parametrize(
    ("text", "erwartet"),
    [
        ("ein Tisch fuer vier Personen", [4]),
        ("eine Frage bitte", []),
        ("zwei drei", [2, 3]),
        ("die 12 oder die 14", [12, 14]),
        # "einmal"/"zweimal" sind Mengen, keine genannten Zahlen - dafuer find_quantity
        ("einmal 23 und zweimal 47", [23, 47]),
        ("nichts davon", []),
    ],
)
def test_alle_zahlen_im_satz(text, erwartet):
    """Der bloße Artikel zaehlt nicht mit, sonst faende sich in fast jedem Satz
    eine Eins."""
    assert find_numbers(text) == erwartet
