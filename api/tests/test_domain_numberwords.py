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


# --- Codex-Review PR #105 ------------------------------------------------


@pytest.mark.parametrize(
    "text",
    ["Nummer 20, eine Portion", "Nummer 20 eine Portion", "die 40, zwei Portionen"],
)
def test_zahl_waechst_nicht_ueber_die_menge_hinweg(text):
    """P1: "Nummer 20, eine Portion" ergab 21 - die Regel fuer ziffernweise
    gesprochene Zahlen ("vierzig sieben") griff ueber das Komma und ueber den
    Artikel der Mengenangabe hinweg. Falsches Gericht und absurde Menge."""
    nummer = find_item_number(text)
    menge = find_quantity(text)

    assert nummer in (20, 40)
    assert menge in (1, 2)
    assert nummer != menge


@pytest.mark.parametrize(
    ("text", "erwartet"),
    [
        ("die ein und zwanzig", 21),
        ("ich haette gern die ein und dreissig", 31),
    ],
)
def test_artikel_der_eine_zahl_beginnt_zaehlt_mit(text, erwartet):
    """P1: "ein" wurde als Artikel verworfen, bevor jemand geprueft hat, ob es
    eine Zahl beginnt - "die ein und zwanzig" wurde so zur 20."""
    assert find_item_number(text) == erwartet
    assert find_numbers(text) == [erwartet]


@pytest.mark.parametrize(
    ("text", "nummer", "menge"),
    [
        ("2 x die 23", 23, 2),
        ("drei Portionen von der 23", 23, 3),
        ("zweimal die dreiundzwanzig", 23, 2),
    ],
)
def test_eine_menge_macht_die_bestellung_nicht_mehrdeutig(text, nummer, menge):
    """P2: die Menge zaehlte als zweite Zahl, also galt die Bestellung als
    mehrdeutig und der Gast wurde ohne Not zurueckgefragt."""
    assert find_item_number(text) == nummer
    assert find_quantity(text) == menge


# --- find_item_number_ref: Wert, Kartenschreibweise und Marker aus einer Stelle ---

from api.domain.menu.numberwords import find_item_number_ref  # noqa: E402


@pytest.mark.parametrize(
    ("text", "value", "card", "marked"),
    [
        ("Nummer 23a", 23, "23a", True),
        ("Nummer 23 a", 23, "23a", True),
        ("die 23A bitte", 23, "23a", False),
        ("Nummer 07", 7, "07", True),
        ("Nummer acht", 8, "8", True),
        ("Nr. 31b", 31, "31b", True),
        ("23a, nein, Nummer 23", 23, "23", True),
        ("die 23 aber scharf", 23, "23", False),
        ("2 x die 23", 23, "23", False),
    ],
)
def test_item_number_ref(text, value, card, marked):
    ref = find_item_number_ref(text)
    assert (ref.value, ref.text, ref.marked) == (value, card, marked)


@pytest.mark.parametrize(
    "text", ["zwei Frühlingsrollen, Nummer weiß ich nicht", "Nummer", "23 und 47"]
)
def test_item_number_ref_ohne_eindeutige_nummer(text):
    ref = find_item_number_ref(text)
    assert ref is None or not ref.marked


# --- Zwei ausdrueckliche Nummern: keine waehlen (Codex PR #117, P1) ---------------

from api.domain.menu.numberwords import find_marked_item_numbers  # noqa: E402


@pytest.mark.parametrize(
    "text",
    ["Nummer 23, nein, Nummer 24", "Nummer 23 oder Nummer 24", "Nr. 5 und Nr. 6"],
)
def test_zwei_markierte_nummern_ergeben_keine(text):
    assert find_item_number_ref(text) is None


def test_zwei_markierte_nummern_werden_beide_gemeldet():
    refs = find_marked_item_numbers("Nummer 23, nein, Nummer 24a")
    assert [r.text for r in refs] == ["23", "24a"]


def test_dieselbe_nummer_zweimal_ist_eindeutig():
    ref = find_item_number_ref("Nummer 23, ja genau, Nummer 23")
    assert ref is not None and ref.text == "23" and ref.marked


@pytest.mark.parametrize(
    ("text", "card"),
    [
        ("Nummer 23h", "23h"),
        ("Nummer 23ab", "23ab"),
        ("Nummer 23 h", "23h"),
    ],
)
def test_ungueltige_endung_macht_die_nummer_ungueltig(text, card):
    """Codex PR #117: eine unbekannte Endung wird nie abgeschnitten."""
    ref = find_item_number_ref(text)
    assert ref is not None and ref.text == card and ref.valid is False


@pytest.mark.parametrize(
    "text", ["die 23h", "einmal 7up bitte", "das ist 5mg Zucker", "das ist 5g Zucker"]
)
def test_ohne_marker_ist_eine_unsaubere_form_keine_nummer(text):
    """Ohne "Nummer" ist "23h" oder "7up" Text, keine genannte Kartennummer.

    Die 23 darf daraus nie werden - aber die Leiter soll auch nicht nach einer
    Nummer fragen, die der Gast gar nicht genannt hat: search_menu findet den
    Alias "7up" ueber den Namen (Review PR #117).
    """
    assert find_item_number_ref(text) is None
    assert find_item_number(text) is None


@pytest.mark.parametrize(
    "text", ["Nummer 23a", "Nummer 23", "Nummer 23 bitte", "2x die 23"]
)
def test_gueltige_nummern_bleiben_gueltig(text):
    ref = find_item_number_ref(text)
    assert ref is not None and ref.valid is True


def test_gleiche_nummer_in_zwei_schreibweisen_ist_eine():
    """Codex PR #117: "Nummer 07 oder Nummer 7" ist dieselbe Nummer."""
    refs = find_marked_item_numbers("Nummer 07 oder Nummer 7")
    assert len(refs) == 1
    assert find_item_number_ref("Nummer 07 oder Nummer 7") is not None


@pytest.mark.parametrize(
    ("text", "cards"),
    [
        ("Nummer 23 oder 24", ["23", "24"]),
        ("Nummer 23, nein 24", ["23", "24"]),
        ("Nummer 23, nein, vierundzwanzig", ["23", "24"]),
    ],
)
def test_ein_marker_mit_alternative_ergibt_beide(text, cards):
    """Codex PR #117: die zweite Zahl hinter einem einzigen "Nummer" nicht verlieren."""
    assert [r.text for r in find_marked_item_numbers(text)] == cards
    assert find_item_number_ref(text) is None


@pytest.mark.parametrize(
    "text",
    [
        "zwei Nummer 23",
        "Nummer 23 zweimal",
        "Nummer 23, zwei Portionen",
        "Nummer 23 bitte",
    ],
)
def test_menge_neben_markierter_nummer_bleibt_eindeutig(text):
    ref = find_item_number_ref(text)
    assert ref is not None and ref.text == "23"


def test_x_an_der_markierten_nummer_ist_keine_endung():
    """Codex PR #117: "Nummer 23x" ist keine Kartennummer - ungueltig statt still 23."""
    ref = find_item_number_ref("Nummer 23x")
    assert ref is not None and ref.text == "23x" and ref.valid is False


def test_x_ohne_marker_ist_menge_und_keine_nummer():
    """ "die 23x" ohne Marker: 23 mal - aber welches Gericht? Keine Nummer."""
    assert find_item_number_ref("die 23x") is None


def test_x_als_menge_bleibt_menge():
    ref = find_item_number_ref("2x die 23")
    assert ref is not None and ref.text == "23" and ref.valid is True


def test_abgesetztes_x_nach_markierter_nummer_ist_ungueltig():
    """Codex PR #117: "Nummer 23 x" ist nicht die 23."""
    ref = find_item_number_ref("Nummer 23 x")
    assert ref is not None and ref.text == "23x" and ref.valid is False


@pytest.mark.parametrize(
    "text",
    ["Nummer 23 mit 2 Soßen", "Nummer 23 um 12 Uhr", "Nummer 23 für 4 Personen"],
)
def test_zahl_ohne_korrekturwort_ist_keine_alternative(text):
    """Codex PR #117: nur "oder", "nein" & Co. machen eine zweite Zahl zur Wahl."""
    ref = find_item_number_ref(text)
    assert ref is not None and ref.text == "23" and ref.valid is True


@pytest.mark.parametrize(
    "text",
    [
        "Nummer 23 oder 24",
        "Nummer 23, nein 24",
        "Nummer 23 bzw. 24",
        "Nummer 23, lieber 24",
    ],
)
def test_korrekturwort_macht_die_zweite_zahl_zur_alternative(text):
    assert [r.text for r in find_marked_item_numbers(text)] == ["23", "24"]


@pytest.mark.parametrize(
    ("text", "cards"),
    [("Nummer zwei drei", ["2", "3"]), ("Nummer 23 24", ["23", "24"])],
)
def test_direkt_folgende_zahl_ist_alternative(text, cards):
    """Codex PR #117: zwei Zahlen direkt hintereinander - nicht die erste nehmen."""
    assert [r.text for r in find_marked_item_numbers(text)] == cards
    assert find_item_number_ref(text) is None


@pytest.mark.parametrize(
    "text", ["Nummer 23 mit 2 oder 3 Soßen", "Nummer 23 mit 2 Soßen oder 3 Dips"]
)
def test_korrekturwort_zwischen_anderen_zahlen_verbindet_nicht(text):
    """Codex PR #117: das "oder" gehoert zu den Sossen, nicht zur 23."""
    ref = find_item_number_ref(text)
    assert ref is not None and ref.text == "23"


@pytest.mark.parametrize(
    ("text", "cards"),
    [("Nummer 23 und 24", ["23", "24"]), ("Nummer 23, 24", ["23", "24"])],
)
def test_aufzaehlung_hinter_einem_marker(text, cards):
    """Codex PR #117: eine zweite Nummer per "und" oder Komma nie verschlucken."""
    assert [r.text for r in find_marked_item_numbers(text)] == cards
    assert find_item_number_ref(text) is None


def test_zusammengesetzte_zahl_bleibt_eine():
    ref = find_item_number_ref("Nummer drei und zwanzig")
    assert ref is not None and ref.text == "23"


@pytest.mark.parametrize("text", ["Nummer 1000 und 23", "Nummer 1000", "Nr. 5000, 23"])
def test_markierte_nummer_ausserhalb_des_bereichs_bleibt_ungueltig(text):
    """Codex PR #117: keine spaetere Zahl rueckt nach, wenn die markierte ungueltig ist."""
    ref = find_item_number_ref(text)
    assert (
        ref is not None and ref.valid is False and ref.text.startswith(("1000", "5000"))
    )


@pytest.mark.parametrize(
    "text", ["die Nummer ist 23", "ich meine Nummer die 23", "Nummer war 23"]
)
def test_fuellwort_zwischen_marker_und_zahl(text):
    """Codex PR #117: natuerliche Saetze mit "ist"/"die" hinter "Nummer"."""
    ref = find_item_number_ref(text)
    assert ref is not None and ref.text == "23" and ref.marked


@pytest.mark.parametrize(
    "text", ["Nummer 23, äh, 24", "Nummer 23 ähm 24", "Nummer 23 hm 24"]
)
def test_zoegerlaut_zwischen_zwei_nummern(text):
    """Codex PR #117: eine Pause waehlt nicht still die erste Nummer."""
    assert [r.text for r in find_marked_item_numbers(text)] == ["23", "24"]
    assert find_item_number_ref(text) is None


@pytest.mark.parametrize(
    "text",
    ["Nummer 23 mit Reis oder Nudeln um 18 Uhr", "Nummer 23, und zwar mit 2 Dips"],
)
def test_verbindungswort_muss_direkt_zwischen_den_zahlen_stehen(text):
    """Codex PR #117: ein "oder" irgendwo im Satz verbindet keine spaete Uhrzeit."""
    ref = find_item_number_ref(text)
    assert ref is not None and ref.text == "23"


def test_artikel_zwischen_verbindungswort_und_zahl():
    assert [r.text for r in find_marked_item_numbers("Nummer 23 oder die 24")] == [
        "23",
        "24",
    ]


# --- Regel A: sole_item_number ----------------------------------------------------

from api.domain.menu.numberwords import sole_item_number  # noqa: E402


@pytest.mark.parametrize(
    ("text", "card"),
    [
        ("Nummer 23", "23"),
        ("die 23", "23"),
        ("Nummer 23a", "23a"),
        ("die 23 a", "23a"),
        ("Nummer 07", "07"),
        ("drei Portionen von der 23", "23"),
        ("zwei Nummer 23", "23"),
        ("Nummer 23g", "23g"),
    ],
)
def test_sole_item_number_eindeutig(text, card):
    ref, unclear = sole_item_number(text)
    assert not unclear and ref is not None and ref.text == card


@pytest.mark.parametrize(
    "text",
    [
        "Nummer 23 oder 24",
        "Nummer 47, die Ente",
        "23 und 24",
        "Nummer 1000 und 23",
        # Ohne Marker: "oder" ist ein Verbindungswort, kein Gerichtname. Sonst
        # liefe die Namenssuche auf "oder" und fände ein Gericht wie "Reis oder
        # Nudeln" (Codex PR #117, P1).
        "23 oder 24",
        "23 oder 24 bitte",
        "nein 23 oder 24",
        "23, äh, 24",
    ],
)
def test_sole_item_number_unklar(text):
    ref, unclear = sole_item_number(text)
    assert unclear and ref is None


@pytest.mark.parametrize(
    "text",
    [
        # Marker plus zweite Position: Rueckfrage nach der einen Nummer.
        "ich hätte gern die Nummer 23 und einmal Pho Bo",
        # Marker plus Zusatzwunsch: die Sauce ist eine Option, kein Gericht -
        # Regel A sieht nur, dass neben der Nummer Inhalt steht.
        "Nummer 23 mit Erdnusssauce",
    ],
)
def test_marker_plus_inhalt_ist_eine_rueckfrage(text):
    """Mit „Nummer" daneben und Inhalt im Satz wird gefragt, nicht gewählt."""
    assert sole_item_number(text) == (None, True)


@pytest.mark.parametrize(
    "text",
    [
        "die 23 und einmal Pho Bo",
        "einmal die 23 und zweimal Frühlingsrollen",
    ],
)
def test_ohne_marker_ist_ein_zweiter_teil_kein_nummernsatz(text):
    """Ohne „Nummer" entscheidet die Namenssuche - und verliert dabei die Zahl.

    `(None, False)` heisst: kein Nummernsatz, der Satz geht als Ganzes in die
    Namenssuche. Die genannte 23 taucht danach nirgends mehr auf; ein Satz mit
    zwei Positionen liefert am Ende **eine**. Das ist heute so und steht als
    offener Punkt in `docs/01_STATUS.md`: die Zerlegung je Position gehört vor
    die Suche, in den Bestellfluss (T-4.5).
    """
    assert sole_item_number(text) == (None, False)


def test_zwei_zahlen_ohne_marker_fragen_nach_statt_namen_zu_suchen():
    """ "23 oder 24" darf nicht als Name "oder" in der Suche landen."""
    assert sole_item_number("23 oder 24") == (None, True)


@pytest.mark.parametrize(
    "text",
    [
        # Ein Gerichtname darf "oder" enthalten - ohne Zahl bleibt es ein Name.
        "Reis oder Nudeln",
        # Zahl neben einem Namen: Menge, die Namenssuche entscheidet. Auch mit
        # Verbindungswort - "zwei Cola oder Fanta" ist keine Frage nach einer
        # Nummer, und der Alias "cola oder fanta" muss erreichbar bleiben
        # (Codex PR #117, P2).
        "zwei Cola 0,5",
        "Pizza 4 Jahreszeiten",
        "zwei Cola oder Fanta",
        "die 23 oder Reis",
    ],
)
def test_verbindungswort_im_namen_bleibt_namenssuche(text):
    assert sole_item_number(text) == (None, False)


def test_marker_ueberlebt_fuellwort_und_zoegerlaut():
    """ "Nummer bitte 23, Pho" darf den Marker nicht verlieren.

    Sonst bliebe nach der Normalisierung nur "pho" stehen und der exakte Alias
    könnte stillschweigend ein anderes Gericht liefern, obwohl der Gast eine
    Nummer genannt hat (Codex PR #117, P1).
    """
    for text in ("Nummer bitte 23, Pho", "Nummer äh 23, Pho"):
        assert sole_item_number(text) == (None, True)


def test_fuellwort_verschluckt_die_zahl_nicht():
    """ "ein" eröffnet hier die Zahl und ist kein Füllwort: 21, nicht None."""
    ref, unclear = sole_item_number("Nummer ein und zwanzig")
    assert not unclear and ref is not None and ref.value == 21


@pytest.mark.parametrize(
    "text",
    [
        "Nummer A12",
        "Nummer a12",
        "Nr. C3 bitte",
        "Nummer A 12",
        "Nummer a 12",
        # Mehrere Buchstaben, getrennt wie zusammen (Codex PR #117, P2).
        "Nummer AB12",
        "Nummer AB 12",
    ],
)
def test_buchstabe_vor_der_ziffer_nach_marker_ist_ungueltig(text):
    """ "Nummer A12" ist keine Kartenform (docs/14: Ziffern, dahinter a-f).

    Ohne das liefe die Suche auf den Namen "a12" und ein importierter Alias
    "a12" könnte die genannte Nummer stillschweigend ersetzen, obwohl der
    Importer "A12" als Kartennummer ablehnt (Codex PR #117, P1).
    """
    ref, unclear = sole_item_number(text)
    assert not unclear and ref is not None and not ref.valid


@pytest.mark.parametrize("text", ["die A12", "A12", "7up"])
def test_buchstabe_vor_der_ziffer_ohne_marker_bleibt_name(text):
    """Ohne "Nummer" ist "7up" ein Alias, kein Nummernversuch."""
    assert sole_item_number(text) == (None, False)


@pytest.mark.parametrize(
    "text",
    [
        "Nummer tausend",
        "Nummer tausendzwei",
        # Im Deutschen steht der Faktor davor, "tausend" auch mittendrin
        # (Codex PR #117, P2).
        "Nummer eintausend",
        "Nummer zweitausend",
        "Nummer dreitausendzwei",
    ],
)
def test_zu_grosses_zahlwort_nach_marker_ist_ungueltig(text):
    """ "Nummer tausend" ist eine Nummer, die es nicht gibt - kein Gerichtname.

    Ohne das liefe die Suche auf den Namen "tausend" und könnte ein fremdes
    Gericht liefern (Codex PR #117, P2).
    """
    ref, unclear = sole_item_number(text)
    assert not unclear and ref is not None and not ref.valid


@pytest.mark.parametrize(
    "text",
    [
        # Zurueckgenommen oder nicht zu Ende gesprochen.
        "Nummer 23, nein",
        "Nummer 23 oder",
        "Nummer 23 und",
        "Nein, Nummer 23",
    ],
)
def test_freihaengendes_verbindungswort_fragt_nach(text):
    """Ein Verbindungswort ohne zweite Zahl ist kein Gerichtname.

    "Nummer 23, nein" hat der Gast zurueckgenommen, "Nummer 23 oder" nicht zu
    Ende gesprochen. Durchsichtig ist ein Verbindungswort nur zwischen zwei
    Zahlen (Codex PR #117, P1).
    """
    assert sole_item_number(text) == (None, True)


@pytest.mark.parametrize(
    "text", ["zwei Frühlingsrollen", "die knusprige Ente", "Nummer weiß ich nicht"]
)
def test_sole_item_number_kein_nummernsatz(text):
    assert sole_item_number(text) == (None, False)


# --- Kartenformat aus der Karte: Praefix S/SM, Endung a bis g (T-4.12) ---------

from api.domain.menu.numberwords import (  # noqa: E402
    CardFormat,
    canonical_card,
    reserved_prefix,
)

# Wie die Kasse (Stand 26.09.2026): S1 bis S53 und SM1 bis SM6 in der
# Warengruppe Sushi, dazu Nummern ohne Praefix und 25G.
KARTE = CardFormat.from_items(
    [
        ("s1", "Sushi"),
        ("s12", "Sushi"),
        ("sm1", "Sushi"),
        ("12", "Suppen"),
        ("25", "Hauptspeisen"),
        ("25g", "Hauptspeisen"),
    ]
)


def test_kartenformat_aus_den_nummern():
    """Welche Praefixe gelten, sagt die Karte, nicht der Code (Regel 1)."""
    assert KARTE.prefixes == frozenset({"s", "sm"})
    assert dict(KARTE.words) == {"sushi": ("s", "sm")}
    assert CardFormat().prefixes == frozenset()


def test_kategoriewort_nur_ohne_fremde_praefixe():
    """Traegt ein Praefix zwei Kategorien, ist kein Kategoriewort eindeutig."""
    karte = CardFormat.from_items([("m1", "Menü"), ("m2", "Menü Sushi")])
    assert karte.prefixes == frozenset({"m"})
    assert dict(karte.words) == {}


def test_kategoriewort_nur_ohne_nummern_ohne_praefix():
    """Codex PR #155: steht in der Kategorie auch eine Nummer ohne Praefix,
    gibt es kein Kategoriewort. Sonst faende "Sushi zwölf" die 12 einer
    anderen Kategorie (Suppen), sobald S12 fehlt."""
    karte = CardFormat.from_items([("s1", "Sushi"), ("99", "Sushi"), ("12", "Suppen")])
    assert dict(karte.words) == {}
    ref, _ = sole_item_number("Sushi zwölf", karte)
    assert ref is None or "12" not in ref.cards


@pytest.mark.parametrize(
    ("text", "cards"),
    [
        ("S12", ("s12",)),
        ("s12", ("s12",)),
        ("Nummer S12", ("s12",)),
        ("S 12", ("s12",)),
        ("S zwölf", ("s12",)),
        ("Es zwölf", ("s12",)),
        ("ess zwölf", ("s12",)),
        ("Sushi zwölf", ("s12", "sm12")),
        ("Nummer Sushi zwölf", ("s12", "sm12")),
        ("SM eins", ("sm1",)),
        ("Es Em eins", ("sm1",)),
        ("S M 1", ("sm1",)),
        ("zweimal SM1", ("sm1",)),
        ("zwei S zwölf", ("s12",)),
        ("fünfundzwanzig G", ("25g",)),
        ("25 g", ("25g",)),
        ("25G", ("25g",)),
        ("Nummer 25g", ("25g",)),
        ("die 12", ("12",)),
    ],
)
def test_praefix_und_endung_g(text, cards):
    ref, unclear = sole_item_number(text, KARTE)
    assert not unclear and ref is not None and ref.valid
    assert ref.cards == cards


def test_praefixnummer_behaelt_wert_und_marker():
    ref, _ = sole_item_number("Nummer S zwölf", KARTE)
    assert ref is not None and ref.value == 12 and ref.marked and ref.text == "s12"
    ref, _ = sole_item_number("Sushi zwölf", KARTE)
    assert ref is not None and ref.text == "s12 oder sm12" and not ref.marked


@pytest.mark.parametrize(
    "text",
    ["S12 oder S13", "Nummer S12 oder 13", "Sushi Nummer zwölf", "S12, nein, S13"],
)
def test_praefixnummer_unklar(text):
    assert sole_item_number(text, KARTE) == (None, True)


@pytest.mark.parametrize("text", ["Sushi", "zwei Sushi", "S 12 Stück"])
def test_praefix_ohne_nummernsatz(text):
    """Ohne reine Nummer entscheidet die Namenssuche, wie bei "23 Lachs"."""
    assert sole_item_number(text, KARTE) == (None, False)


@pytest.mark.parametrize("text", ["S12", "S zwölf", "Es zwölf", "Sushi zwölf"])
def test_ohne_praefix_in_der_karte_keine_praefixnummer(text):
    """Eine Karte ohne S kennt kein S12: kein Treffer auf die 12 (Regel 2)."""
    ref, _ = sole_item_number(text)
    assert ref is None or ref.text != "12"


def test_nummer_23g_ist_nie_die_23():
    """Codex PR #117: g ist jetzt Kartenendung, bleibt aber 23g, nie 23."""
    for text in ["Nummer 23g", "die 23 g", "Nummer dreiundzwanzig g"]:
        ref, unclear = sole_item_number(text, KARTE)
        assert not unclear and ref is not None and ref.cards == ("23g",)


def test_unbekanntes_praefix_nach_marker_bleibt_ungueltig():
    ref, _ = sole_item_number("Nummer A12", KARTE)
    assert ref is not None and not ref.valid and ref.text == "a12"


@pytest.mark.parametrize(
    ("card", "canonical"),
    [("07", "7"), ("S07", "s7"), ("sm01", "sm1"), ("0", "0"), ("25G", "25g")],
)
def test_canonical_card_mit_praefix(card, canonical):
    assert canonical_card(card) == canonical


def test_find_item_number_ref_mit_praefix():
    ref = find_item_number_ref("S zwölf", KARTE)
    assert ref is not None and ref.text == "s12" and ref.valid
    # Die blanke Zahl waere Gericht 12, nicht S12 (Review PR #155).
    assert find_item_number("S zwölf", KARTE) is None
    assert find_item_number("Sushi zwölf", KARTE) is None
    assert find_item_number("Nummer 23", KARTE) == 23


# --- Review PR #155 -----------------------------------------------------------------


@pytest.mark.parametrize(
    "text", ["Dann nehme ich es zwei", "ja genau, es zwei", "ich hole es 18 ab"]
)
def test_pronomen_es_ist_kein_praefix(text):
    """ "es" als Pronomen vor einer Zahl ist nicht S (Regel 2)."""
    ref, _ = sole_item_number(text, KARTE)
    assert ref is None or not ref.text.startswith("s")


@pytest.mark.parametrize(
    "text",
    ["Es zwölf", "und Es zwölf", "zweimal Es zwölf", "Nummer es zwölf", "die Es zwölf"],
)
def test_buchstabiertes_es_am_satzanfang_bleibt_s(text):
    ref, unclear = sole_item_number(text, KARTE)
    assert not unclear and ref is not None and ref.cards == ("s12",)


@pytest.mark.parametrize(
    "text",
    ["die 23 S12", "23 S12", "die 23 S zwölf", "2 S12", "S12 Lachs", "Pho Bo S12"],
)
def test_praefixnummer_neben_zahl_oder_name_fragt_nach(text):
    """Eine Zahl vor dem Praefix ist nicht stillschweigend die Menge, und neben
    einem Namen ist eine Praefixnummer keine Menge - also nachfragen."""
    assert sole_item_number(text, KARTE) == (None, True)


@pytest.mark.parametrize("text", ["Nummer n 23", "Nummer, n 23"])
def test_umgangssprachliches_n_ist_kein_praefix(text):
    """'n (einen) hinter dem Marker ist keine Nummer "n23"."""
    assert sole_item_number(text, KARTE) == (None, True)


def test_einheit_g_ohne_marker_ist_keine_nummer_fuer_die_leiter():
    assert find_item_number_ref("die 25g bitte").cards == ("25g",)


def test_kartenformat_ist_hashbar():
    assert hash(KARTE) == hash(
        CardFormat.from_items(
            [
                ("s1", "Sushi"),
                ("sm1", "Sushi"),
                ("s12", "Sushi"),
                ("12", "Suppen"),
                ("25", "Hauptspeisen"),
                ("25g", "Hauptspeisen"),
            ]
        )
    )


def test_canonical_card_leer_ist_null():
    assert canonical_card("") == "0"
    assert canonical_card("000") == "0"


def test_find_item_number_mit_kandidaten_ist_keine_blanke_zahl():
    """Codex PR #155: "Sushi 99" mit den Kandidaten S99 und SM99 ist keine
    blanke 99 - welche es ist, entscheidet die Suche an der Karte."""
    karte = CardFormat.from_items([("s1", "Sushi"), ("sm1", "Sushi")])
    assert find_item_number("Sushi 99", karte) is None


@pytest.mark.parametrize(
    ("text", "card"), [("Nummer N12", "n12"), ("Nummer S12", "s12")]
)
def test_geklebter_klitik_buchstabe_ist_praefix(text, card):
    """Codex PR #155: 'n und 's sind nur abgesetzt ein Wort. Geklebt an die
    Ziffer ("Nummer N12") ist es ein Praefix, das die Karte nicht hat."""
    ref, unclear = sole_item_number(text)
    assert not unclear and ref is not None and not ref.valid and ref.text == card


@pytest.mark.parametrize("text", ["Nummer ZZ12", "Nummer so23"])
def test_geklebtes_praefix_aus_zwei_buchstaben_ist_nummer(text):
    """Codex PR #155: geklebt an die Ziffer ist jedes Praefix, das der Import
    erlaubt, eine genannte Nummer - nie die blanke Zahl, nie eine Rueckfrage."""
    ref, unclear = sole_item_number(text)
    assert not unclear and ref is not None and not ref.valid
    assert find_item_number(text) is None


@pytest.mark.parametrize("text", ["ES12", "Nummer ES12", "Nummer EM1"])
def test_geklebter_buchstabenname_ist_kein_buchstabiertes_praefix(text):
    """Codex PR #155: "ES12" geklebt ist das Praefix ES, nicht "Es zwölf" -
    nie still S12 (oder SM1 aus "EM1")."""
    ref, _ = sole_item_number(text, KARTE)
    assert ref is None or (not ref.valid and ref.cards[0] in ("es12", "em1"))


# --- Code-Review PR #155 ---------------------------------------------------------


@pytest.mark.parametrize(
    "text", ["Ich haette gern Es zwoelf", "hallo, Es zwoelf", "ich moechte Es zwoelf"]
)
def test_buchstabiertes_es_nach_satzanfang_mit_fuellwort(text):
    ref, unclear = sole_item_number(text, KARTE)
    assert not unclear and ref is not None and ref.cards == ("s12",)


def test_ungueltige_nummer_als_wort_mit_leerzeichen():
    """ "Nummer S tausend" wird vorgelesen: "s tausend", nicht "stausend"."""
    ref, _ = sole_item_number("Nummer S tausend", KARTE)
    assert ref is not None and ref.text == "s tausend"


@pytest.mark.parametrize("prefix", ["ja", "es", "so", "um", "zu", "hm"])
def test_gesprochene_woerter_sind_reservierte_praefixe(prefix):
    assert reserved_prefix(prefix)


def test_praefix_s_bleibt_erlaubt():
    assert not reserved_prefix("s") and not reserved_prefix("sm")
