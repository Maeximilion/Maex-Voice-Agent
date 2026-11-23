"""An allergen question beyond the maintained values goes to the team before any confirm (T-11.1, L17).

A phone order is distance selling: allergen information has to be available
before the contract (legal check L17). What the database does not hold (no
maintained value, traces, cross-contact, a promise of safety) is answered by a
team member, or by a callback when nobody answers (D24). The core decides it,
before the model or right after `get_item_details`, and the call ends without
a `confirm`. Caller sentences in German: the detector reads German.
"""

import json
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import Session

from api.agent import guards
from api.agent.escalation import (
    asks_about_allergies,
    asks_beyond_the_allergens,
    names_an_additive,
)
from api.agent.guards import note_said
from api.agent.llm import FakeLLM, LLMTurn, ToolCall
from api.agent.loop import (
    MAX_TOOL_HOPS,
    SAY_ALLERGENS_BEYOND,
    SAY_CALLBACK_NUMBER,
    SAY_NOBODY_REACHABLE,
    SAY_NOT_A_PERSON,
    SAY_ORDER_NOT_PLACED,
    SAY_STUCK,
    SAY_TABLE_NOT_BOOKED,
    SUMMARY_ADDITIVES,
    SUMMARY_ALLERGENS,
    SUMMARY_NOTE_TO_CLEAR,
    ConversationLoop,
)
from api.agent.state import ConversationState
from api.domain.callbacks.transfer import SUMMARY_MISSED, team_missed
from api.domain.menu.details import SAY_ALLERGENS_UNKNOWN
from api.domain.menu.importer import (
    ALLERGENS_FILE,
    MENU_FILE,
    OPTIONS_FILE,
    apply,
    parse,
)
from api.domain.menu.search import search_menu
from api.domain.menu.wishes import only_denies
from api.models import Call, Callback, MenuItem, Order, OrderItem
from api.tests.test_domain_draft_order import KARTE, NOW, _call, _tenant, item

BERLIN = ZoneInfo("Europe/Berlin")
CLOSED = datetime(2026, 9, 15, 8, 0, tzinfo=BERLIN)  # before opening: no team
CALLER = "+4972215551234"


@pytest.mark.parametrize(
    "said",
    [
        "Sind da Spuren von Erdnüssen drin?",
        "Kann da eine Kontamination mit Nüssen sein?",
        "Wird das in derselben Fritteuse gemacht?",
        "Wird das im selben Öl frittiert?",
        "Haben Sie eine eigene Fritteuse?",
        "Wird das zusammen mit den Garnelen frittiert?",
        # Own review of PR #369: the forms the list missed.
        "Ist das dasselbe Öl wie beim Fleisch?",
        "Wird das in demselben Öl frittiert?",
        "Ist das das gleiche Öl?",
        "Haben Sie ein eigenes Öl dafür?",
        "Sind Erdnussspuren drin?",
        "Ist das spurenfrei?",
        "Wird das mit Fisch zusammen frittiert?",
    ],
)
def test_traces_and_shared_frying_are_beyond(said):
    assert asks_beyond_the_allergens(said)


@pytest.mark.parametrize(
    "said",
    [
        # Contact and certainty are read on the model's answer since the
        # redesign of PR #369 (`guards.allergen_claim`), never on the
        # guest's question: as questions, every round found a sentence about
        # payment, persons or time.
        "Kommt das mit Sesam in Kontakt?",
        "Ist das wirklich sicher?",
        "Können Sie garantieren, dass keine Allergene drin sind?",
        "Kann ich berührungslos bezahlen?",
        "Kann ich mit dem Chef in Kontakt kommen?",
        "Ist das wirklich sicher bis sieben fertig?",
        "Ja, ganz sicher.",
        # A question the database answers.
        "Ist in den Frühlingsrollen Gluten?",
        "Welche Allergene hat die 23?",
        "Ich habe eine Erdnussallergie.",
        # A fried dish ordered by its name (Codex PR #369): the eval menu
        # has "21;Wan-Tan frittiert".
        "Ich hätte gern Wantan frittiert.",
        "Einmal die 21, Wan-Tan frittiert, bitte.",
        "Ich nehme alles zusammen, einmal die 21, Wan-Tan frittiert.",
        "Ist was Frittiertes dabei?",
        # "extra" is more of it, not separate frying (Codex PR #369).
        "Bitte mit extra Öl.",
        "Die 21 mit extra Pfanne gebraten.",
    ],
)
def test_other_sentences_are_not_beyond(said):
    assert not asks_beyond_the_allergens(said)


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


NOT_ON_THE_CARD = "Sellerie Spezial Quark"
ASKED = "Hallo, ist in der Pho Bo Sellerie? Ich habe eine Allergie."


def searches(query: str = NOT_ON_THE_CARD) -> LLMTurn:
    return LLMTurn(tool_call=ToolCall("search_menu", {"query": query}))


@pytest.mark.parametrize(
    "said",
    [
        ASKED,
        "Welche Allergene hat die 99?",
        "Ist die 99 allergenfrei?",
        "Sind Sie ein Roboter? Ist in der 99 Sellerie? Ich habe eine Allergie.",
        "Ich habe eine Erdnussallergie, ist da Erdnuss drin?",
        # The speech engine dropped the mark: the word order still asks.
        "Ist in der Pho Bo Sellerie ich habe eine Allergie",
    ],
)
def test_a_question_about_an_allergy_is_read_as_one(said):
    assert asks_about_allergies(said)


@pytest.mark.parametrize(
    "said",
    [
        # An allergy told, nothing asked: the core notes it for the kitchen.
        "Die Sellerie Spezial bitte, ich habe eine Erdnussallergie.",
        # A question, but no word of an allergy.
        "Haben Sie Sellerie Spezial?",
        # An allergy denied (PR #445 review).
        "Haben Sie Pizza? Ich habe keine Allergie.",
        "Ich bin nicht allergisch, haben Sie die 99?",
        "Ich habe keine Allergie. Ist da Sellerie drin?",
        # The question mark belongs to something else.
        "Sind Sie ein Roboter? Die 99 bitte, ich habe eine Erdnussallergie.",
        "Haben Sie heute noch auf? Die 99 bitte, ich habe eine Nussallergie.",
        "Hallo? Ich möchte die 99, ich habe eine Nussallergie.",
        # A surname that reads like an allergen names none (audit of 7768182,
        # finding 9; deep review of #860).
        "Kann ich einen Tisch auf den Namen Fischer bekommen? "
        "Ich habe eine Nussallergie.",
    ],
)
def test_other_sentences_are_no_allergy_question(said):
    assert not asks_about_allergies(said)


def test_an_allergy_question_on_a_dish_not_found_goes_to_the_team(
    session, known_tenant
):
    """`allergie_0002` on the real model: it searched the dish with the allergen
    in the query, found nothing and asked for the number, and nobody answered
    the question (L17). The model is not asked again."""
    state = state_for(session, known_tenant, phone=CALLER)

    result, llm = run(session, state, ASKED, searches())

    assert len(llm.calls) == 1
    assert result.ended
    assert result.say[0] == SAY_ALLERGENS_BEYOND
    assert state.stage == "transferred"
    call = session.get(Call, state.call_id)
    session.refresh(call)
    assert call.transfer_reason == "out_of_scope"


def test_that_handover_claims_nothing_about_a_dish_nobody_looked_up(
    session, known_tenant
):
    """The dish may well be maintained: "Pho Bo Sellerie" finds nothing though
    the Pho Bo stands on the card (PR #445 review). "Nichts hinterlegt" would
    be a statement about the database that nobody read."""
    state = state_for(session, known_tenant, phone=CALLER)

    result, _ = run(session, state, ASKED, searches("Pho Bo Sellerie"))

    assert SAY_ALLERGENS_UNKNOWN not in result.say


def test_a_lookup_on_a_question_without_a_mark_goes_through(session, known_tenant):
    """The speech engine drops the mark (T-10.3): the core must not take the
    question for an allergy only told and refuse the lookup (PR #445 review)."""
    state = state_for(session, known_tenant, phone=CALLER)

    result, _ = run(
        session,
        state,
        "Ist in der Pho Bo Sellerie ich habe eine Allergie",
        details(session, known_tenant, "13"),
    )

    assert result.say[0] == SAY_ALLERGENS_UNKNOWN


def test_a_model_that_talks_past_the_question_is_sent_to_the_menu(
    session, known_tenant
):
    """`allergie_0002` in a full run: the model asked for the number without
    any search, although the guest named the dish. Its sentence goes back once
    (PR #445, the trace of 30.10.2026)."""
    state = state_for(session, known_tenant, phone=CALLER)

    result, llm = run(
        session, state, ASKED, LLMTurn(say="Welche Nummer hat das Gericht?"), searches()
    )

    assert len(llm.calls) == 2
    assert result.ended
    assert result.say[0] == SAY_ALLERGENS_BEYOND


def test_a_model_that_looks_up_after_that_answers_from_the_menu(session, known_tenant):
    state = state_for(session, known_tenant, phone=CALLER)

    result, llm = run(
        session,
        state,
        ASKED,
        LLMTurn(say="Welche Nummer hat das Gericht?"),
        searches("Pho Bo"),
        details(session, known_tenant, "13"),
    )

    assert len(llm.calls) == 3
    assert result.say[0] == SAY_ALLERGENS_UNKNOWN


def test_a_model_that_talks_past_it_twice_gets_the_offer_said(session, known_tenant):
    """#798 row 4: the guest's allergy opened the turn, so the model's own
    sentence goes back once; the second time the guest hears the offer to look
    a dish up, and the question stays pending for one turn."""
    state = state_for(session, known_tenant, phone=CALLER)

    result, llm = run(
        session,
        state,
        ASKED,
        LLMTurn(say="Welche Nummer hat das Gericht?"),
        LLMTurn(say="Nennen Sie mir bitte die Nummer."),
    )

    assert len(llm.calls) == 2
    assert not result.ended
    assert result.say == [guards.SAY_ALLERGENS_OFFER]
    assert state.allergen_question_open


def test_a_sentence_that_names_an_allergy_hears_only_core_sentences(
    session, known_tenant
):
    """#798, question 1 (A): nothing tells "Ich habe eine Erdnussallergie"
    next to a question from one about a dish without reading grammar, so the
    model's own answer waits one exchange; the guest hears the noted sentence
    and is not handed over."""
    state = state_for(session, known_tenant, phone=CALLER)

    result, llm = run(
        session,
        state,
        "Haben Sie heute noch auf? Ich habe eine Erdnussallergie.",
        LLMTurn(say="Ja, bis 22 Uhr."),
        LLMTurn(say=guards.SAY_ALLERGY_NOTED),
    )

    assert len(llm.calls) == 2
    assert not result.ended
    assert result.say == [guards.SAY_ALLERGY_NOTED]


def test_the_card_of_that_question_leaves_out_what_was_searched(session, known_tenant):
    """The search words can hold the guest's allergy, and the card never does
    (L13)."""
    state = state_for(session, known_tenant, phone=CALLER)

    run(
        session,
        state,
        "Ist in der Sellerie Spezial Erdnuss? Ich habe eine Erdnussallergie.",
        searches("Sellerie Spezial Erdnuss"),
    )

    assert state.handoff_summary is not None
    # The kitchen marker leads every card of an allergic guest (#940).
    assert state.handoff_summary.startswith(
        f"{SUMMARY_NOTE_TO_CLEAR}. {SUMMARY_ALLERGENS}"
    )
    assert "Erdnuss" not in state.handoff_summary


def test_with_nobody_there_that_question_asks_for_a_number(session, known_tenant):
    state = state_for(session, known_tenant)

    result, _ = run(session, state, ASKED, searches(), now=CLOSED)

    assert not result.ended
    assert SAY_CALLBACK_NUMBER in result.say


def test_a_question_on_several_dishes_none_found_goes_to_the_team(
    session, known_tenant
):
    """A sentence with several positions comes back ok, with the `not_found` in
    each part (`dispatch._search_menu`, PR #445 review)."""
    state = state_for(session, known_tenant, phone=CALLER)

    result, llm = run(
        session,
        state,
        "Ist in der 98 und der 99 Sellerie? Ich habe eine Allergie.",
        searches("die 98 und die 99"),
    )

    assert len(llm.calls) == 1
    assert result.ended


@pytest.mark.parametrize(
    "said, query",
    [
        # Nothing of what these say is an allergy question.
        ("Die Sellerie Spezial bitte, ich habe eine Erdnussallergie.", NOT_ON_THE_CARD),
        ("Haben Sie Pizza? Ich habe keine Allergie.", "Pizza"),
        # A dish was found: its allergens are there to look up.
        (ASKED, "Pho Bo"),
        # One part found: the model can look that one up.
        (
            "Ist in der 99 und der Pho Bo Sellerie? Ich habe eine Allergie.",
            "die 99 und Pho Bo",
        ),
    ],
)
def test_other_searches_stay_with_the_model(session, known_tenant, said, query):
    """Each sentence speaks of an allergy, so the model's own question is not
    heard (#798); its fixed question is, and nobody hands over."""
    state = state_for(session, known_tenant, phone=CALLER)

    result, llm = run(
        session,
        state,
        said,
        searches(query),
        LLMTurn(say=guards.SAY_ASK_ALLERGY),
    )

    assert len(llm.calls) == 2
    assert not result.ended


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
    assert callback.summary.startswith(f"{SUMMARY_NOTE_TO_CLEAR}. {SUMMARY_ALLERGENS}")
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


CORE_SAY = (
    "Laut unseren Angaben enthält Nummer 23 Frühlingsrollen: glutenhaltiges Getreide."
)


@pytest.mark.parametrize(
    "answer",
    [
        # Nothing wrong with the claim for a guard to catch: a letter dropped
        # or one added (#717), a name other than the table's (#669).
        "Die 23 enthält Gluten und Soja.",
        "Die 23 enthält Gluten, Erdnüsse und Soja.",
        "Die 23 enthält Weizen.",
        "In den Frühlingsrollen ist Gluten.",
        CORE_SAY,
    ],
)
def test_the_guest_hears_the_core_sentence_for_a_maintained_value(
    session, known_tenant, answer
):
    """#669, #717 (rule 1): the allergens a guest hears come from the table of
    the core, whatever the model makes of the result."""
    state = state_for(session, known_tenant, phone=CALLER)

    result, _ = run(
        session,
        state,
        "Ist in den Frühlingsrollen Gluten?",
        details(session, known_tenant, "23"),
        LLMTurn(say=answer),
    )

    assert not result.ended
    assert result.say == [CORE_SAY]
    assert state.last_said == CORE_SAY


def test_each_dish_looked_up_in_a_turn_gets_its_sentence(session):
    tenant_id = _tenant(session, "Testbetrieb")
    csv = "number;allergen_codes;confirmed_by\n23;A;Maxi\n13;F,L;Maxi\n"
    plan = parse({**KARTE, ALLERGENS_FILE: csv})
    assert plan.ok, plan.errors
    apply(session, tenant_id, plan, now=NOW)
    state = state_for(session, tenant_id, phone=CALLER)

    result, _ = run(
        session,
        state,
        "Was ist in den Frühlingsrollen und in der Pho Bo?",
        details(session, tenant_id, "23"),
        details(session, tenant_id, "23"),
        details(session, tenant_id, "13"),
        LLMTurn(say="Die Frühlingsrollen enthalten Gluten, die Pho Bo Soja."),
    )

    assert result.say == [
        f"{CORE_SAY} Laut unseren Angaben enthält Nummer 13 Pho Bo: Soja und Sellerie."
    ]


def test_a_sentence_without_a_lookup_in_the_turn_is_left_alone(session, known_tenant):
    state = state_for(session, known_tenant, phone=CALLER)

    result, _ = run(
        session,
        state,
        "Hallo, ich möchte etwas bestellen.",
        LLMTurn(say="Gern, was darf es sein?"),
    )

    assert result.say == ["Gern, was darf es sein?"]


def unflagged(session, tenant_id, number: str) -> LLMTurn:
    """A lookup the model sent as a plain detail question."""
    return LLMTurn(
        tool_call=ToolCall(
            "get_item_details",
            {
                "menu_item_id": str(item(session, tenant_id, number)),
                "allergen_question": False,
            },
        )
    )


@pytest.mark.parametrize(
    "said",
    [
        # No allergy word: the guest only names the allergen (#764).
        "Ist in den Frühlingsrollen Gluten?",
        "Hat die 23 Gluten?",
        "Ist da Gluten drin in der 23",
    ],
)
def test_a_lookup_without_the_flag_still_gives_the_core_sentence(
    session, known_tenant, said
):
    """#764 (rule 1): a question whether an allergen is in a dish is one,
    whatever flag the model set; the guest hears the table of the core."""
    state = state_for(session, known_tenant, phone=CALLER)

    result, _ = run(
        session,
        state,
        said,
        unflagged(session, known_tenant, "23"),
        LLMTurn(say="Die 23 enthält Gluten und Soja."),
    )

    assert result.say == [CORE_SAY]


def test_an_unflagged_lookup_with_no_maintained_value_goes_to_the_team(
    session, known_tenant
):
    state = state_for(session, known_tenant, phone=CALLER)

    result, _ = run(
        session,
        state,
        "Ist in der Pho Bo Sesam?",
        unflagged(session, known_tenant, "13"),
        LLMTurn(say="Die Pho Bo enthält Sesam."),
    )

    assert result.ended
    assert result.say[0] == SAY_ALLERGENS_UNKNOWN


@pytest.mark.parametrize("number", ["23", "13"])
def test_a_model_sentence_naming_allergens_after_any_lookup_is_the_core_s(
    session, known_tenant, number
):
    """A follow-up that names no allergen ("Und die 23?"): the lookup is a
    plain one, and the model's allergens are licensed by nothing of the turn.
    #798 row 6: back once, the second time the table, or the team where the
    table holds nothing."""
    state = state_for(session, known_tenant, phone=CALLER)
    state.allergy_talk = True
    answer = f"Die {number} enthält Gluten und Soja."

    result, llm = run(
        session,
        state,
        f"Und die {number}?",
        unflagged(session, known_tenant, number),
        LLMTurn(say=answer),
        LLMTurn(say=answer),
    )

    assert json.loads(llm.calls[2][2])["hint"] == guards.HINT_UNLICENSED_ALLERGEN
    assert answer not in result.say

    if number == "23":
        assert result.say == [CORE_SAY]
    else:
        assert result.ended
        assert result.say[0] == SAY_ALLERGENS_UNKNOWN


@pytest.mark.parametrize("number", ["23", "13"])
def test_after_a_table_sentence_a_lookup_is_answered_by_the_core_at_once(
    session, known_tenant, number
):
    """Sticky (#798): the last sentence the guest heard was a table sentence,
    so a lookup in the follow-up is an allergen lookup, whatever the model
    says."""
    state = state_for(session, known_tenant, phone=CALLER)
    run(
        session,
        state,
        "Welche Allergene hat die 23?",
        details(session, known_tenant, "23"),
        LLMTurn(say="ok"),
    )
    assert state.last_said == CORE_SAY

    result, llm = run(
        session,
        state,
        f"Und die {number}?",
        unflagged(session, known_tenant, number),
        LLMTurn(say="Ja, die geht auch."),
    )

    assert len(llm.calls) == 2
    if number == "23":
        assert result.say == [CORE_SAY]
    else:
        assert result.ended
        assert result.say[0] == SAY_ALLERGENS_UNKNOWN
        assert state.handoff_summary is not None
        assert "Gericht: 13 Pho Bo." in state.handoff_summary


def test_an_answer_on_options_after_an_unflagged_lookup_reaches_the_guest(
    session, known_tenant
):
    state = state_for(session, known_tenant, phone=CALLER)
    answer = "Zur 23 gibt es eine süß-saure Sauce."

    result, _ = run(
        session,
        state,
        "Welche Sauce gibt es zur 23?",
        unflagged(session, known_tenant, "23"),
        LLMTurn(say=answer),
    )

    assert result.say == [answer]


def test_a_follow_up_without_a_lookup_is_not_answered_by_the_model(
    session, known_tenant
):
    """#764: turn 2 of an allergen talk asks on without a lookup; the model's
    own "auch Soja" never reaches the guest, the lookup it is sent to does."""
    state = state_for(session, known_tenant, phone=CALLER)
    run(
        session,
        state,
        "Welche Allergene hat die 23?",
        details(session, known_tenant, "23"),
        LLMTurn(say="ok"),
    )

    result, llm = run(
        session,
        state,
        "Und ist da auch Soja drin?",
        LLMTurn(say="Ja, die 23 enthält auch Soja."),
        unflagged(session, known_tenant, "23"),
        LLMTurn(say="Ja, auch Soja."),
    )

    assert len(llm.calls) == 3
    assert result.say == [CORE_SAY]


@pytest.mark.parametrize(
    ("first", "asked", "answer", "pending"),
    [
        # The skeptic's probe of #764: the allergy one turn before, not taken
        # up, and the same allergen left open again (#798).
        (
            "Ich habe eine Erdnussallergie.",
            "Ist in der 13 Erdnuss?",
            "Nein, in der 13 ist keine Erdnuss.",
            True,
        ),
        (
            "Ich habe eine Sojaallergie.",
            "Ist in der 23 Soja?",
            "Die 23 enthält nur Gluten.",
            True,
        ),
        # No allergy at all.
        ("Hallo.", "Ist in der Pho Bo Milch?", "Die Pho Bo enthält Soja.", False),
        # The question names no allergen, the model's answer does.
        ("Hallo.", "Und was ist mit der Pho Bo?", "Die Pho Bo enthält Milch.", False),
        (
            "Hallo.",
            "Und sonst?",
            "Die Frühlingsrollen enthalten laut unseren Angaben Gluten.",
            False,
        ),
    ],
)
def test_a_model_answer_on_allergens_without_a_lookup_never_reaches_the_guest(
    session, known_tenant, first, asked, answer, pending
):
    """#764, #798 rows 4 and 6: back to the model once; the second such answer
    is replaced by the offer to look a dish up, or goes to the team when the
    turn before left a question open (rule 5)."""
    state = state_for(session, known_tenant, phone=CALLER)
    run(session, state, first, LLMTurn(say=guards.SAY_ALLERGY_NOTED))
    assert bool(state.allergen_question_codes) is pending
    # The noted sentence makes no next dish the answer (deep review of #860).
    assert not state.allergen_question_open

    result, _ = run(session, state, asked, LLMTurn(say=answer), LLMTurn(say=answer))

    assert answer not in result.say
    if pending:
        assert result.ended
        assert result.say[0] == SAY_ALLERGENS_BEYOND
    else:
        assert not result.ended
        assert result.say == [guards.SAY_ALLERGENS_OFFER]


@pytest.mark.parametrize(
    "answer",
    [
        "Gern, die 13 ohne Erdnüsse. Noch etwas?",
        "Ihre Erdnussallergie ist notiert. Was darf es sein?",
        "Gern, Nummer 13 Pho Bo. Noch etwas?",
    ],
)
def test_a_wish_with_the_guests_allergy_is_noted_and_never_handed_over(
    session, known_tenant, answer
):
    """#798, question 1 (A): the model's own sentence goes back once with the
    hint; the wish and the allergy in the note of the line, the guest hears
    the noted sentence and nothing stays pending."""
    state = state_for(session, known_tenant, phone=CALLER)
    pho = str(item(session, known_tenant, "13"))
    note = "ohne Erdnüsse, WICHTIG !!! Kunde ist allergisch auf Erdnüsse"

    result, llm = run(
        session,
        state,
        "Die 13 ohne Erdnüsse, ich habe eine Erdnussallergie.",
        searches("13"),
        LLMTurn(say=answer),
        LLMTurn(
            say=guards.SAY_ALLERGY_NOTED,
            cart=[{"menu_item_id": pho, "quantity": 1, "note": note}],
        ),
    )

    assert json.loads(llm.calls[2][2])["hint"] == guards.HINT_ALLERGY_TURN
    assert not result.ended
    assert result.say == [guards.SAY_ALLERGY_NOTED]
    assert not state.allergen_question_open


@pytest.mark.parametrize(
    "answer",
    [
        "Ja, das ist sicher.",
        "Die Pho Bo ist glutenfrei.",
        "Da sind keine Spuren von Nüssen drin.",
        "Das kann ich Ihnen garantieren.",
        # The fixed sentence of the prompt for such a question.
        "Dazu kann ich Ihnen keine verlässliche Auskunft geben.",
    ],
)
def test_a_claim_of_the_model_about_allergens_goes_to_the_team(
    session, known_tenant, answer
):
    """L17, redesign of PR #369: a question for safety or contact goes to the
    model; the core reads its answer. A claim no database value answers, or
    the fixed sentence for such a question, never reaches the guest from the
    model: the core hands the call to the team."""
    state = state_for(session, known_tenant, phone=CALLER)
    run(
        session,
        state,
        "Ist in den Frühlingsrollen Gluten?",
        details(session, known_tenant, "23"),
        LLMTurn(say="Die Frühlingsrollen enthalten laut unseren Angaben Gluten."),
    )

    result, _ = run(
        session, state, "Und die Pho Bo, ist die wirklich sicher?", LLMTurn(say=answer)
    )

    assert result.ended
    assert result.say[0] == SAY_ALLERGENS_BEYOND
    # The claim itself never reaches the guest; the fixed sentence is the
    # core's lead, said once.
    assert result.say.count(answer) == (answer == SAY_ALLERGENS_BEYOND)


@pytest.mark.parametrize(
    "answer",
    [
        # No claim, but a model's sentence on allergens without a lookup goes
        # back since #764 (`test_a_model_answer_on_allergens_without_a_lookup`).
        "Sicher, gern. Was darf es noch sein?",
        "Ist sicher notiert.",
        "Das ist sicher bis sieben fertig.",
        "Die Pho Bo ohne Zwiebeln, gern.",
        # Codex PR #369: absence of something that is no allergen.
        "Die Bestellung enthält keine Getränke.",
        "Der Preis enthält keine Liefergebühr.",
        "Der Tisch ist frei von 18 bis 20 Uhr.",
        "Da ist kein Besteck dabei.",
    ],
)
def test_an_answer_without_a_claim_reaches_the_guest(session, known_tenant, answer):
    state = state_for(session, known_tenant, phone=CALLER)
    state.allergy_talk = True

    result, _ = run(session, state, "Und sonst?", LLMTurn(say=answer))

    assert not result.ended
    assert result.say == [answer]


def test_without_talk_of_allergies_a_promise_is_no_claim(session, known_tenant):
    state = state_for(session, known_tenant, phone=CALLER)

    result, _ = run(
        session,
        state,
        "Ist der Tisch am Fenster frei?",
        LLMTurn(say="Das kann ich Ihnen nicht garantieren, ich notiere den Wunsch."),
    )

    assert not result.ended


def test_an_allergen_named_by_the_guest_makes_it_an_allergy_call(session, known_tenant):
    """ "Ist das glutenfrei?" names no allergy of the guest's own, but an
    allergen: the model's "Ja, das ist glutenfrei" is a claim."""
    state = state_for(session, known_tenant, phone=CALLER)

    result, _ = run(
        session,
        state,
        "Ist die Pho Bo glutenfrei?",
        LLMTurn(say="Ja, die Pho Bo ist glutenfrei."),
    )

    assert result.ended
    assert result.say[0] == SAY_ALLERGENS_BEYOND


def test_an_allergy_note_stays_whatever_the_dish_contains(session, known_tenant):
    """Maxi, 27.10.2026: an allergy note is taken whatever the dish contains;
    the kitchen works with it. The 23 has gluten maintained and no sesame:
    the sesame note the model wrote stays on its line, unchanged and with no
    consent question (Maxi, 15.11.2026)."""
    state = state_for(session, known_tenant, phone=CALLER)
    item_23 = str(item(session, known_tenant, "23"))
    note = "WICHTIG !!! Kunde ist allergisch auf Sesam"
    draft = ToolCall(
        "draft_order",
        {
            "type": "pickup",
            "customer": {"name": "Müller", "phone": CALLER},
            "items": [{"menu_item_id": item_23, "quantity": 1, "note": note}],
        },
    )
    run(
        session,
        state,
        "Die 23 auf Müller, ich habe eine Sesamallergie.",
        LLMTurn(
            tool_call=ToolCall(
                "search_menu", {"query": "Die 23, ich habe eine Sesamallergie"}
            )
        ),
        LLMTurn(tool_call=draft),
        LLMTurn(say=guards.SAY_ALLERGY_NOTED),
    )

    assert state.stage == "readback_pending"
    [order] = session.scalars(select(Order)).all()
    assert session.scalars(
        select(OrderItem.note).where(OrderItem.order_id == order.id)
    ).all() == [note]


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
    assert result.say[:2] == [SAY_ALLERGENS_BEYOND, SAY_ORDER_NOT_PLACED]
    assert session.scalars(select(Order)).one().status == "draft"
    callback = session.scalars(select(Callback)).one()
    assert "Offene Bestellung, nicht aufgegeben: 1x 13 Pho Bo" in callback.summary
    assert "Erdnüss" not in callback.summary


def test_a_noted_allergy_never_reaches_the_card(session, known_tenant):
    """Codex PR #369, P1: the order with the allergy note is open, and a
    traces question goes to a callback card. The card is kept for the period
    of a callback, the allergy only until the night after its order (L13):
    the line names a note to clear, never the allergy."""
    state = state_for(session, known_tenant, phone=CALLER)
    pho = str(item(session, known_tenant, "13"))
    note = "WICHTIG !!! Kunde ist allergisch auf Erdnüsse"
    draft = ToolCall(
        "draft_order",
        {
            "type": "pickup",
            "customer": {"name": "Müller", "phone": CALLER},
            "items": [{"menu_item_id": pho, "quantity": 1, "note": note}],
        },
    )
    run(
        session,
        state,
        "Einmal die Pho Bo auf Müller, ich habe eine Erdnussallergie.",
        LLMTurn(tool_call=ToolCall("search_menu", {"query": "Pho Bo"})),
        LLMTurn(tool_call=draft),
        LLMTurn(say=guards.SAY_ALLERGY_NOTED),
    )
    assert state.stage == "readback_pending"

    result, _ = run(session, state, "Sind da Spuren von Sesam drin?", now=CLOSED)

    assert result.ended
    callback = session.scalars(select(Callback)).one()
    assert f"1x 13 Pho Bo ({SUMMARY_NOTE_TO_CLEAR})" in callback.summary
    assert "allergisch" not in callback.summary
    assert "Erdnüss" not in callback.summary
    assert "WICHTIG" not in callback.summary


def test_a_short_ingredient_of_the_guest_never_reaches_the_handover_card(
    session, known_tenant
):
    """#764 (L13): "Kiwi" said with the allergy one turn before stays off the
    card of a later handover that quotes the guest."""
    state = state_for(session, known_tenant, phone=CALLER)
    run(
        session,
        state,
        "Ich bin allergisch gegen Kiwi.",
        LLMTurn(say=guards.SAY_ALLERGY_NOTED),
        now=CLOSED,
    )

    run(
        session,
        state,
        "Ich möchte bitte mit jemandem sprechen, ist im Obstsalat Kiwi?",
        LLMTurn(tool_call=ToolCall("transfer_to_team", {"reason": "human_requested"})),
        now=CLOSED,
    )

    cards = [c.summary for c in session.scalars(select(Callback)).all()]
    assert cards
    assert not any("Kiwi" in card for card in cards)


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
    assert result.say[1] == SAY_ALLERGENS_BEYOND


def test_without_a_number_the_core_asks_for_one_before_it_gives_up(
    session, known_tenant
):
    """Codex PR #369, P1: a caller who withheld the number asks about traces
    while the team is not reachable. The call ended with neither the team nor
    a callback; before the L17 path the model would have asked for a number.
    The core asks itself, and the answer makes the callback."""
    state = state_for(session, known_tenant)

    result, llm = run(session, state, "Sind da Spuren von Erdnüssen drin?", now=CLOSED)

    assert not result.ended
    assert result.say == [SAY_ALLERGENS_BEYOND, SAY_CALLBACK_NUMBER]
    assert llm.calls == []
    assert state.asked_for_phone
    assert session.scalars(select(Callback)).all() == []

    # The model is never asked: no turn is prepared for it.
    result, llm = run(session, state, "07221 5551234", now=CLOSED)

    assert result.ended
    assert llm.calls == []
    callback = session.scalars(select(Callback)).one()
    assert callback.phone == CALLER
    assert callback.reason == "out_of_scope"
    assert callback.summary.startswith(SUMMARY_ALLERGENS)
    assert "Erdnüss" not in callback.summary


def test_without_a_number_in_the_answer_the_call_ends_honestly(session, known_tenant):
    state = state_for(session, known_tenant)
    run(session, state, "Sind da Spuren von Erdnüssen drin?", now=CLOSED)

    result, llm = run(session, state, "Nein, lieber nicht.", now=CLOSED)

    assert result.ended
    assert result.say == [SAY_NOBODY_REACHABLE]
    assert llm.calls == []
    assert session.scalars(select(Callback)).all() == []


def test_a_human_question_with_the_number_hears_the_truth_first(session, known_tenant):
    """Codex PR #369: the answer to the question for the number may ask
    whether a person is on the line; the truthful sentence comes first, then
    the callback (docs/09 L1)."""
    state = state_for(session, known_tenant)
    run(session, state, "Sind da Spuren von Erdnüssen drin?", now=CLOSED)

    result, _ = run(
        session,
        state,
        "Sind Sie ein Mensch? Meine Nummer ist 07221 5551234.",
        now=CLOSED,
    )

    assert result.ended
    assert result.say[0] == SAY_NOT_A_PERSON
    assert session.scalars(select(Callback)).one().phone == CALLER


def test_the_number_after_a_silence_prompt_still_makes_the_callback(
    session, known_tenant
):
    """Codex PR #369, P1: the phone line's "Sind Sie noch da?" came between
    the question for the number and the answer; the digits were no longer
    read as a number to call, and the call ended without a callback."""
    state = state_for(session, known_tenant)
    run(session, state, "Sind da Spuren von Erdnüssen drin?", now=CLOSED)
    note_said(state, "Sind Sie noch da?")

    result, _ = run(session, state, "07221 5551234", now=CLOSED)

    assert result.ended
    assert session.scalars(select(Callback)).one().phone == CALLER


def test_a_yes_to_a_silence_prompt_puts_the_number_question_again(
    session, known_tenant
):
    state = state_for(session, known_tenant)
    run(session, state, "Sind da Spuren von Erdnüssen drin?", now=CLOSED)
    note_said(state, "Sind Sie noch da?")

    result, llm = run(session, state, "Ja.", now=CLOSED)

    assert not result.ended
    assert result.say == [SAY_CALLBACK_NUMBER]
    assert llm.calls == []
    result, _ = run(session, state, "07221 5551234", now=CLOSED)
    assert result.ended
    assert session.scalars(select(Callback)).one().phone == CALLER


def test_a_table_read_back_is_named_unbooked_on_the_card(session, known_tenant):
    """Own review of PR #369: a table waits for its yes when the guest asks
    about traces. The guest hears that it is not confirmed, and the card
    carries the table, as for an open order."""
    state = state_for(session, known_tenant, phone=CALLER)
    table = ToolCall(
        "create_reservation",
        {
            "guest_name": "Müller",
            "phone": CALLER,
            "party_size": 4,
            "reserved_for": "2026-09-16T19:00:00+02:00",
        },
    )
    run(
        session,
        state,
        "Tisch für vier auf Müller.",
        LLMTurn(tool_call=table),
        LLMTurn(say="Passt das?"),
    )
    assert state.stage == "readback_pending"

    result, _ = run(session, state, "Sind da Spuren von Nüssen im Essen?", now=CLOSED)

    assert result.ended
    assert result.say[:2] == [SAY_ALLERGENS_BEYOND, SAY_TABLE_NOT_BOOKED]
    callback = session.scalars(select(Callback)).one()
    assert (
        "Offene Reservierung, nicht bestätigt: 4 Personen, 16.09.2026 19:00 Uhr, Name Müller"
        in callback.summary
    )


def table_read_back(session, tenant_id) -> ConversationState:
    state = state_for(session, tenant_id, phone=CALLER)
    table = ToolCall(
        "create_reservation",
        {
            "guest_name": "Müller",
            "phone": CALLER,
            "party_size": 4,
            "reserved_for": "2026-09-16T19:00:00+02:00",
        },
    )
    run(
        session,
        state,
        "Tisch für vier auf Müller.",
        LLMTurn(tool_call=table),
        LLMTurn(say="Passt das?"),
    )
    assert state.stage == "readback_pending"
    return state


@pytest.mark.parametrize(
    "said, turns",
    [
        ("Moment, ist in der Sellerie Spezial Erdnuss? Ich habe eine Allergie.", 1),
        ("Moment, welche Allergene hat die Pho Bo?", 2),
    ],
)
def test_a_search_leaves_the_table_on_the_card(session, known_tenant, said, turns):
    """A search takes the read-back back before the handover, and the table has
    to reach the team all the same (PR #445 review): the guest heard it and may
    take it for booked. Through a dish not found and through a dish without
    maintained values."""
    state = table_read_back(session, known_tenant)
    model = (
        [searches()]
        if turns == 1
        else [searches("Pho Bo"), details(session, known_tenant, "13")]
    )

    result, _ = run(session, state, said, *model, now=CLOSED)

    assert SAY_TABLE_NOT_BOOKED in result.say
    callback = session.scalars(select(Callback)).one()
    assert "Offene Reservierung, nicht bestätigt: 4 Personen" in callback.summary


def test_a_search_that_finds_nothing_leaves_the_call_a_table_call(
    session, known_tenant
):
    """A failed search must not count as the guest changing their mind: the
    read-back and the intent stay, the call row says reservation (PR #445
    review)."""
    state = table_read_back(session, known_tenant)

    run(
        session,
        state,
        "Welche Allergene hat die Sellerie Spezial?",
        searches(),
        now=CLOSED,
    )

    assert state.intent == "reservation"


def test_a_wait_for_the_number_is_asked_once_more(session, known_tenant):
    """Own review of PR #369: "Moment, ich muss kurz nachschauen" after the
    question for the number ended the call; it is asked once more."""
    state = state_for(session, known_tenant)
    run(session, state, "Sind da Spuren von Erdnüssen drin?", now=CLOSED)

    result, _ = run(session, state, "Moment, ich muss kurz nachschauen.", now=CLOSED)
    assert result.say == [SAY_CALLBACK_NUMBER]
    result, _ = run(session, state, "07221 5551234", now=CLOSED)

    assert result.ended
    assert session.scalars(select(Callback)).one().phone == CALLER


def test_the_number_is_asked_for_at_most_three_times(session, known_tenant):
    state = state_for(session, known_tenant)
    run(session, state, "Sind da Spuren von Erdnüssen drin?", now=CLOSED)

    for _ in range(2):
        result, _ = run(session, state, "Moment.", now=CLOSED)
        assert result.say == [SAY_CALLBACK_NUMBER]
    result, _ = run(session, state, "Moment.", now=CLOSED)

    assert result.ended
    assert result.say == [SAY_NOBODY_REACHABLE]


def test_a_table_read_back_survives_a_new_allergy_in_the_same_turn(
    session, known_tenant
):
    """Codex PR #369: "Ich habe eine Erdnussallergie. Sind da Spuren ...?"
    after the read-back took the draft back before the handover read it, and
    the card had no table. The table is read before."""
    state = state_for(session, known_tenant, phone=CALLER)
    table = ToolCall(
        "create_reservation",
        {
            "guest_name": "Müller",
            "phone": CALLER,
            "party_size": 4,
            "reserved_for": "2026-09-16T19:00:00+02:00",
        },
    )
    run(
        session,
        state,
        "Tisch für vier auf Müller.",
        LLMTurn(tool_call=table),
        LLMTurn(say="Passt das?"),
    )

    result, _ = run(
        session,
        state,
        "Ich habe eine Erdnussallergie. Sind da Spuren von Erdnüssen im Essen?",
        now=CLOSED,
    )

    assert result.say[:2] == [SAY_ALLERGENS_BEYOND, SAY_TABLE_NOT_BOOKED]
    callback = session.scalars(select(Callback)).one()
    assert "Offene Reservierung, nicht bestätigt: 4 Personen" in callback.summary
    assert "Erdnuss" not in callback.summary


@pytest.mark.parametrize("way", ["claim", "unflagged lookup"])
def test_a_handover_on_the_models_answer_names_the_table_a_new_allergy_took_back(
    session, known_tenant, way
):
    """Audit of 7768182, finding 8: the allergy takes the draft back before the
    model answers, so only the table read at the start of the turn is left for
    the card. The claim goes to the team at once; an unflagged lookup in the
    open turn is answered by the table, here the team (#798 row 1)."""
    state = table_read_back(session, known_tenant)
    model = (
        [LLMTurn(say="Ja, das ist sicher.")]
        if way == "claim"
        else [unflagged(session, known_tenant, "13"), LLMTurn(say="Gern.")]
    )

    result, _ = run(
        session,
        state,
        "Ich habe eine Erdnussallergie. Ist in der Pho Bo Erdnuss drin?",
        *model,
        now=CLOSED,
    )

    lead = SAY_ALLERGENS_BEYOND if way == "claim" else SAY_ALLERGENS_UNKNOWN
    assert result.say[:2] == [lead, SAY_TABLE_NOT_BOOKED]
    callback = session.scalars(select(Callback)).one()
    assert "Offene Reservierung, nicht bestätigt: 4 Personen" in callback.summary


def test_the_fixed_sentence_hands_over_in_any_call(session, known_tenant):
    """Codex PR #369: "Kommt das bei der Zubereitung mit anderen Speisen in
    Kontakt?" names no allergen; the model answers with the fixed sentence of
    the prompt, and the core hands over although the call spoke of no
    allergy before."""
    state = state_for(session, known_tenant, phone=CALLER)

    result, _ = run(
        session,
        state,
        "Kommt das bei der Zubereitung mit anderen Speisen in Kontakt?",
        LLMTurn(say=SAY_ALLERGENS_BEYOND),
    )

    assert result.ended
    assert result.say[0] == SAY_ALLERGENS_BEYOND
    assert result.say.count(SAY_ALLERGENS_BEYOND) == 1


def test_an_absence_of_an_allergen_is_never_heard_in_any_call(session, known_tenant):
    """Behaviour change of #798: no longer "the team at once" by a list of
    German forms. Nothing of the turn licenses the nuts: back once, then the
    offer to look the dish up."""
    state = state_for(session, known_tenant, phone=CALLER)
    answer = "Die Pho Bo enthält keine Nüsse."

    result, _ = run(
        session,
        state,
        "Was ist in der Pho Bo?",
        LLMTurn(say=answer),
        LLMTurn(say=answer),
    )

    assert answer not in result.say
    assert not result.ended
    assert result.say == [guards.SAY_ALLERGENS_OFFER]


@pytest.mark.parametrize(
    "said",
    [
        "Die 13, ich habe eine Erdnussallergie.",
        "Die 13, mein Sohn hat eine Erdnussallergie.",
        # The denial next to it: `test_agent_guards.py`,
        # `test_a_denial_next_to_an_order_opens_nothing` (#798).
        "Die 13, ich habe eine Allergie.",
    ],
)
def test_a_stated_allergy_is_no_allergen_question(session, known_tenant, said):
    """Real-model run of 27.10.2026: after "Die 13, ich habe eine
    Erdnussallergie." the model looked up the 13's allergens itself, nothing
    was maintained, and the L17 handover ended the order. A guest who only
    names an allergy asked nothing (Maxi, 27.10.2026: the note stays, the
    kitchen works with it): the lookup is refused, the order goes on."""
    state = state_for(session, known_tenant, phone=CALLER)

    result, _ = run(
        session,
        state,
        said,
        details(session, known_tenant, "13"),
        LLMTurn(say=guards.SAY_ALLERGY_NOTED),
    )

    assert not result.ended
    assert result.say == [guards.SAY_ALLERGY_NOTED]
    assert session.scalars(select(Callback)).all() == []
    assert state.stage != "transferred"


def test_an_allergen_question_with_an_own_allergy_still_goes_to_the_team(
    session, known_tenant
):
    """The question itself keeps L17: asked about the dish, nothing
    maintained, the team answers."""
    state = state_for(session, known_tenant, phone=CALLER)

    result, _ = run(
        session,
        state,
        "Ich habe eine Erdnussallergie, ist in der Pho Bo Erdnuss?",
        details(session, known_tenant, "13"),
    )

    assert result.ended
    assert result.say[0] == SAY_ALLERGENS_UNKNOWN


@pytest.mark.parametrize("flag", [True, "true", 1, "yes"])
def test_the_lookup_flag_is_read_as_the_tool_reads_it(session, known_tenant, flag):
    state = state_for(session, known_tenant, phone=CALLER)
    lookup = ToolCall(
        "get_item_details",
        {
            "menu_item_id": str(item(session, known_tenant, "13")),
            "allergen_question": flag,
        },
    )

    result, _ = run(
        session,
        state,
        "Die 13, ich habe eine Erdnussallergie.",
        LLMTurn(tool_call=lookup),
        LLMTurn(say=guards.SAY_ALLERGY_NOTED),
    )

    assert not result.ended


BOOKED = "Bereits gebucht: Bestellung zur Abholung, Name Schmidt, Code 42."


def _booked_order(session, state) -> None:
    """An order of this call the guest's yes confirmed earlier."""
    session.add(
        Order(
            tenant_id=state.tenant_id,
            call_id=state.call_id,
            type="pickup",
            status="confirmed",
            customer_name="Schmidt",
            phone=CALLER,
            items_total_cents=1380,
            total_cents=1380,
            pickup_code="42",
            idempotency_key=uuid.uuid4().hex,
        )
    )
    session.commit()


def _missed_card(session, state) -> str:
    """The card the line writes when nobody answers the team leg."""
    call = session.get(Call, state.call_id)
    task = team_missed(
        session,
        state.tenant_id,
        call.external_session_id,
        phone=CALLER,
        summary=state.handoff_summary,
        now=NOW + timedelta(minutes=1),
    )
    assert task is not None
    return task.summary


def test_a_live_handover_keeps_the_booking_for_the_line(session, known_tenant):
    """#207: on a live handover the core kept only the allergen card for the
    line; a team leg nobody answered became a card without "Bereits gebucht",
    and the team might take the order again. The booking comes from the
    database when the card is written (#764)."""
    state = state_for(session, known_tenant, phone=CALLER)
    _booked_order(session, state)

    run(
        session,
        state,
        "Ist in der Pho Bo Sellerie?",
        details(session, known_tenant, "13"),
    )

    assert state.stage == "transferred"
    assert state.handoff_summary is not None
    assert state.handoff_summary.startswith(SUMMARY_ALLERGENS)
    assert _missed_card(session, state).endswith(f"{state.handoff_summary} {BOOKED}")


def test_any_live_handover_keeps_the_booking_for_the_line(session, known_tenant):
    state = state_for(session, known_tenant, phone=CALLER)
    _booked_order(session, state)

    run(session, state, "Ich möchte bitte mit einem Menschen sprechen.")

    assert state.stage == "transferred"
    assert state.handoff_summary is not None
    assert _missed_card(session, state).endswith(BOOKED)


def test_the_card_of_a_missed_team_leg_fits_with_a_large_open_order(
    session, known_tenant
):
    """The open order is cut so the line's first sentence and the booking
    still fit a callback card (1000 characters, #207): nothing of the core's
    card is cut away for the booking (#764)."""
    state = state_for(session, known_tenant, phone=CALLER)
    _booked_order(session, state)
    dish = str(item(session, known_tenant, "13"))
    state.seen_items[dish] = {"number": "13", "name": "Pho Bo"}
    state.cart = [
        {"menu_item_id": dish, "quantity": n + 1, "note": f"Wunsch {n} " + "x" * 60}
        for n in range(30)
    ]

    run(
        session,
        state,
        "Ist in der Pho Bo Sellerie?",
        details(session, known_tenant, "13"),
    )

    assert state.handoff_summary is not None
    card = _missed_card(session, state)
    assert card == f"{SUMMARY_MISSED} {state.handoff_summary} {BOOKED}"
    assert len(card) <= 1000


def test_a_wait_for_the_number_is_no_turn_without_understanding(session, known_tenant):
    """Codex PR #437, P1: five turns that left the state as it was, then a
    lookup with nothing maintained while nobody is reachable. The count of
    turns without understanding replaced the question for the callback number
    with the last resort, and the callback was lost."""
    state = state_for(session, known_tenant)
    turns = [LLMTurn(say="Wie bitte?") for _ in range(5)]
    turns.append(details(session, known_tenant, "13"))
    loop = ConversationLoop(session, FakeLLM(turns), "system", now=CLOSED)
    for _ in range(5):
        assert not loop.run_turn(state, "... krrr ...").ended

    result = loop.run_turn(state, "Ist in der Pho Bo Sellerie?")

    assert not result.ended
    assert result.say[-1] == SAY_CALLBACK_NUMBER
    assert state.callback_waits is not None


# The audit of 30.10.2026 on `7768182` (#252): a sentence about allergens
# rests on `get_item_details`, and an allergy said on the phone is never lost.
CLAIM = "Die Nummer 13 ist bei Erdnüssen gut verträglich."


@pytest.mark.parametrize(
    "said",
    [
        "Ja. Ist da Erdnuss drin. Ich habe eine Erdnussallergie.",
        # Behind a comma, naming nothing, and still about the allergy.
        "Ich habe eine Nussallergie, was empfehlen Sie?",
        "Ich habe eine Nussallergie, kann ich die Pho Bo essen?",
        # Without its mark: the word order asks, the allergy stands beside it
        # (Codex on PR #447).
        "Ist das für mich geeignet, ich habe eine Nussallergie",
        "Ist in der Pho Bo was davon drin, ich habe eine Nussallergie",
        "Ist die 13 okay, ich habe eine Nussallergie",
        # "frei" of an allergen.
        "Ich habe eine Nussallergie, ist die Pho Bo frei davon?",
        "Ich habe eine Nussallergie, ist die Pho Bo frei?",
        # A comma of the speech engine inside the question, a digit in it.
        "Ich habe eine Allergie. Ist da, Sellerie drin.",
        "Ist 13 sicher, ich habe eine Erdnussallergie",
        # A word of another topic in a question about the dish.
        "Ich habe eine Nussallergie, was ist in den langen Nudeln drin?",
        "Ich habe eine Nussallergie, was ist in der 13 drin, und wann ist sie fertig?",
        # On the safe side, docs/05 §4: the model has no dish to read, so the
        # call goes to the team. A narrower reading lost real questions.
        "Ich habe eine Nussallergie, haben Sie heute noch geöffnet?",
        "Ich möchte einen Tisch für vier, wir haben eine Nussallergie, haben Sie Platz?",
        "Ist gut, ich habe eine Erdnussallergie.",
        "Ist zwölf, ich habe eine Erdnussallergie.",
    ],
)
def test_these_ask_about_the_allergy(said):
    assert asks_about_allergies(said)


@pytest.mark.parametrize(
    "said",
    [
        "Ich habe eine Nussallergie. Ist die 13 okay für mich",
        "Ist das für mich geeignet. Ich habe eine Nussallergie.",
        # A number that is part of an ingredient keeps the question.
        "Ist die 13 für mich geeignet, ich habe keine Allergie gegen Nüsse, "
        "aber gegen 5-Gewürze-Pulver?",
        "Ist die 13 geeignet, ich habe keine Allergie gegen Milch, "
        "aber gegen alle Nüsse?",
        # Behind a comma, without its mark.
        "Ich habe eine Nussallergie, ist das für mich geeignet",
        "Ich habe eine Nussallergie, ist die 13 okay für mich",
    ],
)
def test_a_question_in_a_sentence_of_its_own_may_look_up(session, known_tenant, said):
    state = state_for(session, known_tenant, phone=CALLER)

    result, _ = run(session, state, said, details(session, known_tenant, "13"))

    assert result.say[0] == SAY_ALLERGENS_UNKNOWN


def test_a_lookup_on_an_allergy_question_is_one_whatever_its_flag(
    session, known_tenant
):
    """Without the flag an unmaintained value would not go to the team, and
    the model would answer from nothing. #798 row 1: the turn is open, so the
    model's sentence after the lookup is not read; the team gets the dish on
    the card."""
    state = state_for(session, known_tenant, phone=CALLER)
    answer = "Nein, in der Pho Bo ist kein Sellerie."

    result, _ = run(
        session,
        state,
        ASKED,
        unflagged(session, known_tenant, "13"),
        LLMTurn(say=answer),
    )

    assert result.ended
    assert result.say[0] == SAY_ALLERGENS_UNKNOWN
    assert answer not in result.say
    assert state.handoff_summary is not None
    assert "Gericht: 13 Pho Bo." in state.handoff_summary


def test_a_refused_lookup_is_no_read_of_the_menu(session, known_tenant):
    """The question is read as one wherever it stands, so the lookup goes
    through and the guest's question is answered from the database."""
    state = state_for(session, known_tenant, phone=CALLER)

    result, _ = run(
        session,
        state,
        "Ja. Ist in der Pho Bo Erdnuss. Ich habe eine Erdnussallergie.",
        details(session, known_tenant, "13"),
    )

    assert result.say[0] == SAY_ALLERGENS_UNKNOWN


def test_a_lookup_that_failed_is_no_read_of_the_menu(session, known_tenant):
    state = state_for(session, known_tenant, phone=CALLER)
    wrong_id = "00000000-0000-0000-0000-000000000000"

    result, llm = run(
        session,
        state,
        ASKED,
        LLMTurn(tool_call=ToolCall("get_item_details", {"item_id": wrong_id})),
        LLMTurn(say=CLAIM),
        LLMTurn(say=CLAIM),
    )

    assert len(llm.calls) == 3
    assert CLAIM not in result.say
    assert result.say == [guards.SAY_ALLERGENS_OFFER]


@pytest.mark.parametrize(
    "answer",
    [
        "Nein, da ist kein Sellerie.",
        "Ja, die Pho Bo enthält Sellerie.",
        # Names nothing, says the same.
        "Da ist nichts davon drin.",
        # A question behind it does not make the first sentence a question.
        "Nein, da ist nichts drin. Möchten Sie die Pho Bo?",
    ],
)
def test_a_search_alone_answers_no_allergy_question(session, known_tenant, answer):
    """A hit carries no allergens (`MenuHit`). #798 row 4: the second answer
    is replaced by the offer to look the dish up."""
    state = state_for(session, known_tenant, phone=CALLER)

    result, llm = run(
        session,
        state,
        ASKED,
        searches("Pho Bo"),
        LLMTurn(say=answer),
        LLMTurn(say=answer),
    )

    assert len(llm.calls) == 3
    assert answer not in result.say
    assert not result.ended
    assert result.say == [guards.SAY_ALLERGENS_OFFER]
    assert state.allergen_question_open


def test_an_unmarked_question_is_not_answered_without_a_lookup(session, known_tenant):
    """Neither by the model, nor is the lookup refused as an allergy only told
    (Codex on PR #447)."""
    state = state_for(session, known_tenant, phone=CALLER)
    said = "Ist das für mich geeignet, ich habe eine Nussallergie"
    answer = "Ja, das können Sie essen."

    result, llm = run(session, state, said, LLMTurn(say=answer), LLMTurn(say=answer))

    assert len(llm.calls) == 2
    assert result.say == [guards.SAY_ALLERGENS_OFFER]

    state = state_for(session, known_tenant, phone=CALLER)
    result, _ = run(session, state, said, details(session, known_tenant, "13"))

    assert result.say[0] == SAY_ALLERGENS_UNKNOWN


def test_after_a_search_the_model_asks_with_the_fixed_question(session, known_tenant):
    """#798 row 2: in an allergy turn the model's own question is not heard;
    the fixed one is."""
    state = state_for(session, known_tenant, phone=CALLER)

    result, llm = run(
        session,
        state,
        ASKED,
        searches("Pho Bo"),
        LLMTurn(say="Meinen Sie die 13 oder die 14?"),
        LLMTurn(say=guards.SAY_ASK_ALLERGY),
    )

    assert len(llm.calls) == 3
    assert json.loads(llm.calls[2][2])["hint"] == guards.HINT_ALLERGY_TURN
    assert result.say == [guards.SAY_ASK_ALLERGY]


def test_the_card_names_the_table_the_guest_moved_to(session, known_tenant):
    """The guest moves a table that was read back and asks about an allergen
    in one turn: the team calls about the 20:00, not the 19:00."""
    state = table_read_back(session, known_tenant)
    moved = ToolCall(
        "create_reservation",
        {
            "guest_name": "Müller",
            "phone": CALLER,
            "party_size": 4,
            "reserved_for": "2026-09-16T20:00:00+02:00",
        },
    )

    result, _ = run(
        session,
        state,
        "Lieber um 20 Uhr. Welche Allergene hat die Pho Bo?",
        LLMTurn(tool_call=moved),
        details(session, known_tenant, "13"),
        now=CLOSED,
    )

    assert SAY_TABLE_NOT_BOOKED in result.say
    summary = session.scalars(select(Callback)).one().summary
    assert "20:00" in summary
    assert "19:00" not in summary


@pytest.mark.parametrize(
    "said",
    [
        "Ich habe keine Allergie gegen Erdnüsse, aber gegen Sesam.",
        "Ich bin nicht allergisch gegen Nüsse, aber gegen Sellerie.",
        "Ich bin nicht gegen Nüsse allergisch, sondern gegen Sellerie.",
        "Keine Allergie gegen Nüsse, nur gegen Sesam.",
        "Ich bin nicht allergisch gegen Nüsse, aber leider gegen Sellerie.",
        "Ich habe keine Allergie gegen Nüsse, allerdings gegen Sesam.",
        "Ich habe keine Allergie gegen Nüsse, jedoch gegen Kiwi.",
        # A number that is part of an ingredient (Codex on PR #449).
        "Ich habe keine Allergie gegen Nüsse, aber gegen 5-Gewürze-Pulver.",
        "Ich habe keine Allergie gegen Milch, aber gegen alle Nüsse.",
        # Behind the ingredient, words that are no time.
        "Ich habe keine Nussallergie, nur gegen Sesam ist es schlimm.",
        "Ich habe keine Allergie, aber gegen Zwiebeln, mir wird schlecht.",
        "Ich habe keine Allergie, sondern gegen Sesam passt das nicht.",
    ],
)
def test_a_mixed_negation_keeps_the_allergy(session, known_tenant, said):
    """What the second half names is an allergy: the call is one in which a
    draft has to note it (`guards.unnoted_allergy`)."""
    state = state_for(session, known_tenant, phone=CALLER)

    run(session, state, said, LLMTurn(say=guards.SAY_ALLERGY_NOTED))

    assert not only_denies(said)
    assert state.allergy_said == [said]


@pytest.mark.parametrize(
    "said",
    [
        "Ich habe keine Allergie, aber bitte ohne Koriander.",
        "Ich bin nicht allergisch.",
        # "gegen" as a time.
        "Wir haben keine Allergien, aber gegen 19 Uhr kommen wir.",
        "Wir haben keine Allergien, aber gegen 19:30 kommen wir.",
        "Wir haben keine Allergien, aber gegen 8, geht das?",
        "Keine Allergie, aber gegen Abend hätte ich gern einen Tisch.",
        "Ich habe keine Allergie, nur gegen acht Uhr wäre gut.",
        "Ich habe keine Allergie gegen Nüsse, aber auch nicht gegen Sesam.",
        "Ich habe keine Allergie gegen Nüsse, aber weder gegen Sesam noch Soja.",
        "Ich habe keine Allergie gegen Nüsse, aber nie gegen Sesam.",
    ],
)
def test_a_plain_denial_stays_one(said):
    assert only_denies(said)


# -- additives: no table holds one (#638, § 5 LMZDV) ---------------------------


@pytest.mark.parametrize(
    "said",
    [
        "Ist da Geschmacksverstärker drin?",
        "Hallo, ist in der Pho Bo Geschmacksverstaerker drin?",
        "Welche Zusatzstoffe hat die 23?",
        "Sind da Süßungsmittel oder E-Nummern drin?",
        # Codex PR #643: no mark from the speech engine, and a word order the
        # yes detector does not know as a question.
        "Sind da Konservierungsstoffe drin",
        "Kochen Sie mit Glutamat",
        # A wish names one as well: read back, it would promise what no table
        # holds.
        "Die 13 ohne Glutamat, bitte.",
        "Können Sie das ohne Farbstoff machen",
        # The register's legend has both (docs/14, #649).
        "Ist im Tiramisu Koffein?",
        "Ist das Tonic chininhaltig?",
    ],
)
def test_a_sentence_that_names_an_additive_is_read_as_one(said):
    assert names_an_additive(said)


@pytest.mark.parametrize(
    "said",
    [
        "Haben Sie Süßkartoffeln?",
        "Ist in der Pho Bo Sellerie?",
        "Welche Nummer hat die Pho Bo?",
        "Meine Nummer ist 0721 5551234.",
        "Einmal die 13 ohne Zwiebeln, bitte.",
        # A street spelled out, its number behind the last letter.
        "Mit L, E, Nummer 12.",
        "mit l e nummer 12",
        # "chinin" inside another word.
        "Haben Sie Zucchininudeln?",
    ],
)
def test_other_sentences_name_no_additive(said):
    assert not names_an_additive(said)


@pytest.mark.parametrize(
    "said",
    [
        "Ist in der Pho Bo Geschmacksverstärker drin?",
        "Sind da Konservierungsstoffe drin",
        "Kochen Sie mit Glutamat",
        "Die 13 ohne Glutamat, bitte.",
    ],
)
def test_a_sentence_about_an_additive_goes_to_the_team_before_the_model(
    session, known_tenant, said
):
    state = state_for(session, known_tenant, phone=CALLER)

    result, llm = run(session, state, said)

    assert llm.calls == []
    assert result.ended and state.stage == "transferred"
    assert result.say[0] == SAY_ALLERGENS_BEYOND
    call = session.get(Call, state.call_id)
    session.refresh(call)
    assert call.transfer_reason == "out_of_scope"


def test_without_a_team_the_card_names_the_additive_question(session, known_tenant):
    state = state_for(session, known_tenant, phone=CALLER)

    result, _ = run(session, state, "Sind da Konservierungsstoffe drin?", now=CLOSED)

    assert result.ended and state.stage == "callback"
    callback = session.scalars(select(Callback)).one()
    assert callback.reason == "out_of_scope"
    assert callback.summary.startswith(SUMMARY_ADDITIVES)


@pytest.mark.parametrize(
    "answer",
    [
        "Da ist kein Glutamat drin.",
        "Die Pho Bo enthält keine Konservierungsstoffe.",
        # Codex PR #643: absence has more forms than a list holds, and a yes
        # is as unfounded as a no. The name of an additive is enough.
        "Die Pho Bo ist glutamatfrei.",
        "Wir bereiten die Pho Bo ohne Glutamat zu.",
        "Ja, da sind Konservierungsstoffe drin.",
        "Die Pho Bo enthält kein Koffein.",
    ],
)
def test_a_claim_of_the_model_about_an_additive_goes_to_the_team(
    session, known_tenant, answer
):
    """A model that brings an additive up itself: its claim that one is
    absent never reaches the guest."""
    state = state_for(session, known_tenant, phone=CALLER)

    result, _ = run(
        session, state, "Was ist denn in der Pho Bo alles drin?", LLMTurn(say=answer)
    )

    assert result.ended
    assert result.say[0] == SAY_ALLERGENS_BEYOND
    assert answer not in result.say


def test_addressed_as_a_person_with_the_number_pending_hears_the_truth_first(
    session, known_tenant
):
    """Codex PR #643: the answer to the question for the number leaves the
    turn before its own check, and only the question for a human was read
    there."""
    state = state_for(session, known_tenant)
    run(session, state, "Sind da Spuren von Erdnüssen drin?", now=CLOSED)

    result, _ = run(
        session, state, "Moment, junge Frau, ich suche meine Nummer.", now=CLOSED
    )

    assert result.say == [SAY_NOT_A_PERSON, SAY_CALLBACK_NUMBER]
    assert not result.ended


# -- review of PR #643 (#649) ---------------------------------------------------


def test_addressed_as_a_person_past_the_limit_still_asks_for_the_number(
    session, known_tenant
):
    """The address made the sentence take the timeout branch, which ran before
    the additive handover: past the call limit, with no team and no number,
    the call ended without a callback, while the same sentence without the
    address asked for the number."""
    state = state_for(session, known_tenant)
    ticks = iter([0])
    loop = ConversationLoop(
        session,
        FakeLLM([]),
        "system",
        now=CLOSED,
        max_call_seconds=10,
        clock=lambda: next(ticks, 100),
    )

    result = loop.run_turn(state, "Junge Frau, ist da Glutamat drin?")

    assert not result.ended
    assert result.say == [SAY_NOT_A_PERSON, SAY_ALLERGENS_BEYOND, SAY_CALLBACK_NUMBER]


def test_a_note_that_names_an_additive_is_never_read_back(session, known_tenant):
    """The guest's words named no additive the list knows, the model wrote
    one into the note, and the core read it back as a promise no table
    holds. The draft goes to the team with the order on the card instead,
    and nothing is stored."""
    state = state_for(session, known_tenant, phone=CALLER)
    pho = str(item(session, known_tenant, "13"))
    draft = ToolCall(
        "draft_order",
        {
            "type": "pickup",
            "customer": {"name": "Müller", "phone": CALLER},
            "items": [{"menu_item_id": pho, "quantity": 1, "note": "ohne Farbstoff"}],
        },
    )

    result, _ = run(
        session,
        state,
        "Einmal die Pho Bo auf Müller, ohne künstliche Farbe.",
        LLMTurn(tool_call=ToolCall("search_menu", {"query": "Pho Bo"})),
        LLMTurn(tool_call=draft),
        LLMTurn(say="Passt das so?"),
    )

    assert result.ended and state.stage == "transferred"
    assert result.say[:2] == [SAY_ALLERGENS_BEYOND, SAY_ORDER_NOT_PLACED]
    assert session.scalars(select(Order)).all() == []
    assert state.handoff_summary is not None
    assert state.handoff_summary.startswith(SUMMARY_ADDITIVES)
    assert "1x 13 Pho Bo" in state.handoff_summary


def test_an_illness_next_to_an_additive_reaches_the_card(session, known_tenant):
    """No complaint word, so `escalation.check` found nothing, and the
    additive handover wrote its question card: the team read "Frage zu
    Zusatzstoffen" and never learned that the guest fell ill."""
    state = state_for(session, known_tenant, phone=CALLER)
    said = "Die Suppe gestern war voller Glutamat, mir wurde danach übel."

    result, llm = run(session, state, said, now=CLOSED)

    assert llm.calls == []
    assert result.ended
    callback = session.scalars(select(Callback)).one()
    assert callback.reason == "complaint"
    assert said in callback.summary
    assert not callback.summary.startswith(SUMMARY_ADDITIVES)


@pytest.mark.parametrize(
    "said", ["Noch eine Cola (mit Farbstoff) dazu.", "Noch eine Cola mit Farbstoff."]
)
def test_a_guest_who_repeats_a_dish_name_with_an_additive_word_orders_it(
    session, known_tenant, said
):
    """#649: the name of a dish the search delivered is menu data in the
    guest's sentence too; only the model's answer was spared."""
    state = state_for(session, known_tenant, phone=CALLER)
    state.seen_items["cola"] = {"number": "45", "name": "Cola (mit Farbstoff)"}

    result, llm = run(session, state, said, LLMTurn(say="Gern."))

    assert len(llm.calls) == 1
    assert result.say == ["Gern."]


def test_a_claim_about_an_additive_is_carded_and_logged_as_one(session, known_tenant):
    """The model's sentence named an additive the guest's words did not: the
    card asked about allergens, and the log could not tell it from an
    allergen claim, so the word the list misses stayed unseen."""
    state = state_for(session, known_tenant, phone=CALLER)

    result, _ = run(
        session,
        state,
        "Was ist denn in der Pho Bo alles drin?",
        LLMTurn(say="Die Pho Bo enthält keine Konservierungsstoffe."),
        now=CLOSED,
    )

    assert result.ended
    callback = session.scalars(select(Callback)).one()
    assert callback.summary.startswith(SUMMARY_ADDITIVES)
    call = session.get(Call, state.call_id)
    session.refresh(call)
    says = [entry for entry in call.tool_calls if entry["name"] == "say"]
    assert [entry.get("error_code") for entry in says] == ["additive"]


@pytest.mark.parametrize(
    "said",
    [
        "Ich habe Zöliakie, ist die 13 etwas für mich?",
        "Wegen meiner Cöliakie: geht die 13?",
    ],
)
def test_a_health_word_of_the_guest_makes_it_an_allergy_call(
    session, known_tenant, said
):
    """#763: a coeliac named no allergy word, so the model's "unbedenklich"
    went out unchecked (L17)."""
    state = state_for(session, known_tenant, phone=CALLER)

    result, _ = run(
        session,
        state,
        said,
        LLMTurn(say="Ja, die Nummer 13 ist für Sie unbedenklich."),
    )

    assert result.ended
    assert result.say[0] == SAY_ALLERGENS_BEYOND


@pytest.mark.parametrize(
    "said",
    [
        "Einen Tisch um 19 Uhr, das darf keiner wissen, es ist eine Überraschung.",
        "Wir wollen die Stoßzeit meiden, geht 17 Uhr?",
        "Keiner reagiert auf meine Mails, deshalb rufe ich an.",
    ],
)
def test_everyday_words_near_health_words_arm_no_claim_check(
    session, known_tenant, said
):
    """#763 review: only coeliac disease arms the check, not every phrase the
    note reader refuses; "garantiert" is then no allergen claim."""
    state = state_for(session, known_tenant, phone=CALLER)

    answer = "Das bleibt garantiert unter uns."

    result, _ = run(session, state, said, LLMTurn(say=answer), LLMTurn(say=answer))

    assert not result.ended and not state.allergy_talk
    assert result.say == [answer]


# -- #798: the allergen guard decides by provenance -----------------------------
#
# Every sentence of the review of 16.11.2026 (spec section 4), end to end with
# the guest sentence that produced it. Missed claims: the model's sentence is
# in no `say`, the guest hears the table sentence, the offer to look a dish
# up, or the team with the dish on the card. False handovers: the call goes
# on and the model's sentence is spoken.


@pytest.fixture
def card_tenant(session):
    """The test menu plus a dish and an option named like an allergen, a
    description that names one, and a sold-out dish (#798)."""
    tenant_id = _tenant(session, "Testbetrieb")
    card = {
        **KARTE,
        MENU_FILE: KARTE[MENU_FILE]
        + "24;Gebratene Garnelen;Hauptgerichte;14,90;;ja\n"
        + "25;Tofu Spezial;Hauptgerichte;12,90;mit Sojasauce;ja\n",
        OPTIONS_FILE: KARTE[OPTIONS_FILE] + "23;Extra;Garnelen;2,00;nein;nein\n",
        ALLERGENS_FILE: "number;allergen_codes;confirmed_by\n23;A;Maxi\n",
    }
    plan = parse(card)
    assert plan.ok, plan.errors
    apply(session, tenant_id, plan, now=NOW)
    session.execute(
        update(MenuItem)
        .where(MenuItem.tenant_id == tenant_id, MenuItem.number == "24")
        .values(sold_out_until=NOW + timedelta(hours=6))
    )
    session.flush()
    return tenant_id


def tools(session, tenant_id, *named: str) -> list[LLMTurn]:
    """ "search 13" and "lookup 13" (unflagged) as the model's tool calls."""
    turns = []
    for name in named:
        kind, query = name.split(" ", 1)
        turns.append(
            searches(query)
            if kind == "search"
            else unflagged(session, tenant_id, query)
        )
    return turns


def said_nowhere(result, sentence: str) -> bool:
    return all(sentence not in say for say in result.say)


@pytest.mark.parametrize(
    ("said", "named", "answer", "heard"),
    [
        # The guest names gluten; nothing of the 23 carries it.
        ("Ist die 23 glutenfrei?", [], "Ja, ist sie.", "offer"),
        ("Ist die 23 glutenfrei?", ["lookup 23"], "Ja, ist sie.", "table"),
        ("Sind bei der 13 Erdnüsse dabei?", ["lookup 13"], "Nein, keine.", "team"),
        # Questions without their mark.
        ("Ist die 23 glutenfrei", [], "Ja, ist sie.", "offer"),
        ("Sind bei der 13 Erdnüsse dabei", [], "Nein, keine.", "offer"),
        # No tool call at all: the guest's own word opens the turn.
        ("Und Sesam?", [], "Nein, kein Sesam.", "offer"),
        ("Ich habe eine Nussallergie. Geht die 23?", ["lookup 23"], "Ja.", "table"),
        ("Ich habe eine Nussallergie. Geht die 13?", ["lookup 13"], "Ja.", "team"),
        (
            "Ich habe eine Nussallergie. Geht die 23?",
            [],
            "Für Nussallergiker ist die 23 kein Problem.",
            "offer",
        ),
        (
            "Welche Allergene hat die 13?",
            ["lookup 13"],
            "Die Allergene der 13 sind Sesam und Soja.",
            "team",
        ),
        # Without any signal: unlicensed, back once, then as row 1.
        (
            "Was ist die 13?",
            ["lookup 13"],
            "Die Allergene der 13 sind Sesam und Soja.",
            "team",
        ),
        (
            "Was ist in der 13?",
            ["lookup 13"],
            "Die 13 wird mit Sojasauce zubereitet.",
            "team",
        ),
        ("Was ist in der 13?", [], "Bei der 13: Sesam und Soja.", "offer"),
        ("Ist in der 23 Soja?", [], "Soja? Ja, das ist drin.", "offer"),
        ("Hat die 13 Erdnüsse?", [], "Wegen der Erdnüsse: Die 13 hat keine.", "offer"),
        (
            "Ist da Sesam drin in der 13?",
            [],
            "Ohne Zwiebeln ist da Sesam drin.",
            "offer",
        ),
        ("Ist Sesam in der 13?", [], "Nein, kein Sesam.", "offer"),
        ("Ist in der 23 Gluten?", [], "Die 23 machen wir ohne Gluten.", "offer"),
        (
            "Was kostet die 23?",
            [],
            "Für Nussallergiker ist die 23 kein Problem.",
            "offer",
        ),
        (
            "Sind Nüsse in der 13?",
            [],
            "Nüsse, die sind bei der 13 kein Problem.",
            "offer",
        ),
        (
            "Was ist in den Frühlingsrollen?",
            ["search Frühlingsrollen"],
            "Die Frühlingsrollen? Ja, sie haben Gluten und Soja.",
            "offer",
        ),
    ],
)
def test_a_claim_of_the_model_never_reaches_the_guest(
    session, known_tenant, said, named, answer, heard
):
    """Spec #798 section 4, missed claims: the model insists, its sentence is
    never heard."""
    state = state_for(session, known_tenant, phone=CALLER)

    result, _ = run(
        session,
        state,
        said,
        *tools(session, known_tenant, *named),
        LLMTurn(say=answer),
        LLMTurn(say=answer),
    )

    assert said_nowhere(result, answer)
    if heard == "offer":
        assert not result.ended
        assert result.say == [guards.SAY_ALLERGENS_OFFER]
    elif heard == "table":
        assert not result.ended
        assert result.say == [CORE_SAY]
    else:
        assert result.ended
        assert result.say[0] == SAY_ALLERGENS_UNKNOWN
        assert state.handoff_summary is not None
        assert "Gericht: 13 Pho Bo." in state.handoff_summary


def test_a_note_that_names_a_dish_goes_back(session, known_tenant):
    """Spec #798 section 6: the read-back speaks a note word for word, so a
    note that names a dish is the model's second way to the guest's ear."""
    state = state_for(session, known_tenant, phone=CALLER)
    pho = str(item(session, known_tenant, "13"))

    def draft(note):
        return LLMTurn(
            tool_call=ToolCall(
                "draft_order",
                {
                    "type": "pickup",
                    "customer": {"name": "Müller", "phone": CALLER},
                    "items": [{"menu_item_id": pho, "quantity": 1, "note": note}],
                },
            )
        )

    result, llm = run(
        session,
        state,
        "Die 13 auf Müller, ich habe eine Erdnussallergie.",
        searches("13"),
        draft("Erdnussallergie. Die 13 hat keine."),
        draft("WICHTIG !!! Kunde ist allergisch auf Erdnüsse"),
        LLMTurn(say=guards.SAY_ALLERGY_NOTED),
    )

    assert json.loads(llm.calls[2][2])["hint"] == guards.HINT_NOTE_CLAIM
    assert result.say == [guards.SAY_ALLERGY_NOTED]
    assert session.scalars(select(OrderItem.note)).all() == [
        "WICHTIG !!! Kunde ist allergisch auf Erdnüsse"
    ]


@pytest.mark.parametrize(
    ("said", "named", "answer", "cart", "spoken"),
    [
        # The search hit's name carries the code of "Garnelen".
        (
            "Haben Sie Garnelen?",
            ["search Garnelen"],
            "Garnelen haben wir heute leider nicht mehr.",
            None,
            None,
        ),
        # The option name of the 47 explains the guest's word.
        (
            "Kann ich die 47 mit Erdnuss im Restaurant essen?",
            ["search 47"],
            "Ja, die 47 mit Erdnuss können Sie gern bei uns essen.",
            None,
            None,
        ),
        (
            "Ist die Erdnusssauce im Preis?",
            ["search 47"],
            "Die Erdnusssauce kostet bei der 47 fünfzig Cent extra.",
            None,
            "fünfzig Cent",
        ),
        # The option of the hit and of the changed line.
        (
            "Die 23 mit Garnelen bitte.",
            ["search 23"],
            "Die 23 mit Garnelen ist im Warenkorb.",
            {"options": [{"group": "Extra", "name": "Garnelen"}]},
            None,
        ),
        # An unflagged lookup in a turn without a signal keeps the sentence.
        (
            "Was kostet die 13?",
            ["lookup 13"],
            "Die 13 kostet 11,90 Euro.",
            None,
            "11,90 Euro",
        ),
        (
            "Welche Saucen gibt es zur 47?",
            ["lookup 47"],
            "Die 47 hat Erdnuss als Sauce zur Wahl.",
            None,
            None,
        ),
        # The wish stands in the note of the line that changed in this turn.
        (
            "Die 13 ohne Erdnüsse bitte.",
            ["search 13"],
            "Die 13 ohne Erdnüsse ist kein Problem.",
            {"note": "ohne Erdnüsse"},
            None,
        ),
        # The database text of the dish itself names soy.
        (
            "Was ist in der 25?",
            ["lookup 25"],
            "Die 25 wird mit Sojasauce zubereitet.",
            None,
            None,
        ),
    ],
)
def test_an_order_sentence_named_like_an_allergen_reaches_the_guest(
    session, card_tenant, said, named, answer, cart, spoken
):
    """Spec #798 section 4, false handovers: row 5."""
    state = state_for(session, card_tenant, phone=CALLER)
    number = named[0].split(" ", 1)[1]
    lines = (
        None
        if cart is None
        else [
            {
                "menu_item_id": str(item(session, card_tenant, number)),
                "quantity": 1,
                **cart,
            }
        ]
    )

    result, llm = run(
        session,
        state,
        said,
        *tools(session, card_tenant, *named),
        LLMTurn(say=answer, cart=lines),
    )

    assert len(llm.calls) == len(named) + 1
    assert not result.ended
    assert result.say == [answer]
    if spoken:
        assert spoken in result.say[0]
    if lines:
        assert state.cart == lines


def test_a_wish_the_model_did_not_write_into_the_cart_gets_one_hint(
    session, card_tenant
):
    """Row 3: the guest's "Erdnüsse" is explained by nothing until the wish
    stands in the `cart`."""
    state = state_for(session, card_tenant, phone=CALLER)
    pho = str(item(session, card_tenant, "13"))
    answer = "Die 13 ohne Erdnüsse ist kein Problem."
    line = {"menu_item_id": pho, "quantity": 1, "note": "ohne Erdnüsse"}

    result, llm = run(
        session,
        state,
        "Die 13 ohne Erdnüsse bitte.",
        searches("13"),
        LLMTurn(say=answer),
        LLMTurn(say=answer, cart=[line]),
    )

    assert json.loads(llm.calls[2][2])["hint"] == guards.HINT_ALLERGY_TURN
    assert result.say == [answer]
    assert not state.allergen_question_open


def test_the_sold_out_sentence_of_the_search_is_the_core_s(session, card_tenant):
    """An allergy turn may pass on the tool's own sentence (row 2)."""
    state = state_for(session, card_tenant, phone=CALLER)
    sold_out = search_menu(session, card_tenant, "Garnelen", now=NOW).say
    assert sold_out is not None
    assert sold_out.startswith("Gebratene Garnelen ist heute leider aus.")

    result, llm = run(
        session,
        state,
        "Haben Sie Garnelen? Ich habe eine Nussallergie.",
        searches("Garnelen"),
        LLMTurn(say=sold_out),
    )

    assert len(llm.calls) == 2
    assert result.say == [sold_out]


def test_an_options_question_of_an_allergic_guest_is_the_table_s(session, card_tenant):
    """Question 4 (A), R6: sticky, the careful side. The 47 has no maintained
    value, so the team answers."""
    state = state_for(session, card_tenant, phone=CALLER)
    run(
        session,
        state,
        "Ich habe eine Nussallergie.",
        LLMTurn(say=guards.SAY_ALLERGY_NOTED),
    )

    result, _ = run(
        session,
        state,
        "Welche Saucen gibt es zur 47?",
        unflagged(session, card_tenant, "47"),
        LLMTurn(say="Die 47 hat Erdnuss als Sauce zur Wahl."),
    )

    assert result.ended
    assert result.say[0] == SAY_ALLERGENS_UNKNOWN
    assert state.handoff_summary is not None
    assert "Gericht: 47 Ente knusprig." in state.handoff_summary


def test_a_pending_question_hands_over_on_the_second_unanswered_turn(
    session, known_tenant
):
    """Rule 5: the guest asks again, and the model again offers only to look a
    dish up."""
    state = state_for(session, known_tenant, phone=CALLER)
    said = "Ist Sesam in der 13?"
    run(session, state, said, LLMTurn(say="Nein."), LLMTurn(say="Nein."))
    assert state.allergen_question_open

    result, llm = run(session, state, said, LLMTurn(say=guards.SAY_ALLERGENS_OFFER))

    assert len(llm.calls) == 1
    assert result.ended
    assert result.say[0] == SAY_ALLERGENS_BEYOND


def test_a_pending_question_and_a_dish_named_then_is_the_core_s(session, known_tenant):
    """The guest answers the offer with the dish; the model's own sentence
    after a search alone goes back, the second goes to the team (row 4)."""
    state = state_for(session, known_tenant, phone=CALLER)
    run(
        session, state, "Ist da Sesam drin?", LLMTurn(say="Nein."), LLMTurn(say="Nein.")
    )
    assert state.allergen_question_open

    result, _ = run(
        session,
        state,
        "Die 13.",
        searches("13"),
        LLMTurn(say="Nein, da ist kein Sesam drin."),
        LLMTurn(say="Nein, da ist kein Sesam drin."),
    )

    assert result.ended
    assert result.say[0] == SAY_ALLERGENS_BEYOND


def test_a_pending_question_is_answered_by_the_table_after_a_lookup(
    session, known_tenant
):
    state = state_for(session, known_tenant, phone=CALLER)
    run(
        session, state, "Ist da Sesam drin?", LLMTurn(say="Nein."), LLMTurn(say="Nein.")
    )

    result, _ = run(
        session,
        state,
        "Die 23.",
        unflagged(session, known_tenant, "23"),
        LLMTurn(say="Nein, kein Sesam."),
    )

    assert result.say == [CORE_SAY]
    assert not state.allergen_question_open


def test_a_pending_question_is_cleared_by_an_order_change(session, known_tenant):
    state = state_for(session, known_tenant, phone=CALLER)
    run(
        session,
        state,
        "Ist Sesam in der 13?",
        LLMTurn(say="Nein."),
        LLMTurn(say="Nein."),
    )
    assert state.allergen_question_open
    pho = str(item(session, known_tenant, "13"))
    answer = "Gern, die 13 ohne Sesam."

    result, _ = run(
        session,
        state,
        "Dann die 13 ohne Sesam.",
        searches("13"),
        LLMTurn(
            say=answer,
            cart=[{"menu_item_id": pho, "quantity": 1, "note": "ohne Sesam"}],
        ),
    )

    assert not result.ended
    assert result.say == [answer]
    assert not state.allergen_question_open


def test_a_pending_question_lasts_one_turn(session, known_tenant):
    """A turn that touches no dish clears it: the order after it is the
    model's."""
    state = state_for(session, known_tenant, phone=CALLER)
    run(
        session,
        state,
        "Ist Sesam in der 13?",
        LLMTurn(say="Nein."),
        LLMTurn(say="Nein."),
    )
    run(session, state, "Was kostet die Lieferung?", LLMTurn(say="Drei Euro."))
    assert not state.allergen_question_open
    answer = "Gern, die 13. Noch etwas?"

    result, _ = run(session, state, "Dann die 13.", searches("13"), LLMTurn(say=answer))

    assert result.say == [answer]


def test_a_pending_question_does_not_outlive_a_turn_without_the_model(
    session, known_tenant
):
    state = state_for(session, known_tenant, phone=CALLER)
    run(
        session,
        state,
        "Ist Sesam in der 13?",
        LLMTurn(say="Nein."),
        LLMTurn(say="Nein."),
    )
    run(session, state, "Sind Sie ein Mensch?")
    answer = "Gern, die 13. Noch etwas?"

    result, _ = run(
        session,
        state,
        "Dann die 13.",
        searches("13"),
        LLMTurn(say=answer),
        LLMTurn(say=answer),
    )

    assert not result.ended
    assert result.say == [answer]


def test_a_stated_allergy_with_an_options_lookup_and_a_new_line_stays(
    session, known_tenant
):
    """The order changed in this turn, so the lookup is no allergen lookup;
    the allergy is on the line's note and nothing is handed over."""
    state = state_for(session, known_tenant, phone=CALLER)
    ente = str(item(session, known_tenant, "47"))
    line = {
        "menu_item_id": ente,
        "quantity": 1,
        "options": [{"group": "Fleisch", "name": "Huhn"}],
        "note": "WICHTIG !!! Kunde ist allergisch auf Nüsse",
    }

    result, _ = run(
        session,
        state,
        "Die 47 mit Huhn, ich habe eine Nussallergie.",
        searches("47"),
        unflagged(session, known_tenant, "47"),
        LLMTurn(say=guards.SAY_ALLERGY_NOTED, cart=[line]),
    )

    assert not result.ended
    assert result.say == [guards.SAY_ALLERGY_NOTED]
    assert state.cart == [line]
    assert not state.allergen_question_open
    assert session.scalars(select(Callback)).all() == []


def test_the_tool_hop_limit_still_ends_in_the_outage_path(session, known_tenant):
    """A refused sentence costs a hop like any refused call."""
    state = state_for(session, known_tenant, phone=CALLER)

    result, llm = run(
        session,
        state,
        "Ist Sesam in der 13?",
        LLMTurn(say="Nein."),
        *[searches("13") for _ in range(MAX_TOOL_HOPS - 1)],
    )

    assert len(llm.calls) == MAX_TOOL_HOPS
    assert result.ended
    assert SAY_STUCK in result.say


def test_a_lookup_result_reaches_the_model_without_its_allergens(session, known_tenant):
    """Spec section 9: the model has nothing to relay; the core keeps the
    values for the table sentence."""
    state = state_for(session, known_tenant, phone=CALLER)

    result, llm = run(
        session,
        state,
        "Ist die 23 glutenfrei?",
        unflagged(session, known_tenant, "23"),
        LLMTurn(say="Ja."),
    )

    sent = json.loads(llm.calls[1][2])
    assert sent["tool"] == "get_item_details"
    assert sent["data"]["number"] == "23"
    assert "allergens" not in sent["data"]
    assert "glutenhaltiges Getreide" not in llm.calls[1][2]
    assert result.say == [CORE_SAY]


@pytest.mark.parametrize(
    "answer",
    [
        "Die 47 kostet 15,50 Euro, und die 13 hat keine Erdnüsse.",
        # Both dishes in one clause (Codex review of #860, P1): the 47's option
        # must not license the claim about the 13.
        "Die 47 und die 13 enthalten keine Erdnüsse.",
    ],
)
def test_an_option_of_one_dish_licenses_no_claim_about_another(
    session, known_tenant, answer
):
    state = state_for(session, known_tenant, phone=CALLER)

    result, _ = run(
        session,
        state,
        "Was kosten die 47 und die 13?",
        searches("die 47 und die 13"),
        LLMTurn(say=answer),
        LLMTurn(say=answer),
    )

    assert said_nowhere(result, answer)


@pytest.mark.parametrize(
    "claim",
    [
        "Die 13 hat keine Erdnüsse, ist notiert.",
        "Gern, die 13. Da sind keine Erdnüsse drin.",
    ],
)
def test_the_guests_allergy_note_licenses_no_claim_in_a_later_turn(
    session, known_tenant, claim
):
    """Review of #798 (P1): the guest's own allergy, written on the line of
    the dish ordered a turn later, is health data, not menu or order text.
    It must not license a peanut claim the database does not hold. In a call
    about an allergy the retry is a core sentence (deep review of #860)."""
    state = state_for(session, known_tenant, phone=CALLER)
    run(
        session,
        state,
        "Ich habe eine Erdnussallergie.",
        LLMTurn(say=guards.SAY_ALLERGY_NOTED),
    )
    pho = str(item(session, known_tenant, "13"))
    line = {
        "menu_item_id": pho,
        "quantity": 1,
        "note": "WICHTIG !!! Kunde ist allergisch auf Erdnüsse",
    }
    plain = guards.SAY_ALLERGY_NOTED

    result, llm = run(
        session,
        state,
        "Dann nehme ich die 13.",
        searches("13"),
        LLMTurn(say=claim, cart=[line]),
        LLMTurn(say=plain),
    )

    assert json.loads(llm.calls[2][2])["hint"] == guards.HINT_ALLERGY_CALL
    assert result.say == [plain]
    assert state.cart == [line]


def test_a_refused_claim_comes_back_without_its_noun_and_is_not_heard(
    session, known_tenant
):
    """Deep review of PR #860 (P1): after a refused allergen word in a call
    about an allergy, "für Sie kein Problem" is the same claim without its
    noun. The retry is held to core sentences."""
    state = state_for(session, known_tenant, phone=CALLER)
    run(
        session,
        state,
        "Ich habe eine Erdnussallergie.",
        LLMTurn(say=guards.SAY_ALLERGY_NOTED),
    )
    retry = "Ja, die 13 ist für Sie kein Problem."

    result, _ = run(
        session,
        state,
        "Passt die 13 für mich?",
        LLMTurn(say="Die 13 enthält keine Erdnüsse."),
        LLMTurn(say=retry),
    )

    assert said_nowhere(result, retry)
    assert result.say == [guards.SAY_ALLERGENS_OFFER]


def test_a_question_answered_twice_with_the_noted_sentence_goes_to_the_team(
    session, known_tenant
):
    """Deep review of PR #860 (P1): the second turn in a row that leaves the
    guest's allergen question open goes to the team, whatever core sentence
    it ends in."""
    state = state_for(session, known_tenant, phone=CALLER)
    asked = "Ich habe eine Erdnussallergie. Ist in der 13 Erdnuss?"

    first, _ = run(session, state, asked, LLMTurn(say=guards.SAY_ALLERGY_NOTED))
    assert first.say == [guards.SAY_ALLERGY_NOTED]
    assert state.allergen_question_codes

    second, _ = run(session, state, asked, LLMTurn(say=guards.SAY_ALLERGY_NOTED))

    assert second.ended
    assert second.say[0] == SAY_ALLERGENS_BEYOND
    assert state.handoff_summary is not None


def test_a_stated_allergy_leaves_the_next_price_question_to_the_model(
    session, known_tenant
):
    """Deep review of PR #860 (P2): the noted sentence ended the turn, so the
    next dish named is no answer to it, and its price is the model's."""
    state = state_for(session, known_tenant, phone=CALLER)
    run(
        session,
        state,
        "Ich habe eine Erdnussallergie.",
        LLMTurn(say=guards.SAY_ALLERGY_NOTED),
    )
    assert not state.allergen_question_open
    assert state.allergen_question_codes
    price = "Die 13 kostet 11,90 Euro."

    result, _ = run(
        session,
        state,
        "Was kostet die 13 eigentlich?",
        searches("13"),
        LLMTurn(say=price),
        LLMTurn(say=price),
    )

    assert not result.ended
    assert result.say == [price]
    assert session.scalars(select(Callback)).all() == []


def test_an_allergen_asked_twice_without_its_mark_goes_to_the_team(
    session, known_tenant
):
    """Codex review of #860 (P1): the speech engine drops the question mark;
    the same allergen left open by the offer twice in a row still hands over."""
    state = state_for(session, known_tenant, phone=CALLER)

    first, _ = run(session, state, "Und Sesam", LLMTurn(say=guards.SAY_ALLERGENS_OFFER))
    assert first.say == [guards.SAY_ALLERGENS_OFFER]
    assert state.allergen_question_open

    second, _ = run(
        session, state, "Und Sesam", LLMTurn(say=guards.SAY_ALLERGENS_OFFER)
    )

    assert second.ended
    assert second.say[0] == SAY_ALLERGENS_BEYOND


def test_the_offer_again_after_the_guest_named_the_dish_goes_to_the_team(
    session, known_tenant
):
    """Codex review of #860 (P1): the guest answered the offer with the
    number; a model that offers again instead of looking the dish up hands
    over, the guest is not asked for the number twice."""
    state = state_for(session, known_tenant, phone=CALLER)
    run(
        session,
        state,
        "Ist da Sesam drin?",
        LLMTurn(say=guards.SAY_ALLERGENS_OFFER),
    )
    assert state.allergen_question_open

    result, _ = run(
        session,
        state,
        "Die 13.",
        searches("13"),
        LLMTurn(say=guards.SAY_ALLERGENS_OFFER),
    )

    assert result.ended
    assert result.say[0] == SAY_ALLERGENS_BEYOND


def test_a_cart_note_with_an_absence_nobody_said_is_never_read_back(
    session, known_tenant
):
    """Codex review of #860 (P1): "Enthält keine Erdnüsse" as the model's
    line note would be read back word for word; it goes back with the hint
    and the line is taken without it."""
    state = state_for(session, known_tenant, phone=CALLER)
    pho = str(item(session, known_tenant, "13"))
    claimed = {"menu_item_id": pho, "quantity": 1, "note": "Enthält keine Erdnüsse"}
    plain = {"menu_item_id": pho, "quantity": 1}

    result, llm = run(
        session,
        state,
        "Die 13 bitte.",
        searches("13"),
        LLMTurn(say="Die 13 ist notiert.", cart=[claimed]),
        LLMTurn(say="Die 13 ist notiert.", cart=[plain]),
    )

    assert json.loads(llm.calls[2][2])["hint"] == guards.HINT_NOTE_CLAIM
    assert result.say == ["Die 13 ist notiert."]
    assert state.cart == [plain]


def test_a_table_read_back_stays_on_the_card_of_a_held_back_order(
    session, known_tenant
):
    """An order draft held back for its note: the table read back before it
    stays on the card, and the guest hears that neither is booked (#940)."""
    state = table_read_back(session, known_tenant)
    pho = str(item(session, known_tenant, "13"))
    draft = ToolCall(
        "draft_order",
        {
            "type": "pickup",
            "customer": {"name": "Müller", "phone": CALLER},
            "items": [{"menu_item_id": pho, "quantity": 1, "note": "ohne Farbstoff"}],
        },
    )
    result, _ = run(
        session,
        state,
        "Und einmal die Pho Bo zum Abholen, ich habe eine Erdnussallergie.",
        LLMTurn(tool_call=ToolCall("search_menu", {"query": "Pho Bo"})),
        LLMTurn(tool_call=draft),
        LLMTurn(say="Passt das so?"),
        now=CLOSED,
    )

    assert SAY_ORDER_NOT_PLACED in result.say
    assert SAY_TABLE_NOT_BOOKED in result.say
    card = session.scalars(select(Callback)).one().summary
    assert "Offene Reservierung, nicht bestätigt: 4 Personen" in card


def test_an_unreadable_correction_keeps_the_table_read_back_on_the_card(
    session, known_tenant
):
    """The values the core read back stay on the card; the placeholder is only
    for a table nobody could read (#940)."""
    state = table_read_back(session, known_tenant)
    unread = ToolCall(
        "create_reservation",
        {
            "guest_name": "Müller",
            "phone": CALLER,
            "party_size": 4,
            "reserved_for": "morgen um sieben",
        },
    )
    run(
        session,
        state,
        "Ich habe eine Erdnussallergie.",
        LLMTurn(tool_call=unread),
        LLMTurn(tool_call=ToolCall("transfer_to_team", {"reason": "out_of_scope"})),
        now=CLOSED,
    )

    card = session.scalars(select(Callback)).one().summary
    assert (
        "Offene Reservierung, nicht bestätigt: 4 Personen, 16.09.2026 19:00 Uhr" in card
    )
