"""Guest remarks are plain notes the model writes (Maxi, 15.11.2026, #736, #763).

A remark on a dish goes on its line, one for the whole order on the order, and
an allergy is written by the model as well ("WICHTIG !!! Kunde ist allergisch
auf Nüsse"). The core asks no consent question and reads no allergy list; it
keeps one safety net: a guest who named an allergy gets no draft without a
note that names it, the first one goes back to the model once per turn
(`guards.unnoted_allergy`). Allergen answers stay with the database (L17).
Caller sentences in German: the core parses German.
"""

from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from api.agent import guards
from api.agent.escalation import names_an_allergy
from api.agent.llm import LLMTurn, ToolCall
from api.agent.loop import SAY_ALLERGENS_BEYOND
from api.agent.state import ConversationState
from api.domain.menu import search_menu
from api.domain.menu.importer import apply, parse
from api.models import Call, Order, OrderItem, Reservation
from api.tests.test_agent_allergens_beyond import CALLER, run, state_for
from api.tests.test_domain_draft_order import KARTE, NOW, _tenant, item

ALLERGY_NOTE = "WICHTIG !!! Kunde ist allergisch auf Nüsse"


@pytest.fixture
def session(migrated_db_url):
    engine = create_engine(migrated_db_url)
    with Session(engine) as s:
        yield s
    engine.dispose()


@pytest.fixture
def known_tenant(session):
    tenant_id = _tenant(session, "Testbetrieb")
    plan = parse(KARTE)
    assert plan.ok, plan.errors
    apply(session, tenant_id, plan, now=NOW)
    return tenant_id


def draft(session, tenant, line_note=None, order_note=None) -> LLMTurn:
    line = {"menu_item_id": str(item(session, tenant, "13")), "quantity": 1}
    if line_note is not None:
        line["note"] = line_note
    args = {
        "type": "pickup",
        "customer": {"name": "Müller", "phone": CALLER},
        "items": [line],
    }
    if order_note is not None:
        args["note"] = order_note
    return LLMTurn(tool_call=ToolCall("draft_order", args))


def search() -> LLMTurn:
    return LLMTurn(tool_call=ToolCall("search_menu", {"query": "Pho Bo"}))


def stored(session) -> tuple[str | None, list[str | None]]:
    [order] = session.scalars(select(Order)).all()
    lines = session.scalars(
        select(OrderItem.note).where(OrderItem.order_id == order.id)
    ).all()
    return order.note, list(lines)


def drafts_logged(session, state) -> list[str | None]:
    call = session.get(Call, state.call_id)
    session.refresh(call)
    return [
        entry.get("error_code")
        for entry in call.tool_calls
        if entry["name"] == "draft_order"
    ]


def test_a_remark_on_a_dish_goes_on_its_line(session, known_tenant):
    state = state_for(session, known_tenant, phone=CALLER)

    run(
        session,
        state,
        "Einmal die Pho Bo auf Müller, bitte nicht scharf.",
        search(),
        draft(session, known_tenant, line_note="bitte nicht scharf"),
        LLMTurn(say="Passt das so?"),
    )

    assert stored(session) == (None, ["bitte nicht scharf"])


def test_a_remark_for_the_whole_order_goes_on_the_order(session, known_tenant):
    state = state_for(session, known_tenant, phone=CALLER)

    run(
        session,
        state,
        "Einmal die Pho Bo auf Müller, bitte alles vegan halten.",
        search(),
        draft(session, known_tenant, order_note="bitte alles vegan halten"),
        LLMTurn(say="Passt das so?"),
    )

    assert stored(session) == ("bitte alles vegan halten", [None])


@pytest.mark.parametrize(
    ("line_note", "order_note"),
    [(ALLERGY_NOTE, None), (None, "WICHTIG !!! Allergie (Frau): Nüsse")],
)
def test_the_models_allergy_note_goes_through_unchanged(
    session, known_tenant, line_note, order_note
):
    """No consent question, no rewrite: naming the allergy is the consent
    (Maxi as data protection officer, L13)."""
    state = state_for(session, known_tenant, phone=CALLER)

    result, _ = run(
        session,
        state,
        "Einmal die Pho Bo auf Müller, ich habe eine Nussallergie.",
        search(),
        draft(session, known_tenant, line_note=line_note, order_note=order_note),
        LLMTurn(say="Passt das so?"),
    )

    assert result.say == ["Passt das so?"]
    assert stored(session) == (order_note, [line_note])
    assert drafts_logged(session, state) == [None]


def test_a_draft_without_the_named_allergy_goes_back_once(session, known_tenant):
    state = state_for(session, known_tenant, phone=CALLER)

    run(
        session,
        state,
        "Einmal die Pho Bo auf Müller, ich habe eine Nussallergie.",
        search(),
        draft(session, known_tenant, line_note="ohne Zwiebeln"),
        draft(session, known_tenant, line_note=f"{ALLERGY_NOTE}. Ohne Zwiebeln"),
        LLMTurn(say="Passt das so?"),
    )

    assert drafts_logged(session, state) == ["invalid_input", None]
    assert stored(session) == (None, [f"{ALLERGY_NOTE}. Ohne Zwiebeln"])


def test_a_second_draft_without_it_goes_through_never_into_a_loop(
    session, known_tenant
):
    state = state_for(session, known_tenant, phone=CALLER)

    result, _ = run(
        session,
        state,
        "Einmal die Pho Bo auf Müller, ich habe eine Nussallergie.",
        search(),
        draft(session, known_tenant),
        draft(session, known_tenant),
        LLMTurn(say="Passt das so?"),
    )

    assert result.say == ["Passt das so?"]
    assert drafts_logged(session, state) == ["invalid_input", None]
    assert stored(session) == (None, [None])


def test_an_allergy_said_in_an_earlier_turn_still_arms_it(session, known_tenant):
    state = state_for(session, known_tenant, phone=CALLER)
    run(session, state, "Ich habe eine Nussallergie.", LLMTurn(say="Notiert."))

    run(
        session,
        state,
        "Einmal die Pho Bo auf Müller.",
        search(),
        draft(session, known_tenant),
        draft(session, known_tenant, line_note=ALLERGY_NOTE),
        LLMTurn(say="Passt das so?"),
    )

    assert drafts_logged(session, state) == ["invalid_input", None]


@pytest.mark.parametrize(
    "said",
    [
        "Einmal die Pho Bo auf Müller, ich habe keine Allergie.",
        "Einmal die Pho Bo auf Müller, ich bin nicht allergisch.",
        "Einmal die Pho Bo auf Müller.",
    ],
)
def test_a_denial_or_no_allergy_does_not_arm_it(session, known_tenant, said):
    state = state_for(session, known_tenant, phone=CALLER)

    run(
        session,
        state,
        said,
        search(),
        draft(session, known_tenant),
        LLMTurn(say="Passt das so?"),
    )

    assert not state.allergy_named
    assert drafts_logged(session, state) == [None]


def test_a_table_without_the_named_allergy_goes_back_once(session, known_tenant):
    state = state_for(session, known_tenant, phone=CALLER)
    table = {
        "guest_name": "Müller",
        "phone": CALLER,
        "party_size": 2,
        "reserved_for": "2026-09-16T19:00:00+02:00",
    }

    run(
        session,
        state,
        "Ein Tisch für zwei morgen um 19 Uhr auf Müller, ich habe eine Nussallergie.",
        LLMTurn(tool_call=ToolCall("create_reservation", table)),
        LLMTurn(
            tool_call=ToolCall("create_reservation", {**table, "note": ALLERGY_NOTE})
        ),
        LLMTurn(say="Passt das so?"),
    )

    assert session.scalars(select(Reservation.note)).all() == [ALLERGY_NOTE]


@pytest.mark.parametrize(
    "note",
    [
        ALLERGY_NOTE,
        "WICHTIG !!! Allergie (Frau): Sesam",
        "Laktoseintoleranz",
        "verträgt keine Milch, Unverträglichkeit",
    ],
)
def test_a_note_that_names_the_allergy_passes_the_net(note):
    state = _named()
    call = ToolCall("draft_order", {"items": [{"menu_item_id": "x", "note": note}]})
    assert guards.unnoted_allergy(state, call) is None


@pytest.mark.parametrize("note", [None, "ohne Nüsse", "WICHTIG: ohne Nüsse"])
def test_a_note_that_does_not_name_it_is_caught(note):
    """The words are the retention job's: a note that passes is deleted in
    time (L13)."""
    state = _named()
    args = {"items": [{"menu_item_id": "x", "quantity": 1}]}
    if note is not None:
        args["note"] = note
    assert (
        guards.unnoted_allergy(state, ToolCall("draft_order", args))
        is guards.UNNOTED_ALLERGY
    )
    assert guards.unnoted_allergy(state, ToolCall("search_menu", {})) is None


@pytest.mark.parametrize(
    ("said", "named"),
    [
        ("Ich habe eine Nussallergie.", True),
        ("Meine Frau ist allergisch gegen Sesam.", True),
        ("Ich habe eine Laktoseunverträglichkeit.", True),
        ("Ich habe keine Allergie.", False),
        ("Welche Allergene hat die 13?", False),
        ("Die 13 bitte.", False),
    ],
)
def test_what_names_an_allergy(said, named):
    assert names_an_allergy(said) is named


def test_the_claim_guard_still_refuses_a_dish_free_of_an_allergen(
    session, known_tenant
):
    """L17: "Die 13 ist nussfrei" has no database value behind it; the team
    answers, the guest never hears it."""
    state = state_for(session, known_tenant, phone=CALLER)

    result, _ = run(
        session,
        state,
        "Ich habe eine Nussallergie, geht die 13?",
        LLMTurn(say="Die 13 ist nussfrei."),
    )

    assert result.ended
    assert result.say[0] == SAY_ALLERGENS_BEYOND
    assert "nussfrei" not in " ".join(result.say)


def test_a_priced_addition_still_comes_from_the_menu(session, known_tenant):
    """D8: an addition is an option of `item_options`, with its price."""
    found = search_menu(session, known_tenant, "die 47 mit Huhn", now=NOW)

    assert found.wish is not None
    assert (found.wish.kind, found.wish.option, found.wish.price_delta_cents) == (
        "option",
        "Huhn",
        -100,
    )


def _named() -> ConversationState:
    return ConversationState(call_id=uuid4(), tenant_id=uuid4(), allergy_named=True)
