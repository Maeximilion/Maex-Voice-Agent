"""Zerlegen je Position vor search_menu (T-4.5, docs/01 Open Points, docs/04 §search_menu)."""

import pytest

from api.domain.menu.search import search_menu
from api.domain.menu.split import raw_pieces, separator_pieces, split_positions
from api.tests.test_domain_menu_search import (  # noqa: F401
    NOW,
    engine,
    session,
    tenant_id,
)


@pytest.mark.parametrize(
    ("gesagt", "teile"),
    [
        ("die 23 und einmal Pho Bo", ["die 23", "einmal Pho Bo"]),
        ("die Nummer 23 und einmal Pho Bo", ["die Nummer 23", "einmal Pho Bo"]),
        (
            "einmal die 23 und zweimal Frühlingsrollen",
            ["einmal die 23", "zweimal Frühlingsrollen"],
        ),
        ("die 23, die 24 sowie die 12.", ["die 23", "die 24", "die 12"]),
        # Eine Position bleibt eine
        ("zweimal die 23", ["zweimal die 23"]),
        ("Nummer 23 mit Erdnusssauce", ["Nummer 23 mit Erdnusssauce"]),
        ("die drei und zwanzig", ["die drei und zwanzig"]),
        # Hunderter mit "und" sind eine Nummer, nicht 100 und 1 (Codex PR #124)
        ("Nummer hundert und eins", ["Nummer hundert und eins"]),
        ("die zweihundert und drei", ["die zweihundert und drei"]),
        # Nach "hundert" nur eine Zahl anhängen, keine neue Position (Codex PR #124)
        ("die hundert und einmal Pho Bo", ["die hundert", "einmal Pho Bo"]),
        ("die hundert und eine Cola", ["die hundert", "eine Cola"]),
        ("Nummer hundert und drei mit Reis", ["Nummer hundert und drei mit Reis"]),
        # Einleitende Wörter vor einer neuen Position (Codex PR #124)
        ("die 23 und dazu eine Cola", ["die 23", "dazu eine Cola"]),
        ("die 23 und außerdem zweimal die 13", ["die 23", "außerdem zweimal die 13"]),
        (
            "zweimal die drei und zwanzig und die 13",
            ["zweimal die drei und zwanzig", "die 13"],
        ),
        # A pause at the "und" keeps the number one, not the 3 and the 20
        # (Codex PR #147), but only when nothing else follows; a pause before
        # a new position still splits.
        ("die drei und äh zwanzig", ["die drei und äh zwanzig"]),
        ("drei und bitte zwanzig", ["drei und bitte zwanzig"]),
        ("die drei äh und zwanzig", ["die drei äh und zwanzig"]),
        # Also next to a name, a letter or a quantity: sole_item_number asks.
        (
            "die drei und bitte zwanzig Frühlingsrollen",
            ["die drei und bitte zwanzig Frühlingsrollen"],
        ),
        ("die drei und äh 20a", ["die drei und äh 20a"]),
        ("zwei und zwanzigmal die 13", ["zwei und zwanzigmal die 13"]),
        ("die 23 und äh die 13", ["die 23", "äh die 13"]),
        ("die 4 bitte und 20", ["die 4 bitte", "20"]),
        ("Pho Bo drei äh und zwanzig", ["Pho Bo drei äh und zwanzig"]),
        # A comma right after the "und" is the same pause (#328), also at the
        # start of the sentence; a number that is no unit before it still splits.
        ("die drei und, zwanzig", ["die drei und, zwanzig"]),
        ("die 3 und, 20", ["die 3 und, 20"]),
        ("die drei und, äh, zwanzig", ["die drei und, äh, zwanzig"]),
        ("und äh, zwanzig", ["und äh, zwanzig"]),
        ("und, zwanzig", ["und, zwanzig"]),
        ("die 13, und äh, zwanzig", ["die 13, und äh", "zwanzig"]),
        ("einmal Pho Bo, und, 30", ["einmal Pho Bo, und", "30"]),
        # Every spelling of a hesitation (#327).
        ("die drei und öh zwanzig", ["die drei und öh zwanzig"]),
        ("die 23, eh, 24", ["die 23, eh, 24"]),
        ("die 23 und äääh die 13", ["die 23", "äääh die 13"]),
        # Korrektur und Alternative: im Zweifel ganz lassen
        ("Nummer 23, nein, 24", ["Nummer 23, nein, 24"]),
        ("die 23 oder die 24", ["die 23 oder die 24"]),
        ("die 23, äh, 24", ["die 23, äh, 24"]),
        ("die 23 und bitte", ["die 23 und bitte"]),
        # Ein Teil ohne eigene Position ist Name oder Hinweis (Codex PR #124)
        ("Ente süß und sauer", ["Ente süß und sauer"]),
        ("Nummer 23, ohne Zwiebeln", ["Nummer 23, ohne Zwiebeln"]),
        ("die 23 und einmal ohne Koriander", ["die 23 und einmal ohne Koriander"]),
        (
            "zwei Frühlingsrollen und eine Pho Bo",
            ["zwei Frühlingsrollen", "eine Pho Bo"],
        ),
        # Die Zahl muss den Teil eröffnen, nicht irgendwo darin stehen (Codex PR #124)
        ("Nummer 23, Schärfe 2", ["Nummer 23, Schärfe 2"]),
        ("die 23 und Pho Bo für 2 Personen", ["die 23 und Pho Bo für 2 Personen"]),
        ("die 23 und 2 x Pho Bo", ["die 23", "2 x Pho Bo"]),
        ("die 23 und die Nummer 13", ["die 23", "die Nummer 13"]),
        # Hinweis nach mehrteiliger Menge bleibt Hinweis (Codex PR #124)
        ("die 23 und 2 x ohne Koriander", ["die 23 und 2 x ohne Koriander"]),
        (
            "die 23 und zwei Portionen ohne Koriander",
            ["die 23 und zwei Portionen ohne Koriander"],
        ),
        ("die 23 und eine mit Reis", ["die 23 und eine mit Reis"]),
        # A card number with its own word opens a position, a wish behind it
        # is its own (docs/24, own test PR #139).
        (
            "die 23 ohne Karotten und die 13 mit Allergie",
            ["die 23 ohne Karotten", "die 13 mit Allergie"],
        ),
        ("die 23 und die 13 ohne Zwiebeln", ["die 23", "die 13 ohne Zwiebeln"]),
        (
            "die 23 und Nummer 13 mit Erdnussallergie",
            ["die 23", "Nummer 13 mit Erdnussallergie"],
        ),
        # A dish named by its name before keeps the old rule: the number may
        # point back at it (deep review of item 14).
        (
            "Pho Bo und zweimal die 23 ohne Koriander",
            ["Pho Bo und zweimal die 23 ohne Koriander"],
        ),
        ("die 23 und Nummer 5 ohne Zwiebeln", ["die 23", "Nummer 5 ohne Zwiebeln"]),
        ("die 23 und Nummer zwei mit Reis", ["die 23", "Nummer zwei mit Reis"]),
        # The same words pointing back at portions just named, a single digit
        # and a quantity keep the old rule: the wish stays with the position
        # before (deep review of item 14).
        (
            "zweimal die 13 und die zwei mit Erdnussallergie",
            ["zweimal die 13 und die zwei mit Erdnussallergie"],
        ),
        (
            "zwei Pho Bo, die eine ohne Koriander",
            ["zwei Pho Bo, die eine ohne Koriander"],
        ),
        (
            "zweimal die 13, der eine mit Erdnussallergie",
            ["zweimal die 13, der eine mit Erdnussallergie"],
        ),
        ("zweimal die 13, die 2 mit Reis", ["zweimal die 13, die 2 mit Reis"]),
        ("die 23, das 1 mit Reis", ["die 23, das 1 mit Reis"]),
        (
            "die 23 und die dreizehn ohne Zwiebeln",
            ["die 23 und die dreizehn ohne Zwiebeln"],
        ),
        (
            "die 23 und die 2 x ohne Koriander",
            ["die 23 und die 2 x ohne Koriander"],
        ),
        # A number that points back at what the sentence said before it: a
        # dish named by its name, the same number, a count that reaches it.
        (
            "zwei Pho Bo, die 13 ohne Koriander",
            ["zwei Pho Bo, die 13 ohne Koriander"],
        ),
        (
            "zweimal die 13, die 13 mit Erdnussallergie",
            ["zweimal die 13, die 13 mit Erdnussallergie"],
        ),
        (
            "zwanzig Pho Bo, die 12 ohne Koriander",
            ["zwanzig Pho Bo, die 12 ohne Koriander"],
        ),
        (
            "zwölfmal die 23, die 12 ohne Koriander",
            ["zwölfmal die 23, die 12 ohne Koriander"],
        ),
        ("die 23, quatsch, die 24 mit Reis", ["die 23, quatsch, die 24 mit Reis"]),
        # A count below the number and a name behind a card number point at
        # nothing the number could mean.
        (
            "zweimal die 23 und die 13 ohne Zwiebeln",
            ["zweimal die 23", "die 13 ohne Zwiebeln"],
        ),
        (
            "die 23 Frühlingsrollen ohne Karotten und die 13 mit Allergie",
            ["die 23 Frühlingsrollen ohne Karotten", "die 13 mit Allergie"],
        ),
        ("die 23 und zweimal die 13", ["die 23", "zweimal die 13"]),
        (
            "die 23 und zwei Portionen Pho Bo ohne Koriander",
            ["die 23", "zwei Portionen Pho Bo ohne Koriander"],
        ),
        # Ein Name mit "und" hält die äußeren Grenzen nicht auf (Codex PR #124)
        (
            "die 23 und eine Ente süß und sauer",
            ["die 23", "eine Ente süß und sauer"],
        ),
        (
            "eine Ente süß und sauer und zweimal die 13",
            ["eine Ente süß und sauer", "zweimal die 13"],
        ),
        ("", []),
    ],
)
def test_split_positions(gesagt, teile):
    assert split_positions(gesagt) == teile


@pytest.mark.parametrize(
    ("gesagt", "nummern"),
    [
        # Vorher: fuzzy_single auf Pho Bo, die 23 fiel still weg (Open Point 21.09.2026).
        ("die 23 und einmal Pho Bo", ["23", "13"]),
        # Vorher: ambiguous mit Rückfrage nach der einen Nummer.
        ("die Nummer 23 und einmal Pho Bo", ["23", "13"]),
        # Vorher: alias auf 23, die zweite Position fiel weg.
        ("einmal die 23 und zweimal Sommerrollen", ["23", "24"]),
    ],
)
def test_jede_position_findet_ihr_gericht(session, tenant_id, gesagt, nummern):  # noqa: F811
    gefunden = []
    for teil in split_positions(gesagt):
        result = search_menu(session, tenant_id, teil, now=NOW)
        assert len(result.results) == 1, (teil, result.match_type)
        gefunden.append(result.results[0].number)
    assert gefunden == nummern


@pytest.mark.parametrize(
    ("gesagt", "stuecke"),
    [
        ("die 23, mit Reis", ["die 23, mit Reis"]),
        ("die 23, zweimal ohne Koriander", ["die 23, zweimal ohne Koriander"]),
        ("Pho Bo, dazu extra scharf", ["Pho Bo, dazu extra scharf"]),
        ("die 23 und Pho Bo", ["die 23", "Pho Bo"]),
        (
            "die 23 ohne Karotten und die 13 mit Allergie",
            ["die 23 ohne Karotten", "die 13 mit Allergie"],
        ),
        ("die 23, 2 x ohne Koriander", ["die 23, 2 x ohne Koriander"]),
        (
            "zweimal die 13, die eine ohne Koriander",
            ["zweimal die 13, die eine ohne Koriander"],
        ),
        (
            "zwei Pho Bo, die 2 x ohne Koriander",
            ["zwei Pho Bo, die 2 x ohne Koriander"],
        ),
        ("zwei Pho Bo, die 13 ohne Koriander", ["zwei Pho Bo, die 13 ohne Koriander"]),
        (
            "zweimal die 13, die 13 mit Erdnussallergie",
            ["zweimal die 13, die 13 mit Erdnussallergie"],
        ),
        # The menu check sees the paused number whole as well (Codex PR #147).
        ("die drei und äh zwanzig", ["die drei und äh zwanzig"]),
        # A comma on either side of the "und" is the same pause (#328).
        (
            "die drei, und zwanzig Frühlingsrollen",
            ["die drei, und zwanzig Frühlingsrollen"],
        ),
        ("die 3, und äh 20 mal die 13", ["die 3, und äh 20 mal die 13"]),
        (
            "die drei und, zwanzig Frühlingsrollen",
            ["die drei und, zwanzig Frühlingsrollen"],
        ),
        # Not after a number that is no unit, nor after a name.
        (
            "die 13, und zwanzig Frühlingsrollen",
            ["die 13", "und zwanzig Frühlingsrollen"],
        ),
        ("Pho Bo, und 30", ["Pho Bo", "und 30"]),
    ],
)
def test_raw_pieces_haengt_hinweise_an(gesagt, stuecke):
    """Codex PR #127, P1: ein Hinweis nach dem Trenner ist keine neue Position,
    auch fuer den Gegencheck mit der Karte nicht."""
    assert raw_pieces(gesagt) == stuecke


@pytest.mark.parametrize(
    ("text", "stuecke"),
    [("Pho Bo", 1), ("Fisch und Chips", 2), ("Fisch, Chips und Salat", 3)],
)
def test_separator_pieces_zaehlt_an_den_trennern(text, stuecke):
    assert separator_pieces(text) == stuecke
