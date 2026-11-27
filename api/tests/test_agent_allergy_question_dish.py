"""The question for an allergy without its ingredient names its dish when
several such dishes wait (docs/24, deep review of chain round 4 item 14).

"Die 23 mit Allergie und die 13 mit Allergie." asked "Worauf genau soll die
Küche achten?" twice: the answers went on the dishes in order, but the guest
never heard which dish each question was for. Whole calls through the
conversation loop; caller sentences in German, the core parses German.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from api.agent.guards import SAY_ALLERGY_NOTED, SAY_ASK_ALLERGY
from api.agent.llm import LLMTurn, ToolCall
from api.domain.menu.importer import apply, parse
from api.tests.test_agent_allergens_beyond import CALLER, run, state_for
from api.tests.test_domain_draft_order import KARTE, NOW, _tenant

TWO = "Die 23 mit Allergie und die 13 mit Allergie."
FOR_23 = "Zur Nummer 23 Frühlingsrollen: Worauf genau soll die Küche achten?"
FOR_13 = "Zur Nummer 13 Pho Bo: Worauf genau soll die Küche achten?"


@pytest.fixture
def session(migrated_db_url):
    engine = create_engine(migrated_db_url)
    with Session(engine) as s:
        yield s
    engine.dispose()


@pytest.fixture
def state(session):
    tenant_id = _tenant(session, "Testbetrieb")
    plan = parse(KARTE)
    assert plan.ok, plan.errors
    apply(session, tenant_id, plan, now=NOW)
    return state_for(session, tenant_id, phone=CALLER)


def searched(said: str) -> LLMTurn:
    return LLMTurn(tool_call=ToolCall("search_menu", {"query": said}))


def ask() -> LLMTurn:
    return LLMTurn(say=SAY_ASK_ALLERGY)


def test_each_question_names_its_dish_in_order(session, state):
    first, _ = run(session, state, TWO, searched(TWO), ask())
    second, _ = run(session, state, "Erdnüsse.", ask())
    third, _ = run(session, state, "Sesam.", LLMTurn(say=SAY_ALLERGY_NOTED))

    assert first.say == [FOR_23]
    assert second.say == [FOR_13]
    assert third.say == [SAY_ALLERGY_NOTED]
    assert state.allergy_dishes == []
    # The answers are kept for the note as before.
    assert state.allergy_said[-2:] == ["Erdnüsse.", "Sesam."]


def test_one_dish_keeps_the_plain_question(session, state):
    said = "Die 23 mit Allergie."
    result, _ = run(session, state, said, searched(said), ask())

    assert result.say == [SAY_ASK_ALLERGY]
    assert state.allergy_dishes == []


def test_a_question_back_hears_the_same_dish_again(session, state):
    run(session, state, TWO, searched(TWO), ask())
    again, _ = run(session, state, "Wie bitte?", ask())
    then, _ = run(session, state, "Gegen Erdnüsse.", ask())

    assert again.say == [FOR_23]
    assert then.say == [FOR_13]


def test_the_sentence_said_again_starts_at_its_first_dish(session, state):
    """A new search of the same sentence sets the dishes anew; the guest's
    words of that turn answer nothing of the old list."""
    run(session, state, TWO, searched(TWO), ask())
    again, _ = run(session, state, TWO, searched(TWO), ask())

    assert again.say == [FOR_23]


def test_a_sentence_without_the_question_ends_the_wait(session, state):
    """The model took both answers at once: a later question names no dish."""
    run(session, state, TWO, searched(TWO), ask())
    run(session, state, "Bei beiden Erdnüsse.", LLMTurn(say=SAY_ALLERGY_NOTED))
    later, _ = run(session, state, "Ich habe noch eine Allergie.", ask())

    assert later.say == [SAY_ASK_ALLERGY]
