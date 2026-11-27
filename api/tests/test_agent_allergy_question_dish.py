"""The question for an allergy without its ingredient names the dishes when
several such dishes wait (docs/24, deep review of chain round 4 item 14).

"Die 23 mit Allergie und die 13 mit Allergie." asked "Worauf genau soll die
Küche achten?" twice and never told the guest which dishes it was about. The
core names all waiting dishes in front of the question, never one: the model
writes each answer on its dish, and a core that guessed which one the model
asks about led answers to the wrong dish (`/code-review max` of item 30).
Whole calls through the conversation loop; caller sentences in German, the
core parses German.
"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, update
from sqlalchemy.orm import Session

from api.agent.guards import SAY_ALLERGY_NOTED, SAY_ASK_ALLERGY
from api.agent.llm import LLMTurn, ToolCall
from api.agent.state import ConversationState, _note_search
from api.domain.menu.importer import apply, parse
from api.models import MenuItem
from api.tests.test_agent_allergens_beyond import CALLER, run, state_for
from api.tests.test_domain_draft_order import KARTE, NOW, _tenant

TWO = "Die 23 mit Allergie und die 13 mit Allergie."
BOTH = (
    "Zur Nummer 23 Frühlingsrollen und Nummer 13 Pho Bo: "
    "Worauf genau soll die Küche achten?"
)


@pytest.fixture
def session(migrated_db_url):
    engine = create_engine(migrated_db_url)
    with Session(engine) as s:
        yield s
    engine.dispose()


@pytest.fixture
def tenant(session):
    tenant_id = _tenant(session, "Testbetrieb")
    plan = parse(KARTE)
    assert plan.ok, plan.errors
    apply(session, tenant_id, plan, now=NOW)
    return tenant_id


@pytest.fixture
def state(session, tenant):
    return state_for(session, tenant, phone=CALLER)


def searched(said: str) -> LLMTurn:
    return LLMTurn(tool_call=ToolCall("search_menu", {"query": said}))


def ask() -> LLMTurn:
    return LLMTurn(say=SAY_ASK_ALLERGY)


def test_the_question_names_every_waiting_dish(session, state):
    first, _ = run(session, state, TWO, searched(TWO), ask())
    # One dish answered: the model asks on, the guest hears both dishes
    # again and says which one is still open; the core never guesses.
    second, _ = run(session, state, "Bei der 23 Erdnüsse.", ask())
    third, _ = run(session, state, "Bei der 13 Sesam.", LLMTurn(say=SAY_ALLERGY_NOTED))

    assert first.say == [BOTH]
    assert second.say == [BOTH]
    assert third.say == [SAY_ALLERGY_NOTED]
    assert state.allergy_dishes == []
    assert state.allergy_said[-2:] == ["Bei der 23 Erdnüsse.", "Bei der 13 Sesam."]


def test_one_dish_keeps_the_plain_question(session, state):
    said = "Die 23 mit Allergie."
    result, _ = run(session, state, said, searched(said), ask())

    assert result.say == [SAY_ASK_ALLERGY]
    assert state.allergy_dishes == []


def test_the_model_may_say_the_named_question_itself(session, state):
    """It sees the sentence as its last one and may copy it: the core's own
    sentence, neither refused nor named twice."""
    run(session, state, TWO, searched(TWO), ask())
    again, _ = run(session, state, "Wie bitte?", LLMTurn(say=BOTH))

    assert again.say == [BOTH]


def test_a_later_sentence_with_one_such_dish_ends_the_old_dishes(session, state):
    run(session, state, TWO, searched(TWO), ask())
    said = "Und die 47 mit Allergie."
    later, _ = run(session, state, said, searched(said), ask())

    assert later.say == [SAY_ASK_ALLERGY]


def test_a_dish_that_is_out_is_not_named(session, tenant, state):
    session.execute(
        update(MenuItem)
        .where(MenuItem.tenant_id == tenant, MenuItem.number == "23")
        .values(sold_out_until=datetime(2030, 1, 1, tzinfo=UTC))
    )
    session.commit()
    result, _ = run(session, state, TWO, searched(TWO), ask())

    assert result.say == [SAY_ASK_ALLERGY]
    assert state.allergy_dishes == []


def test_a_sentence_without_the_question_ends_the_wait(session, state):
    """The model took both answers at once: a later question names no dish."""
    run(session, state, TWO, searched(TWO), ask())
    run(session, state, "Bei beiden Erdnüsse.", LLMTurn(say=SAY_ALLERGY_NOTED))
    later, _ = run(session, state, "Ich habe noch eine Allergie.", ask())

    assert later.say == [SAY_ASK_ALLERGY]


def test_a_later_allergy_with_its_ingredient_ends_the_old_dishes(session, state):
    run(session, state, TWO, searched(TWO), ask())
    said = "Die 47 mit Erdnussallergie."
    later, _ = run(session, state, said, searched(said), ask())

    assert later.say == [SAY_ASK_ALLERGY]


def test_an_allergy_that_waits_for_its_dishes_ends_the_old_dishes():
    """#1084: an allergy said with one dish while the order holds another
    leaves the position without a wish; it is a new allergy all the same
    (sweep of the review of item 30)."""
    state = ConversationState(
        call_id=uuid4(),
        tenant_id=uuid4(),
        allergy_dishes=["Nummer 23 Frühlingsrollen", "Nummer 13 Pho Bo"],
    )
    hit = {"menu_item_id": str(uuid4()), "number": "47", "name": "Ente"}
    position = {"match_type": "exact_number", "results": [hit], "wish": None}
    data = {
        "match_type": "positions",
        "positions": [position],
        "allergy": {"text": "mit Allergie", "kind": "allergy"},
    }

    _note_search(state, data)

    assert state.allergy_dishes == []


def test_a_copy_in_another_case_is_not_named_twice(session, state):
    run(session, state, TWO, searched(TWO), ask())
    copy = "zur" + BOTH.removeprefix("Zur")
    again, _ = run(session, state, "Wie bitte?", LLMTurn(say=copy))

    assert again.say == [copy]


def test_the_plain_question_in_another_case_is_named(session, state):
    """Codex PR #1096: the core takes the question in any case."""
    lower = SAY_ASK_ALLERGY[0].lower() + SAY_ASK_ALLERGY[1:]
    first, _ = run(session, state, TWO, searched(TWO), LLMTurn(say=lower))

    assert first.say == [BOTH]


def test_a_copy_in_another_case_keeps_the_dishes(session, state):
    """Codex PR #1096: the next question still names the dishes."""
    run(session, state, TWO, searched(TWO), ask())
    run(session, state, "Wie bitte?", LLMTurn(say=BOTH.lower()))
    then, _ = run(session, state, "Bei der 23 Erdnüsse.", ask())

    assert then.say == [BOTH]
