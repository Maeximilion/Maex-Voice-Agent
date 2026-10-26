"""An allergen question beyond the maintained values goes to the team before any confirm (T-11.1, L17).

A phone order is distance selling: allergen information has to be available
before the contract (legal check L17). What the database does not hold (no
maintained value, traces, cross-contact, a promise of safety) is answered by a
team member, or by a callback when nobody answers (D24). The core decides it,
before the model or right after `get_item_details`, and the call ends without
a `confirm`. Caller sentences in German: the detector reads German.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from api.agent.escalation import asks_beyond_the_allergens
from api.agent.llm import FakeLLM, LLMTurn, ToolCall
from api.agent.loop import (
    SAY_NOT_A_PERSON,
    SAY_ORDER_NOT_PLACED,
    SUMMARY_ALLERGENS,
    ConversationLoop,
)
from api.agent.state import ConversationState
from api.domain.menu.details import SAY_ALLERGENS_UNKNOWN
from api.domain.menu.importer import ALLERGENS_FILE, apply, parse
from api.models import Call, Callback, Order
from api.tests.test_domain_draft_order import KARTE, NOW, _call, _tenant, item

BERLIN = ZoneInfo("Europe/Berlin")
CLOSED = datetime(2026, 9, 15, 8, 0, tzinfo=BERLIN)  # before opening: no team
CALLER = "+4972215551234"


@pytest.mark.parametrize(
    "said",
    [
        "Sind da Spuren von Erdnüssen drin?",
        "Kann da eine Kontamination mit Nüssen sein?",
        "Kommt das mit Sesam in Kontakt?",
        "Wird das in derselben Fritteuse gemacht?",
        "Wird das im selben Öl frittiert?",
        "Haben Sie eine eigene Fritteuse?",
        "Wird das zusammen mit den Garnelen frittiert?",
        "Ich bin allergisch gegen Gluten, ist das wirklich sicher?",
        "Können Sie garantieren, dass keine Allergene drin sind?",
    ],
)
def test_traces_contact_and_a_promise_of_safety_are_beyond(said):
    assert asks_beyond_the_allergens(said, allergy_talk=False)


def test_certainty_after_talk_of_allergies_is_beyond():
    assert asks_beyond_the_allergens("Ist das wirklich sicher?", allergy_talk=True)
    assert asks_beyond_the_allergens("Hundertprozentig?", allergy_talk=True)


@pytest.mark.parametrize(
    "said",
    [
        # Certainty in a call that never spoke of allergies is about something else.
        "Ist das wirklich sicher bis sieben fertig?",
        "Ganz sicher, auf den Namen Müller.",
        # A question the database answers.
        "Ist in den Frühlingsrollen Gluten?",
        "Welche Allergene hat die 23?",
        "Ich habe eine Erdnussallergie.",
        # "Kontakt" alone is no contact with food.
        "Brauchen Sie meine Kontaktdaten?",
        # A fried dish ordered by its name (Codex PR #369): the eval menu
        # has "21;Wan-Tan frittiert".
        "Ich hätte gern Wantan frittiert.",
        "Einmal die 21, Wan-Tan frittiert, bitte.",
        "Ist was Frittiertes dabei?",
    ],
)
def test_ordinary_sentences_are_not_beyond(said):
    assert not asks_beyond_the_allergens(said, allergy_talk=False)


@pytest.fixture
def session(migrated_db_url):
    engine = create_engine(migrated_db_url)
    with Session(engine) as s:
        yield s
    engine.dispose()


@pytest.fixture
def known_tenant(session):
    """The test menu with confirmed allergens for the 23."""
    tenant_id = _tenant(session, "Testbetrieb")
    card = {**KARTE, ALLERGENS_FILE: "number;allergen_codes;confirmed_by\n23;A;Maxi\n"}
    plan = parse(card)
    assert plan.ok, plan.errors
    apply(session, tenant_id, plan, now=NOW)
    return tenant_id


def state_for(session, tenant_id, **slots) -> ConversationState:
    return ConversationState(
        call_id=_call(session, tenant_id),
        tenant_id=tenant_id,
        caller_phone=CALLER,
        slots=dict(slots),
    )


def details(session, tenant_id, number: str) -> LLMTurn:
    return LLMTurn(
        tool_call=ToolCall(
            "get_item_details",
            {
                "menu_item_id": str(item(session, tenant_id, number)),
                "allergen_question": True,
            },
        )
    )


def run(session, state, said, *turns, now=NOW):
    llm = FakeLLM(list(turns))
    result = ConversationLoop(session, llm, "system", now=now).run_turn(state, said)
    return result, llm


def test_an_unmaintained_allergen_value_goes_to_the_team_at_once(session, known_tenant):
    state = state_for(session, known_tenant, phone=CALLER)

    result, llm = run(
        session,
        state,
        "Ist in der Pho Bo Sellerie?",
        details(session, known_tenant, "13"),
    )

    # The model is not asked again: the core says it and hands over.
    assert len(llm.calls) == 1
    assert result.ended
    assert result.say[0] == SAY_ALLERGENS_UNKNOWN
    assert state.stage == "transferred"
    call = session.get(Call, state.call_id)
    session.refresh(call)
    assert call.transfer_reason == "out_of_scope"


def test_without_a_team_the_card_names_the_question_and_the_dish(session, known_tenant):
    state = state_for(session, known_tenant, phone=CALLER)

    result, _ = run(
        session,
        state,
        "Ist in der Pho Bo Sellerie? Ich habe eine Sellerieallergie.",
        details(session, known_tenant, "13"),
        now=CLOSED,
    )

    assert result.ended and state.stage == "callback"
    callback = session.scalars(select(Callback)).one()
    assert callback.reason == "out_of_scope"
    assert callback.summary.startswith(SUMMARY_ALLERGENS)
    assert "13 Pho Bo" in callback.summary
    # The question, never the guest's allergy (L13).
    assert "Sellerie" not in callback.summary


def test_a_maintained_value_is_answered_and_the_call_goes_on(session, known_tenant):
    state = state_for(session, known_tenant)

    result, llm = run(
        session,
        state,
        "Ist in den Frühlingsrollen Gluten?",
        details(session, known_tenant, "23"),
        LLMTurn(say="Die Frühlingsrollen enthalten laut unseren Angaben Gluten."),
    )

    assert not result.ended
    assert len(llm.calls) == 2
    assert state.allergy_talk
    assert session.scalars(select(Callback)).all() == []


def test_a_promise_of_safety_after_the_allergens_goes_to_the_team(
    session, known_tenant
):
    state = state_for(session, known_tenant, phone=CALLER)
    run(
        session,
        state,
        "Ist in den Frühlingsrollen Gluten?",
        details(session, known_tenant, "23"),
        LLMTurn(say="Die Frühlingsrollen enthalten laut unseren Angaben Gluten."),
    )

    # Never shown to the model: no turn is prepared for it.
    result, llm = run(session, state, "Und die Pho Bo, ist die wirklich sicher?")

    assert llm.calls == []
    assert result.ended
    assert result.say[0] == SAY_ALLERGENS_UNKNOWN


def test_an_open_order_is_not_placed_and_goes_onto_the_card(session, known_tenant):
    state = state_for(session, known_tenant, phone=CALLER)
    pho = str(item(session, known_tenant, "13"))
    run(
        session,
        state,
        "Einmal die Pho Bo auf Müller.",
        LLMTurn(tool_call=ToolCall("search_menu", {"query": "Pho Bo"})),
        LLMTurn(
            tool_call=ToolCall(
                "draft_order",
                {
                    "type": "pickup",
                    "customer": {"name": "Müller", "phone": CALLER},
                    "items": [{"menu_item_id": pho, "quantity": 1}],
                },
            )
        ),
        LLMTurn(say="Passt das so?"),
    )
    assert state.stage == "readback_pending"

    result, _ = run(session, state, "Sind da Spuren von Erdnüssen drin?", now=CLOSED)

    assert result.ended
    assert result.say[:2] == [SAY_ALLERGENS_UNKNOWN, SAY_ORDER_NOT_PLACED]
    assert session.scalars(select(Order)).one().status == "draft"
    callback = session.scalars(select(Callback)).one()
    assert "Offene Bestellung, nicht aufgegeben: 1x 13 Pho Bo" in callback.summary
    assert "Erdnüss" not in callback.summary


def test_traces_asked_instead_of_the_consent_answer_go_to_the_team(
    session,
    known_tenant,
):
    """The consent question waits with a draft; the guest asks about traces
    instead. The question goes to the team, nothing is stored, and the
    consent question is not put again."""
    state = state_for(session, known_tenant, phone=CALLER)
    table = ToolCall(
        "create_reservation",
        {
            "guest_name": "Müller",
            "phone": CALLER,
            "party_size": 2,
            "reserved_for": "2026-09-16T19:00:00+02:00",
            "note": "WICHTIG: Keine Erdnuss. Grund: Allergie",
        },
    )
    run(
        session,
        state,
        "Tisch für zwei auf Müller, Erdnussallergie",
        LLMTurn(tool_call=table),
    )
    assert state.held_call is not None

    result, _ = run(session, state, "Gibt es bei Ihnen Spuren von Nüssen?", now=CLOSED)

    assert result.ended
    assert result.say[0] == SAY_ALLERGENS_UNKNOWN
    assert session.scalars(select(Callback)).one().reason == "out_of_scope"


def test_a_human_question_next_to_traces_hears_the_truth_first(session, known_tenant):
    """Merge of T-11.5 into T-11.1: the question for traces goes to the team
    before the model (L17); a "Sind Sie ein Mensch?" in the same sentence still
    gets the truthful answer first, as on every handoff (docs/09 L1)."""
    state = state_for(session, known_tenant, phone=CALLER)

    result, llm = run(
        session, state, "Sind Sie ein Mensch? Sind da Spuren von Erdnüssen drin?"
    )

    assert llm.calls == []
    assert result.say[0] == SAY_NOT_A_PERSON
    assert result.say[1] == SAY_ALLERGENS_UNKNOWN
