"""domain/menu/listing: the menu as the tab "Menü" reads it (#571, plan
number 9): active dishes in card order, the search, and the two notes a row
can carry. Every value comes from the database (rule 1)."""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from api.domain.menu import listing, price_check
from api.domain.menu.details import get_item_details
from api.domain.status import overview
from api.models import ItemAllergen, MenuItem, Tenant
from scripts.seed import seed

NOW = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)


@pytest.fixture
def engine(migrated_db_url):
    engine = create_engine(migrated_db_url)
    yield engine
    engine.dispose()


@pytest.fixture
def session(engine):
    with Session(engine) as s:
        yield s


@pytest.fixture
def tenant_id(session) -> uuid.UUID:
    return uuid.UUID(
        seed(session, tenant_name="Testbetrieb", timezone="Europe/Berlin").tenant_id
    )


def dish(
    session,
    tenant_id,
    number,
    name="Gericht",
    price=690,
    allergens=(),
    register=None,
    rejected=False,
    active=True,
) -> uuid.UUID:
    item = MenuItem(
        tenant_id=tenant_id,
        number=number,
        name=name,
        category="Wok",
        price_cents=price,
        active=active,
        register_price_cents=register,
        register_price_rejected=rejected,
    )
    session.add(item)
    session.flush()
    for code in allergens:
        session.add(
            ItemAllergen(
                menu_item_id=item.id,
                allergen_code=code,
                confirmed_by="Test",
                confirmed_at=NOW,
            )
        )
    session.commit()
    return item.id


def numbers(shown: listing.Listing) -> list[str]:
    return [row.number for row in shown.rows]


# --- the list ---------------------------------------------------------------------


def test_active_dishes_in_card_order_with_the_price_of_the_database(session, tenant_id):
    dish(session, tenant_id, "12", "Ente", 1380)
    dish(session, tenant_id, "2", "Suppe", 450)
    dish(session, tenant_id, "s1", "Sushi", 990)
    dish(session, tenant_id, "23a", "Rolle", 0)
    dish(session, tenant_id, "99", "Alt", active=False)
    shown = listing.listing(session, tenant_id)
    assert [(r.number, r.name, r.price_cents) for r in shown.rows] == [
        ("2", "Suppe", 450),
        ("12", "Ente", 1380),
        ("23a", "Rolle", 0),
        ("s1", "Sushi", 990),
    ]
    assert (shown.items, shown.matching, shown.page, shown.pages) == (4, 4, 1, 1)


def test_two_numbers_of_one_place_keep_their_order(session, tenant_id):
    """ "7" and "07" sort alike: the number itself decides, not the order the
    database happens to answer in, so a page boundary never moves."""
    dish(session, tenant_id, "7", "Zweite")
    dish(session, tenant_id, "07", "Erste")
    dish(session, tenant_id, "007", "Nullte")
    assert numbers(listing.listing(session, tenant_id)) == ["007", "07", "7"]


@pytest.mark.parametrize(
    ("number", "place"),
    [("0" * 5000 + "7", 0), ("9" * 5000, 2), ("s" + "1" * 5000, 2)],
)
def test_a_number_no_card_has_does_not_take_the_list_down(
    session, tenant_id, number, place
):
    """Python converts no run of thousands of digits. Leading zeros are no
    place of their own, any other such run sorts last, and the list and the
    price differences, which sort by the same key, are still there."""
    dish(session, tenant_id, "12", "Ente")
    dish(session, tenant_id, number, "Seltsam", register=700)
    dish(session, tenant_id, "8", "Suppe")
    shown = listing.listing(session, tenant_id)
    assert numbers(shown).index(number) == place and shown.items == 3
    assert shown.price_differences == 1
    assert numbers(listing.listing(session, tenant_id, "seltsam")) == [number]


def test_an_empty_menu_is_one_empty_page(session, tenant_id):
    shown = listing.listing(session, tenant_id, "ente", listing.ONLY_PRICE, 7)
    assert shown.rows == []
    assert (shown.items, shown.matching, shown.page, shown.pages) == (0, 0, 1, 1)
    assert (shown.allergens_missing, shown.price_differences) == (0, 0)


def test_another_restaurant_shows_nothing_of_its_menu(session, tenant_id):
    other = Tenant(name="Nachbar", timezone="Europe/Berlin")
    session.add(other)
    session.flush()
    dish(session, other.id, "1", "Fremd", register=700)
    dish(session, tenant_id, "1", "Eigen", allergens=("A",))
    shown = listing.listing(session, tenant_id)
    assert [r.name for r in shown.rows] == ["Eigen"]
    assert (shown.items, shown.allergens_missing, shown.price_differences) == (1, 0, 0)


# --- the two notes ----------------------------------------------------------------


def test_a_dish_without_an_allergen_row_is_marked_and_no_other(session, tenant_id):
    dish(session, tenant_id, "1", allergens=("A", "G"))
    dish(session, tenant_id, "2")
    dish(session, tenant_id, "3", active=False)
    shown = listing.listing(session, tenant_id)
    assert [(r.number, r.allergens_missing) for r in shown.rows] == [
        ("1", False),
        ("2", True),
    ]
    assert shown.allergens_missing == 1


def test_the_note_says_what_the_agent_says_about_the_dish(session, tenant_id):
    """One rule, two readers: a dish is marked exactly when `get_item_details`
    has no allergen information for it, and the count is the operator's."""
    dish(session, tenant_id, "1", allergens=("C",))
    dish(session, tenant_id, "2")
    shown = listing.listing(session, tenant_id)
    for row in shown.rows:
        details = get_item_details(session, tenant_id, row.menu_item_id, True)
        assert row.allergens_missing is (not details.allergens.known)
    tenant = session.get(Tenant, tenant_id)
    status = overview.status(session, tenant, NOW).menu
    assert status.without_allergen_information == shown.allergens_missing == 1
    assert status.price_differences == shown.price_differences == 0
    assert status.items == shown.items == 2


def test_only_an_open_register_price_is_a_difference(session, tenant_id):
    dish(session, tenant_id, "1", price=690, register=720)
    dish(session, tenant_id, "2", price=690, register=750, rejected=True)
    dish(session, tenant_id, "3", price=690)
    dish(session, tenant_id, "4", price=690, register=0)
    shown = listing.listing(session, tenant_id)
    assert [(r.number, r.price_cents, r.register_cents) for r in shown.rows] == [
        ("1", 690, 720),
        ("2", 690, None),
        ("3", 690, None),
        ("4", 690, 0),
    ]
    assert shown.price_differences == 2
    # The same dishes the board asks the team about.
    assert {
        d.menu_item_id for d in price_check.open_differences(session, tenant_id)
    } == {r.menu_item_id for r in shown.rows if r.register_cents is not None}


def test_an_answered_difference_is_gone_from_the_list(session, tenant_id):
    item_id = dish(session, tenant_id, "1", price=690, register=720)
    price_check.decide(session, tenant_id, item_id, 720, take=True)
    (row,) = listing.listing(session, tenant_id).rows
    assert (row.price_cents, row.register_cents) == (720, None)


# --- the search and the filter ----------------------------------------------------


def test_the_search_finds_a_number_or_a_part_of_the_name(session, tenant_id):
    dish(session, tenant_id, "07", "Wan-Tan-Suppe")
    dish(session, tenant_id, "70", "Knusprige Ente")
    dish(session, tenant_id, "8", "8 Kostbarkeiten")
    assert numbers(listing.listing(session, tenant_id, "7")) == ["07", "70"]
    assert numbers(listing.listing(session, tenant_id, " ente ")) == ["70"]
    assert numbers(listing.listing(session, tenant_id, "8 Kostbar")) == ["8"]
    nothing = listing.listing(session, tenant_id, "pizza")
    assert (nothing.rows, nothing.matching, nothing.items) == ([], 0, 3)


def test_the_counts_are_of_the_whole_menu_whatever_is_searched(session, tenant_id):
    dish(session, tenant_id, "1", "Ente", register=700)
    dish(session, tenant_id, "2", "Suppe", allergens=("A",))
    shown = listing.listing(session, tenant_id, "suppe")
    assert (shown.matching, shown.items) == (1, 2)
    assert (shown.allergens_missing, shown.price_differences) == (1, 1)


def test_the_filter_keeps_the_dishes_with_that_note(session, tenant_id):
    dish(session, tenant_id, "1", "Ente", register=700)
    dish(session, tenant_id, "2", "Suppe", allergens=("A",), register=500)
    dish(session, tenant_id, "3", "Reis", allergens=("A",))
    find = listing.listing
    assert numbers(find(session, tenant_id, only=listing.ONLY_ALLERGENS)) == ["1"]
    assert numbers(find(session, tenant_id, only=listing.ONLY_PRICE)) == ["1", "2"]
    assert numbers(find(session, tenant_id, "suppe", listing.ONLY_PRICE)) == ["2"]
    # A filter nobody offers filters nothing.
    assert numbers(find(session, tenant_id, only="alles")) == ["1", "2", "3"]


def test_a_long_search_text_is_cut_before_it_is_compared(session, tenant_id):
    dish(session, tenant_id, "1", "Ente")
    long = "ente" + " " * listing.QUERY_MAX + "pizza"
    assert numbers(listing.listing(session, tenant_id, long)) == ["1"]


# --- pages ------------------------------------------------------------------------


def test_a_large_menu_comes_in_pages(session, tenant_id, monkeypatch):
    monkeypatch.setattr(listing, "PAGE_SIZE", 2)
    for number in range(1, 6):
        dish(session, tenant_id, str(number))
    first = listing.listing(session, tenant_id)
    assert (numbers(first), first.page, first.pages, first.matching) == (
        ["1", "2"],
        1,
        3,
        5,
    )
    assert numbers(listing.listing(session, tenant_id, page=3)) == ["5"]


@pytest.mark.parametrize(("asked", "page"), [(0, 1), (-4, 1), (4, 3), (10**30, 3)])
def test_a_page_that_does_not_exist_is_the_nearest_one(
    session, tenant_id, monkeypatch, asked, page
):
    monkeypatch.setattr(listing, "PAGE_SIZE", 2)
    for number in range(1, 6):
        dish(session, tenant_id, str(number))
    shown = listing.listing(session, tenant_id, page=asked)
    assert shown.page == page and shown.rows


def test_the_list_costs_the_same_queries_whatever_the_size(session, tenant_id, engine):
    """No query per dish: a menu of a few hundred rows is read in a fixed
    number of statements."""
    counts = []
    for size in (3, 40):
        for number in range(size):
            dish(session, tenant_id, f"{size}{number:02d}", allergens=("A",))
        statements: list[str] = []

        def count(_conn, _cursor, statement, *_rest, seen=statements):
            seen.append(statement)

        event.listen(engine, "before_cursor_execute", count)
        try:
            session.expire_all()
            listing.listing(session, tenant_id)
        finally:
            event.remove(engine, "before_cursor_execute", count)
        counts.append(len(statements))
    assert counts[0] == counts[1] <= 3
