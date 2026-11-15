"""Probe area A, #747: the core's allergen sentence only replaces the model's
when the model itself set `allergen_question: true` in the same turn."""

from api.agent.llm import LLMTurn, ToolCall
from api.tests.test_agent_allergens_beyond import (  # noqa: F401
    CALLER,
    known_tenant,
    run,
    session,
    state_for,
)
from api.tests.test_domain_draft_order import item

CORE_SAY = (
    "Laut unseren Angaben enthält Nummer 23 Frühlingsrollen: glutenhaltiges Getreide."
)


def test_lookup_without_the_flag_lets_the_model_name_allergens(session, known_tenant):
    # "Ist in den Frühlingsrollen Gluten?" names no allergy word, so the core
    # does not force the flag (`flag_the_allergy_question`); the model looks
    # the dish up as a plain detail question and reads data.allergens itself.
    state = state_for(session, known_tenant, phone=CALLER)
    result, _ = run(
        session,
        state,
        "Ist in den Frühlingsrollen Gluten?",
        LLMTurn(
            tool_call=ToolCall(
                "get_item_details",
                {
                    "menu_item_id": str(item(session, known_tenant, "23")),
                    "allergen_question": False,
                },
            )
        ),
        LLMTurn(say="Die 23 enthält Gluten und Soja."),
    )
    assert result.say == [CORE_SAY]


def test_follow_up_without_lookup_answers_from_the_model(session, known_tenant):
    # Turn 2 of an allergen talk, elliptic: no allergy word, no lookup.
    state = state_for(session, known_tenant, phone=CALLER)
    run(
        session,
        state,
        "Welche Allergene hat die 23?",
        LLMTurn(
            tool_call=ToolCall(
                "get_item_details",
                {
                    "menu_item_id": str(item(session, known_tenant, "23")),
                    "allergen_question": True,
                },
            )
        ),
        LLMTurn(say="ok"),
    )
    result, _ = run(
        session,
        state,
        "Und ist da auch Soja drin?",
        LLMTurn(say="Ja, die 23 enthält auch Soja."),
    )
    assert result.say != ["Ja, die 23 enthält auch Soja."]
