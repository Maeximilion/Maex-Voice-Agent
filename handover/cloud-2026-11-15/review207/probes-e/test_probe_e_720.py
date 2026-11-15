"""Probe area E, PR #720: priced additions in a wish."""
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from api.core.errors import Ambiguous
from api.domain.menu.importer import ALIASES_FILE, ALLERGENS_FILE, MENU_FILE, OPTIONS_FILE, apply, parse
from api.domain.menu.search import search_menu
from scripts.seed import seed

NOW = datetime(2026, 9, 18, 16, 0, tzinfo=UTC)
KARTE = {
    MENU_FILE: "number;name;category;price_eur;description;active\n47;Ente knusprig;Hauptgerichte;15,50;;ja\n",
    OPTIONS_FILE: (
        "number;group_name;option_name;price_delta_eur;is_default;required;price_reason\n"
        "47;Beilage;Reis;0,00;ja;nein;\n"
        "47;Beilage;Nudeln;3,00;nein;nein;\n"
        "47;Extra;Tofu;1,50;nein;nein;\n"
        "47;Extra;Ei;1,00;nein;nein;\n"
    ),
    ALLERGENS_FILE: "number;allergen_codes;confirmed_by\n",
    ALIASES_FILE: "number;alias\n",
}


@pytest.fixture
def session(migrated_db_url):
    engine = create_engine(migrated_db_url)
    with Session(engine) as s:
        yield s
    engine.dispose()


@pytest.fixture
def tenant_id(session):
    tid = uuid.UUID(seed(session, tenant_name="ProbeE", timezone="Europe/Berlin").tenant_id)
    plan = parse(KARTE)
    assert plan.ok, plan.errors
    apply(session, tid, plan, now=NOW)
    return tid


@pytest.mark.parametrize(
    "said",
    [
        "die 47 ohne Zwiebeln, plus Tofu",
        "die 47 ohne Zwiebeln, dazu Tofu",
        "die 47 ohne Zwiebeln, zusätzlich Tofu",
        "die 47 ohne Zwiebeln, stattdessen Tofu",
        "die 47 ohne Zwiebeln, aber Tofu",
        "die 47 ohne Zwiebeln, Tofu dazu",
        "die 47 ohne Zwiebeln, 2x Tofu",
        "die 47 mit Nudeln, ohne Zwiebeln, plus Tofu",
        "die 47 mit Nudeln, ohne Zwiebeln, dazu Tofu",
    ],
)
def test_priced_option_never_rides_in_a_note(session, tenant_id, said):
    try:
        found = search_menu(session, tenant_id, said, now=NOW)
    except Ambiguous:
        return
    wish = found.wish
    assert wish is not None
    print(said, "->", wish.kind, wish.option, wish.price_delta_cents, repr(wish.note), repr(wish.text))
    free = (wish.note or "") if wish.kind != "note" else wish.text
    assert "Tofu" not in free, f"Tofu (+150 ct) goes out free: {wish}"


@pytest.mark.parametrize(
    "said",
    [
        "die 47 ohne Zwiebeln, dafür 2x Tofu",
        "die 47 ohne Zwiebeln, dafür zweimal Tofu",
        "die 47 kein Huhn, sondern 2 Stück Tofu",
    ],
)
def test_count_behind_a_swap_word_is_asked(session, tenant_id, said):
    try:
        found = search_menu(session, tenant_id, said, now=NOW)
    except Ambiguous:
        return
    wish = found.wish
    print(said, "->", wish)
    assert not (wish.kind == "option" and wish.option == "Tofu"), (
        f"two tofu said, one charged, nothing asked: {wish}"
    )


@pytest.mark.parametrize(
    "said",
    ["zweimal die 47, einmal mit Tofu", "zweimal die 47, eine mit Nudeln", "zwei 47er, einmal mit Tofu"],
)
def test_a_per_portion_addition_is_not_put_on_every_portion(session, tenant_id, said):
    """#671 item 3 class: "einmal" in front of "mit" is the count of the
    addition (one of the two portions), dropped as a dish quantity, so the
    option and its price land on the whole line of two."""
    try:
        found = search_menu(session, tenant_id, said, now=NOW)
    except Ambiguous:
        return
    wish = found.wish
    print(said, "->", found.match_type, [h.number for h in found.results], wish)
    assert not (wish is not None and wish.kind == "option"), wish


@pytest.mark.parametrize("said", ["die 47 mit 2x Tofu", "die 47 mit zweimal Tofu", "die 47 mit 2 Stück Tofu"])
def test_the_issue_example_without_nummer_is_asked_not_lost(session, tenant_id, said):
    """#671's own example is "die 23 mit 2x Ei"; the PR tests the search only
    with "Nummer 23 ...". Without the marker the search ends NotFound."""
    with pytest.raises(Ambiguous):
        search_menu(session, tenant_id, said, now=NOW)
