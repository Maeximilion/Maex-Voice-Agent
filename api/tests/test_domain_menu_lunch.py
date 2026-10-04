"""Lunch menus are only sold inside the lunch window (T-4.13).

The window comes from `lunch_hours`, the flag from `menu_items.lunch_only`.
Outside the window `search_menu` does not deliver a lunch menu and
`draft_order` rejects it. The menu is invented; no register row is in here.
"""

import uuid
from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import Session

from api.config import settings
from api.core.errors import Closed, NotFound
from api.db import get_db
from api.domain.menu.importer import (
    ALIASES_FILE,
    ALLERGENS_FILE,
    MENU_FILE,
    OPTIONS_FILE,
    apply,
    parse,
)
from api.domain.menu.lunch import Lunch
from api.domain.menu.search import position_parts, search_menu
from api.domain.menu.sold_out import alternatives
from api.domain.ordering import draft_order
from api.main import app
from api.models import Call, LunchHours, MenuItem, Order
from api.schemas.orders import DraftOrderRequest
from api.tests.conftest import p95_ms
from scripts.seed import seed

BERLIN = ZoneInfo("Europe/Berlin")
AUTH = {"Authorization": f"Bearer {settings.agent_api_token}"}
# 15.09.2026 is a Tuesday. Lunch menus Tuesday to Friday, 11:30 to 14:00; the
# seed opens pickup 11:30-14:00 and 17:00-22:00, Monday closed.
TUESDAY = date(2026, 9, 15)
LUNCH = datetime(2026, 9, 15, 12, 30, tzinfo=BERLIN)
EVENING = datetime(2026, 9, 15, 18, 0, tzinfo=BERLIN)
SATURDAY = datetime(2026, 9, 19, 12, 30, tzinfo=BERLIN)
LUNCH_DAYS = (1, 2, 3, 4)
WINDOW = (time(11, 30), time(14, 0))

SAY_M4A = (
    "Menü Ente knusprig ist ein Mittagsmenü. Das gibt es heute nur von halb "
    "zwölf bis zwei Uhr."
)
SAY_SEVERAL = "Die Mittagsmenüs gibt es heute nur von halb zwölf bis zwei Uhr."

MENU = {
    MENU_FILE: (
        "number;name;category;price_eur;description;active;lunch_only\n"
        "23;Frühlingsrollen;Vorspeisen;6,90;;ja;nein\n"
        "47;Ente knusprig;Hauptgerichte;15,50;;ja;nein\n"
        "48;Ente süß-sauer;Hauptgerichte;14,90;;ja;\n"
        "49;Tagesteller;Hauptgerichte;9,90;;ja;ja\n"
        "M1;Menü Nudelsuppe und Bratreis;Menü;9,50;;ja;ja\n"
        "M4A;Menü Ente knusprig;Menü;11,90;;ja;ja\n"
        "M4B;Menü Ente süß-sauer;Menü;11,90;;ja;ja\n"
        "VM1;Menü Tofu und Reis;Menü Vegetarisch;8,90;;ja;ja\n"
        "VM5C;Menü Gemüse-Curry;Menü Vegetarisch;9,90;;ja;ja\n"
        "M8;Menü Maki;Menü Sushi;12,50;;ja;ja\n"
        "VM8;Menü Gurken-Maki;Menü Sushi;10,50;;ja;ja\n"
    ),
    OPTIONS_FILE: "number;group_name;option_name;price_delta_eur;is_default;required\n",
    ALLERGENS_FILE: "number;allergen_codes;confirmed_by\n",
    ALIASES_FILE: "number;alias\nM4A;Mittagsente\n",
}


@pytest.fixture
def engine(migrated_db_url):
    engine = create_engine(migrated_db_url)
    yield engine
    engine.dispose()


@pytest.fixture
def session(engine):
    with Session(engine) as s:
        yield s


def _tenant(session, name: str, days=LUNCH_DAYS) -> uuid.UUID:
    tid = uuid.UUID(seed(session, tenant_name=name, timezone="Europe/Berlin").tenant_id)
    session.add_all(
        LunchHours(tenant_id=tid, weekday=d, starts_at=WINDOW[0], ends_at=WINDOW[1])
        for d in days
    )
    plan = parse(MENU)
    assert plan.ok, plan.errors
    apply(session, tid, plan, now=LUNCH)
    return tid


@pytest.fixture
def tenant_id(session) -> uuid.UUID:
    return _tenant(session, "Testbetrieb")


def item(session, tenant_id, number: str) -> MenuItem:
    return session.scalar(
        select(MenuItem).where(
            MenuItem.tenant_id == tenant_id, MenuItem.number == number
        )
    )


def numbers(result) -> list[str]:
    return [hit.number for hit in result.results]


# --- the window ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("at", "is_open"),
    [
        (datetime(2026, 9, 15, 11, 29, 59, tzinfo=BERLIN), False),
        (datetime(2026, 9, 15, 11, 30, tzinfo=BERLIN), True),
        (datetime(2026, 9, 15, 13, 59, 59, tzinfo=BERLIN), True),
        # The end itself is outside, like closing time.
        (datetime(2026, 9, 15, 14, 0, tzinfo=BERLIN), False),
        # Local time decides, not UTC: 10:30 UTC is 12:30 in summer, 12:30 UTC
        # is 14:30.
        (datetime(2026, 9, 15, 10, 30, tzinfo=UTC), True),
        (datetime(2026, 9, 15, 12, 30, tzinfo=UTC), False),
        # Winter time: 10:45 UTC is 11:45 local on Tuesday 03.11.2026.
        (datetime(2026, 11, 3, 10, 45, tzinfo=UTC), True),
        (datetime(2026, 11, 3, 13, 15, tzinfo=UTC), False),
        # Saturday and Monday have no row.
        (SATURDAY, False),
        (datetime(2026, 9, 14, 12, 30, tzinfo=BERLIN), False),
    ],
)
def test_window_is_local_time_start_inclusive_end_exclusive(
    session, tenant_id, at, is_open
):
    assert Lunch(session, tenant_id, at).open is is_open


def test_only_lunch_menus_are_ever_closed(session, tenant_id):
    lunch = Lunch(session, tenant_id, EVENING)

    assert lunch.closed(item(session, tenant_id, "m4a"))
    assert not lunch.closed(item(session, tenant_id, "47"))
    assert not Lunch(session, tenant_id, LUNCH).closed(item(session, tenant_id, "m4a"))


def test_without_a_window_no_lunch_menu_is_sold(session):
    """After the migration nobody has entered a window yet: never a guess."""
    tid = _tenant(session, "Ohne Fenster", days=())

    with pytest.raises(Closed) as exc:
        search_menu(session, tid, "M vier A", now=LUNCH)
    assert exc.value.say == (
        "Menü Ente knusprig ist ein Mittagsmenü. Das gibt es heute leider nicht."
    )


def test_window_of_another_tenant_does_not_count(session, tenant_id):
    other = _tenant(session, "Anderer Betrieb", days=())

    assert Lunch(session, tenant_id, LUNCH).open
    assert not Lunch(session, other, LUNCH).open


# --- search_menu inside the window -------------------------------------------------


@pytest.mark.parametrize(
    ("said", "number"),
    [
        ("M vier A", "m4a"),
        ("Em vier A", "m4a"),
        ("M4A", "m4a"),
        ("VM fünf C", "vm5c"),
        ("Vau Em fünf C", "vm5c"),
        # Category word: only M4A is on the menu, there is no VM4A.
        ("Menü vier A", "m4a"),
        ("zweimal Menü vier B", "m4b"),
    ],
)
def test_lunch_number_at_lunch_is_a_dish_like_any_other(
    session, tenant_id, said, number
):
    result = search_menu(session, tenant_id, said, now=LUNCH)

    assert result.match_type == "exact_number" and numbers(result) == [number]
    assert result.say is None and not result.results[0].sold_out


@pytest.mark.parametrize(
    ("said", "both"),
    [("Menü eins", ["m1", "vm1"]), ("Menü acht", ["m8", "vm8"])],
)
def test_category_word_menu_asks_between_m_and_vm(session, tenant_id, said, both):
    """ "Menü" names the group Menü with its sub-groups Menü Vegetarisch and
    Menü Sushi; M8 and VM8 are in the sub-group."""
    result = search_menu(session, tenant_id, said, now=LUNCH)

    assert result.match_type == "ambiguous" and numbers(result) == both


def test_at_lunch_a_name_that_fits_both_asks_back(session, tenant_id):
    result = search_menu(session, tenant_id, "Ente süß-sauer", now=LUNCH)

    assert result.match_type == "ambiguous" and set(numbers(result)) == {"48", "m4b"}


# --- search_menu outside the window ------------------------------------------------


@pytest.mark.parametrize(
    "said", ["M vier A", "Em vier A", "Nummer M4A", "zweimal M4A", "Mittagsente"]
)
def test_lunch_menu_in_the_evening_is_closed_with_the_window(session, tenant_id, said):
    with pytest.raises(Closed) as exc:
        search_menu(session, tenant_id, said, now=EVENING)

    assert exc.value.code == "closed" and exc.value.say == SAY_M4A


def test_on_a_day_without_lunch_menus_the_sentence_says_so(session, tenant_id):
    with pytest.raises(Closed) as exc:
        search_menu(session, tenant_id, "M vier A", now=SATURDAY)

    assert exc.value.say == (
        "Menü Ente knusprig ist ein Mittagsmenü. Das gibt es heute leider nicht."
    )


def test_category_word_in_the_evening_names_no_single_dish(session, tenant_id):
    with pytest.raises(Closed) as exc:
        search_menu(session, tenant_id, "Menü eins", now=EVENING)

    assert exc.value.say == SAY_SEVERAL


def test_unknown_lunch_number_stays_not_found(session, tenant_id):
    """M99 is not on the menu at any time: not_found, not the lunch sentence."""
    with pytest.raises(NotFound):
        search_menu(session, tenant_id, "M 99", now=EVENING)


@pytest.mark.parametrize(
    ("said", "number"),
    [("Ente süß-sauer", "48"), ("Ente knusprig", "47"), ("einmal Tagesteler", None)],
)
def test_name_search_in_the_evening_looks_past_lunch_menus(
    session, tenant_id, said, number
):
    if number is None:
        # Only a lunch menu carries the name: the guest hears why, never
        # not_found and never another dish.
        with pytest.raises(Closed) as exc:
            search_menu(session, tenant_id, said, now=EVENING)
        assert exc.value.say.startswith("Tagesteller ist ein Mittagsmenü.")
        return
    result = search_menu(session, tenant_id, said, now=EVENING)

    assert result.match_type == "fuzzy_single" and numbers(result) == [number]


def test_lunch_menu_named_by_name_is_never_swapped_for_the_regular_dish(
    session, tenant_id
):
    """ "Menü Ente knusprig" fits the lunch menu exactly and dish 47 almost:
    the guest meant the menu and hears why it is not sold (rule 2)."""
    with pytest.raises(Closed) as exc:
        search_menu(session, tenant_id, "Menü Ente knusprig", now=EVENING)

    assert exc.value.say == SAY_M4A


def test_lunch_menu_with_a_wish_in_the_evening_is_closed(session, tenant_id):
    with pytest.raises(Closed):
        search_menu(session, tenant_id, "Menü Ente knusprig ohne Zwiebeln", now=EVENING)


def test_closed_lunch_menu_is_a_position_of_its_own(session, tenant_id):
    """ "Ente knusprig und Menü Gemüse-Curry": the second part is not dropped
    and not merged; searched on its own it says why it is not sold."""
    said = "Ente knusprig und Menü Gemüse-Curry"

    assert position_parts(session, tenant_id, said, now=EVENING) == [
        "Ente knusprig",
        "Menü Gemüse-Curry",
    ]


def test_sold_out_alternatives_skip_closed_lunch_menus(session, tenant_id):
    duck = item(session, tenant_id, "47")

    evening = alternatives(session, duck, EVENING)
    lunch = alternatives(session, duck, LUNCH)

    assert [o.number for o in evening] == ["48"]
    assert [o.number for o in lunch] == ["48", "49"]


# --- draft_order ----------------------------------------------------------------------


def _request(session, tenant_id, number: str, at: datetime, **overrides):
    call = Call(
        tenant_id=tenant_id,
        external_session_id=uuid.uuid4().hex,
        started_at=at,
        delete_after=TUESDAY,
    )
    session.add(call)
    session.commit()
    body = {
        "call_id": call.id,
        "tenant_id": tenant_id,
        "idempotency_key": uuid.uuid4().hex,
        "type": "pickup",
        "customer": {"name": "Müller", "phone": "0721 555 1234"},
        "items": [{"menu_item_id": item(session, tenant_id, number).id, "quantity": 1}],
        **overrides,
    }
    return DraftOrderRequest(**body)


def test_draft_order_takes_a_lunch_menu_at_lunch(session, tenant_id):
    draft = draft_order(session, _request(session, tenant_id, "m4a", LUNCH), now=LUNCH)

    assert draft.status == "draft" and draft.total_cents == 1190


def test_draft_order_rejects_a_lunch_menu_after_the_window(session, tenant_id):
    """The id came from a search at lunch; the window closed during the call."""
    req = _request(session, tenant_id, "m4a", LUNCH)

    with pytest.raises(Closed) as exc:
        draft_order(session, req, now=EVENING)

    assert exc.value.say == SAY_M4A
    assert session.scalar(select(Order.id)) is None


def test_draft_order_replay_after_the_window_returns_the_same_draft(session, tenant_id):
    req = _request(session, tenant_id, "m4a", LUNCH)
    first = draft_order(session, req, now=LUNCH)

    again = draft_order(session, req, now=EVENING)

    assert again.order_id == first.order_id and again.total_cents == 1190


# --- HTTP envelope -----------------------------------------------------------------


@pytest.fixture
def client(engine, tenant_id):
    def override_get_db():
        with Session(engine) as s:
            yield s

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _post(client, tenant_id, query):
    body = {"call_id": str(uuid.uuid4()), "tenant_id": str(tenant_id), "query": query}
    return client.post("/v1/tools/search_menu", json=body, headers=AUTH)


def test_tool_answers_closed_with_a_sentence_and_no_dish(client, session, tenant_id):
    """Over HTTP the clock is the real one: without any window a lunch menu is
    closed at every hour."""
    session.execute(delete(LunchHours).where(LunchHours.tenant_id == tenant_id))
    session.commit()

    body = _post(client, tenant_id, "M vier A").json()

    assert body["ok"] is False and body["error"]["code"] == "closed"
    assert body["say"].startswith("Menü Ente knusprig ist ein Mittagsmenü.")
    assert body.get("data") is None


@pytest.mark.latency
def test_search_stays_fast_with_lunch_menus_on_a_large_menu(client, session, tenant_id):
    """Budget 300 ms p95 (docs/04) with 260 dishes, 60 of them lunch menus:
    the number, the closed lunch menu and the name search with its second
    query past the lunch menus."""
    rows = "".join(
        f"{100 + i};Testgericht Nummer {i} mit Reis;Test;9,90;;ja;nein\n"
        for i in range(200)
    ) + "".join(
        f"M{10 + i};Menü Testgericht Nummer {i} mit Reis;Menü;7,90;;ja;ja\n"
        for i in range(60)
    )
    plan = parse({MENU_FILE: MENU[MENU_FILE] + rows, ALIASES_FILE: MENU[ALIASES_FILE]})
    assert plan.ok, plan.errors
    apply(session, tenant_id, plan, now=LUNCH)

    assert p95_ms(lambda: _post(client, tenant_id, "M vier A")) < 300
    assert p95_ms(lambda: _post(client, tenant_id, "Menü eins")) < 300
    assert p95_ms(lambda: _post(client, tenant_id, "Testgericht Nummer 7")) < 300
    assert p95_ms(lambda: _post(client, tenant_id, "Ente süss sauer")) < 300
