"""Probe area E, PR #722 (#677): quantity and card number never read into each other."""
import pytest

from api.core.errors import Ambiguous, NotFound
from api.domain.menu.importer import ALIASES_FILE, MENU_FILE, apply, parse
from api.domain.menu.search import CLEAR_MATCHES, search_menu
from api.tests.test_domain_menu_search import NOW, KARTE, engine, session, tenant_id  # noqa: F401


@pytest.fixture
def prefixed(session, tenant_id):
    rows = (
        "S1;Lachs Nigiri;Sushi;4,50;;ja\n"
        "S20;Ebi Nigiri;Sushi;4,50;;ja\n"
        "S22;Tamago Nigiri;Sushi;4,50;;ja\n"
        "S40;Maki Lachs;Sushi;4,50;;ja\n"
        "S42;Maki Gurke;Sushi;4,50;;ja\n"
        "SM1;Sushi-Menü klein;Sushi;16,90;;ja\n"
        "40;Curry rot;Hauptgerichte;12,50;;ja\n"
        "42;Curry grün;Hauptgerichte;12,50;;ja\n"
    )
    plan = parse({MENU_FILE: KARTE[MENU_FILE] + rows, ALIASES_FILE: KARTE[ALIASES_FILE]})
    assert plan.ok, plan.errors
    apply(session, tenant_id, plan, now=NOW)
    return tenant_id


def outcome(session, tid, said):
    try:
        r = search_menu(session, tid, said, now=NOW)
    except Ambiguous as e:
        return "ask", e.say
    except NotFound as e:
        return "notfound", e.say
    return r.match_type, [h.number for h in r.results]


@pytest.mark.parametrize(
    ("said", "meant"),
    [
        ("S40 zwei Portionen", "s40"),
        ("die S40 zwei Portionen", "s40"),
        ("Sushi vierzig zwei Portionen", "s40"),
        ("S 20 2 Portionen", "s20"),
        ("S40 zwei mal", "s40"),
        ("S20 2x", "s20"),
    ],
)
def test_prefixed_number_with_a_unit_is_the_dish(session, prefixed, said, meant):
    """#677 item 1 for the prefixed card numbers of T-4.12: the guard in _scan
    needs a marker or article right in front of the digits, and the prefix
    token is neither, so the quantity span reads 40+2 = 42 and the dish
    number disappears."""
    got = outcome(session, prefixed, said)
    print(said, "->", got)
    assert got[0] in CLEAR_MATCHES and got[1] == [meant] or got[0] == "ask", got
    assert got[0] != "notfound", got


@pytest.mark.parametrize("said", ["Nummer 40 zwei bitte", "die 40 zwei, bitte"])
def test_tens_number_with_a_bare_count_is_not_42(session, prefixed, said):
    got = outcome(session, prefixed, said)
    print(said, "->", got)
    assert got != ("exact_number", ["42"]), got


@pytest.mark.parametrize(
    "said",
    [
        "Sushi vierzig",
        "Sushi vierzig zwei Portionen",
        "Sushi vierzig, zwei Portionen",
        "Sushi zwanzig zwei Portionen",
        "Sushi zwanzig zweimal",
        "zwei Portionen Sushi vierzig",
        "Sushi vierzig 2x",
    ],
)
def test_category_word_number_with_a_unit_never_takes_another_dish(session, prefixed, said):
    got = outcome(session, prefixed, said)
    print(said, "->", got)
    assert not (got[0] in CLEAR_MATCHES and got[1] not in (["s40"], ["s20"])), got


@pytest.mark.parametrize(
    "said", ["die 23 x die 24", "die 13 x die 12", "die dreizehn mal die zwölf", "die 13 mal Nummer 12"]
)
def test_article_number_times_article_number_asks(session, prefixed, said):
    """#677 item 2 required: with an article in front, "<n> mal <m>" asks.
    `_bare_number_at` lets only a bare second number ask; an article or
    marker on it makes the second the dish and the first the quantity."""
    got = outcome(session, prefixed, said)
    print(said, "->", got)
    assert got[0] == "ask", got
