"""Zerlegen je Position vor search_menu (T-4.5, docs/01 Open Points, docs/04 §search_menu)."""

import pytest

from api.domain.menu.search import search_menu
from api.domain.menu.split import split_positions
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
        ("die hundert und einmal Pho Bo", ["die hundert und einmal Pho Bo"]),
        (
            "zweimal die drei und zwanzig und die 13",
            ["zweimal die drei und zwanzig", "die 13"],
        ),
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
