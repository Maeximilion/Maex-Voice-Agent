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

from api.agent.consent import ALLERGY_QUESTION
from api.agent.escalation import asks_beyond_the_allergens
from api.agent.guards import note_said
from api.agent.llm import FakeLLM, LLMTurn, ToolCall
from api.agent.loop import (
    SAY_ALLERGENS_BEYOND,
    SAY_CALLBACK_NUMBER,
    SAY_NOBODY_REACHABLE,
    SAY_NOT_A_PERSON,
    SAY_ORDER_NOT_PLACED,
    SAY_TABLE_NOT_BOOKED,
    SUMMARY_ALLERGENS,
    ConversationLoop,
)
from api.agent.state import ConversationState
from api.domain.callbacks.transfer import SUMMARY_MISSED
from api.domain.menu.details import SAY_ALLERGENS_UNKNOWN
from api.domain.menu.importer import ALLERGENS_FILE, apply, parse
from api.models import Call, Callback, Order, OrderItem
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


def searches_nothing() -> LLMTurn:
    return LLMTurn(tool_call=ToolCall("search_menu", {"query": NOT_ON_THE_CARD}))


def test_an_allergy_question_on_a_dish_not_found_goes_to_the_team(
    session, known_tenant
):
    """`allergie_0002` on the real model: it searched the dish with the allergen
    in the query, found nothing and asked for the number, and nobody answered
    the question (L17). The model is not asked again."""
    state = state_for(session, known_tenant, phone=CALLER)

    result, llm = run(
        session,
        state,
        "Hallo, ist in der Pho Bo Sellerie? Ich habe eine Allergie.",
        searches_nothing(),
    )

    assert len(llm.calls) == 1
    assert result.ended
    assert result.say[0] == SAY_ALLERGENS_UNKNOWN
    assert state.stage == "transferred"
    call = session.get(Call, state.call_id)
    session.refresh(call)
    assert call.transfer_reason == "out_of_scope"


def test_the_card_of_that_question_does_not_repeat_what_was_searched(
    session, known_tenant
):
    """The search words can hold the guest's allergy, and the card never does
    (L13)."""
    state = state_for(session, known_tenant, phone=CALLER)

    run(
        session,
        state,
        "Hallo, ist in der Pho Bo Sellerie? Ich habe eine Allergie.",
        searches_nothing(),
    )

    assert state.handoff_summary.startswith(SUMMARY_ALLERGENS)
    assert "Sellerie" not in state.handoff_summary


def test_with_nobody_there_that_question_asks_for_a_number(session, known_tenant):
    state = state_for(session, known_tenant)

    result, _ = run(
        session,
        state,
        "Hallo, ist in der Pho Bo Sellerie? Ich habe eine Allergie.",
        searches_nothing(),
        now=CLOSED,
    )

    assert not result.ended
    assert SAY_CALLBACK_NUMBER in result.say


@pytest.mark.parametrize(
    "said",
    [
        # An allergy told, nothing asked: the core notes it for the kitchen.
        "Die Sellerie Spezial bitte, ich habe eine Erdnussallergie.",
        # A question, but no word of an allergy: a dish we do not have.
        "Haben Sie Sellerie Spezial?",
    ],
)
def test_other_sentences_with_a_dish_not_found_stay_with_the_model(
    session, known_tenant, said
):
    state = state_for(session, known_tenant, phone=CALLER)

    result, llm = run(
        session,
        state,
        said,
        searches_nothing(),
        LLMTurn(say="Das haben wir leider nicht auf der Karte."),
    )

    assert len(llm.calls) == 2
    assert not result.ended


def test_a_dish_the_search_finds_goes_on_to_its_allergens(session, known_tenant):
    """Only a search that found nothing ends here: the found dish has an
    answer in the database, or its own way to the team."""
    state = state_for(session, known_tenant, phone=CALLER)

    result, llm = run(
        session,
        state,
        "Hallo, ist in der Pho Bo Sellerie? Ich habe eine Allergie.",
        LLMTurn(tool_call=ToolCall("search_menu", {"query": "Pho Bo"})),
        LLMTurn(say="Ich schaue nach."),
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


@pytest.mark.parametrize(
    "answer",
    [
        "Ja, das ist sicher.",
        "Die Pho Bo ist glutenfrei.",
        "Da sind keine Spuren von Nüssen drin.",
        "Die Pho Bo enthält keine Nüsse.",
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
        "Die Frühlingsrollen enthalten laut unseren Angaben Gluten.",
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
    the sesame note stays on its line."""
    state = state_for(session, known_tenant, phone=CALLER)
    item_23 = str(item(session, known_tenant, "23"))
    draft = ToolCall(
        "draft_order",
        {
            "type": "pickup",
            "customer": {"name": "Müller", "phone": CALLER},
            "items": [{"menu_item_id": item_23, "quantity": 1}],
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
    )
    assert state.last_say == ALLERGY_QUESTION
    run(session, state, "Ja.")

    [order] = session.scalars(select(Order)).all()
    [note] = session.scalars(
        select(OrderItem.note).where(OrderItem.order_id == order.id)
    ).all()
    assert note == "WICHTIG: Keine Sesam. Grund: Allergie"


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


def test_a_consented_allergy_reaches_the_card_only_as_a_removal(session, known_tenant):
    """Codex PR #369, P1: the caller said "ja" to the kitchen note, the order
    is open, and a traces question goes to a callback card. The card is kept
    for the period of a callback, the allergy only until the night after its
    order (L13): it says what to leave out, never why."""
    state = state_for(session, known_tenant, phone=CALLER)
    pho = str(item(session, known_tenant, "13"))
    draft = ToolCall(
        "draft_order",
        {
            "type": "pickup",
            "customer": {"name": "Müller", "phone": CALLER},
            "items": [{"menu_item_id": pho, "quantity": 1}],
        },
    )
    run(
        session,
        state,
        "Einmal die Pho Bo auf Müller, ich habe eine Erdnussallergie.",
        LLMTurn(tool_call=ToolCall("search_menu", {"query": "Pho Bo"})),
        LLMTurn(tool_call=draft),
    )
    assert state.last_say == ALLERGY_QUESTION
    run(session, state, "Ja.")
    assert state.stage == "readback_pending"

    result, _ = run(session, state, "Sind da Spuren von Sesam drin?", now=CLOSED)

    assert result.ended
    callback = session.scalars(select(Callback)).one()
    assert "1x 13 Pho Bo" in callback.summary
    assert "ohne Erdnuss" in callback.summary
    assert "Allergie" not in callback.summary
    assert "WICHTIG" not in callback.summary


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
        },
    )
    # The core reads the allergy from the guest's sentence and holds the
    # draft for the consent question; the model writes no allergy (T-11.1).
    run(
        session,
        state,
        "Tisch für zwei auf Müller, ich habe eine Erdnussallergie",
        LLMTurn(tool_call=table),
    )
    assert state.held_call is not None
    assert state.last_say == ALLERGY_QUESTION

    result, _ = run(session, state, "Gibt es bei Ihnen Spuren von Nüssen?", now=CLOSED)

    assert result.ended
    assert result.say[0] == SAY_ALLERGENS_BEYOND
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


def test_an_absence_of_an_allergen_is_a_claim_in_any_call(session, known_tenant):
    state = state_for(session, known_tenant, phone=CALLER)

    result, _ = run(
        session,
        state,
        "Was ist in der Pho Bo?",
        LLMTurn(say="Die Pho Bo enthält keine Nüsse."),
    )

    assert result.ended
    assert result.say[0] == SAY_ALLERGENS_BEYOND


@pytest.mark.parametrize(
    "said",
    [
        "Die 13, ich habe eine Erdnussallergie.",
        "Die 13, mein Sohn hat eine Erdnussallergie.",
        "Die 13, ich habe keine Erdnussallergie.",
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
        LLMTurn(say="Gern. Darf es noch etwas sein?"),
    )

    assert not result.ended
    assert result.say == ["Gern. Darf es noch etwas sein?"]
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
        LLMTurn(say="Gern."),
    )

    assert not result.ended


BOOKED = "Ihre Bestellung ist aufgegeben, Abholcode 42."


def test_a_live_handover_keeps_the_booking_for_the_line(session, known_tenant):
    """#207: on a live handover the core kept only the allergen card for the
    line; a team leg nobody answered became a card without "Bereits gebucht",
    and the team might take the order again."""
    state = state_for(session, known_tenant, phone=CALLER)
    state.booking_say = BOOKED

    run(
        session,
        state,
        "Ist in der Pho Bo Sellerie?",
        details(session, known_tenant, "13"),
    )

    assert state.stage == "transferred"
    assert state.handoff_summary is not None
    assert state.handoff_summary.startswith(SUMMARY_ALLERGENS)
    assert state.handoff_summary.endswith(f"Bereits gebucht: {BOOKED}")


def test_any_live_handover_keeps_the_booking_for_the_line(session, known_tenant):
    state = state_for(session, known_tenant, phone=CALLER)
    state.booking_say = BOOKED

    run(session, state, "Ich möchte bitte mit einem Menschen sprechen.")

    assert state.stage == "transferred"
    assert state.handoff_summary is not None
    assert state.handoff_summary.endswith(f"Bereits gebucht: {BOOKED}")


def test_the_card_of_a_missed_team_leg_fits_with_a_large_open_order(
    session, known_tenant
):
    """The open order is cut so the line's first sentence and the booking
    still fit a callback card (1000 characters, #207)."""
    state = state_for(session, known_tenant, phone=CALLER)
    state.booking_say = BOOKED
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
    assert len(f"{SUMMARY_MISSED} {state.handoff_summary}") <= 1000
    assert state.handoff_summary.endswith(f"Bereits gebucht: {BOOKED}")


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
