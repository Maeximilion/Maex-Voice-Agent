"""A complaint is resolved by a fixed rule from the database (T-10.11, D19).

The core matches the guest's sentence against the tenant's active rules, the
model never decides a remedy, and an empty catalogue, no match or any doubt
leave the handover as it was. The rule rows here are invented for these tests
and stay here: the catalogue is Maxi's, and nothing seeds one. Caller sentences
in German: the matching reads German.
"""

import uuid
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from api.agent.guards import note_said
from api.agent.llm import FakeLLM, LLMTurn, ToolCall
from api.agent.loop import (
    SAY_ESCALATION,
    SAY_NOT_A_PERSON,
    UNHEARD_TURNS,
    ConversationLoop,
)
from api.agent.recording import SAY_WHAT_CAN_I_DO
from api.agent.state import Allergy, ConversationState
from api.domain.callbacks import matching_rule
from api.domain.callbacks.complaint_rules import ACTION_REMEDY_BOOKED
from api.domain.callbacks.create import SAY_NOTED
from api.models import (
    AuditLog,
    Call,
    Callback,
    ComplaintRule,
    ConsentLog,
    Recording,
)
from scripts.seed import seed
from sim.session import SimCall, resolve_tenant

BERLIN = ZoneInfo("Europe/Berlin")
DAY = date(2026, 9, 15)
OPEN_NOW = datetime(2026, 9, 15, 18, 0, tzinfo=BERLIN)  # the team answers
CLOSED = datetime(2026, 9, 15, 8, 0, tzinfo=BERLIN)  # nobody answers
CALLER = "+4972215551234"
# Invented for these tests, no rule of any restaurant.
OFFER = "Testangebot, stimmt das so für Sie?"
GRANTED = "Testzusage."
COMPLAINT = "Ich bin unzufrieden, das Essen war kalt."


@pytest.fixture
def session(migrated_db_url):
    engine = create_engine(migrated_db_url)
    with Session(engine) as s:
        yield s
    engine.dispose()


@pytest.fixture
def tenant_id(session):
    return uuid.UUID(
        seed(session, tenant_name="Testbetrieb", timezone="Europe/Berlin").tenant_id
    )


def _call(session, tenant_id) -> uuid.UUID:
    call = Call(
        tenant_id=tenant_id,
        external_session_id=uuid.uuid4().hex,
        started_at=OPEN_NOW,
        delete_after=DAY,
    )
    session.add(call)
    session.commit()
    return call.id


def _state(session, tenant_id, **fields) -> ConversationState:
    return ConversationState(
        call_id=_call(session, tenant_id),
        tenant_id=tenant_id,
        caller_phone=CALLER,
        slots={"phone": CALLER},
        **fields,
    )


@pytest.fixture
def state(session, tenant_id):
    return _state(session, tenant_id)


def rule(session, tenant_id, **fields) -> ComplaintRule:
    row = ComplaintRule(
        tenant_id=tenant_id,
        **{
            "name": "Testregel",
            "phrases": ["kalt"],
            "remedy_cents": 345,
            "offer_say": OFFER,
            "granted_say": GRANTED,
            "active": True,
        }
        | fields,
    )
    session.add(row)
    session.commit()
    return row


def loop(session, turns=(), now=OPEN_NOW) -> ConversationLoop:
    return ConversationLoop(session, FakeLLM(list(turns)), "system", now=now)


def booked(session) -> list[AuditLog]:
    return list(
        session.scalars(
            select(AuditLog).where(AuditLog.action == ACTION_REMEDY_BOOKED)
        ).all()
    )


def cards(session, state) -> list[Callback]:
    return list(
        session.scalars(select(Callback).where(Callback.call_id == state.call_id)).all()
    )


def assert_went_to_the_team(result, session, state):
    assert result.ended is True
    assert state.stage == "transferred"
    assert state.remedy_offer is None
    assert session.get(Call, state.call_id).transfer_reason == "complaint"
    assert booked(session) == []


# -- an empty catalogue changes nothing -------------------------------------------


def test_nothing_seeds_a_rule(session, tenant_id):
    assert session.scalar(select(func.count()).select_from(ComplaintRule)) == 0


def test_with_an_empty_catalogue_the_team_takes_the_complaint(session, state):
    agent = loop(session)  # no model turn prepared: it is not asked

    result = agent.run_turn(state, COMPLAINT)

    assert result.say == [SAY_ESCALATION]
    assert_went_to_the_team(result, session, state)
    assert state.handoff_summary == (
        f"Kunde hat sich beschwert. Kunde sagte: {COMPLAINT}"
    )


def test_with_an_empty_catalogue_nobody_answering_leaves_a_callback(session, state):
    result = loop(session, now=CLOSED).run_turn(state, COMPLAINT)

    assert result.ended is True
    assert result.say == [SAY_NOTED]
    card = cards(session, state)[0]
    assert card.reason == "complaint"
    assert card.phone == CALLER
    assert card.summary == f"Kunde hat sich beschwert. Kunde sagte: {COMPLAINT}"


def test_with_an_empty_catalogue_the_models_handover_goes_through(session, state):
    call = ToolCall(name="transfer_to_team", args={"reason": "complaint"})
    result = loop(session, [LLMTurn(tool_call=call)]).run_turn(
        state, "Das Essen war gestern kalt."
    )

    assert_went_to_the_team(result, session, state)


# -- the normal case --------------------------------------------------------------


def test_a_matching_rule_is_offered_in_its_own_words_and_books_nothing_yet(
    session, tenant_id, state
):
    row = rule(session, tenant_id)
    agent = loop(session)  # no model turn prepared: it is not asked

    result = agent.run_turn(state, COMPLAINT)

    assert result.say == [OFFER]
    assert result.ended is False
    assert state.remedy_offer.rule_id == row.id
    assert state.stage == "start"
    assert session.get(Call, state.call_id).transfer_reason is None
    assert cards(session, state) == []
    assert booked(session) == []


def test_the_model_never_reads_the_offer_in_the_state(session, tenant_id, state):
    rule(session, tenant_id)
    loop(session).run_turn(state, COMPLAINT)

    prompt = state.to_prompt_json()

    assert "remedy_offer" not in prompt
    assert "345" not in str(prompt)


def test_a_yes_books_the_amount_of_the_row_on_a_card_and_ends_the_call(
    session, tenant_id, state
):
    row = rule(session, tenant_id)
    agent = loop(session)
    agent.run_turn(state, COMPLAINT)

    result = agent.run_turn(state, "Ja, gerne.")

    assert result.say == [GRANTED]
    assert result.ended is True
    assert state.stage == "callback"
    assert state.remedy_offer is None
    card = cards(session, state)[0]
    assert card.reason == "complaint"
    assert card.phone == CALLER
    assert "Testregel" in card.summary
    assert "3 Euro 45" in card.summary
    assert COMPLAINT in card.summary
    entry = booked(session)[0]
    assert entry.tenant_id == state.tenant_id
    assert entry.entity_id == row.id
    assert entry.payload == {"call_id": str(state.call_id), "remedy_cents": 345}


def test_an_offer_counts_as_heard_on_a_line_with_noise(session, tenant_id, state):
    """After turns the core took nothing from, the offer is what it took: the
    guest hears it, not the handover for a line nobody understood."""
    rule(session, tenant_id)
    call = ToolCall(name="transfer_to_team", args={"reason": "complaint"})
    turns = [LLMTurn(say="Wie bitte?")] * (UNHEARD_TURNS - 1)
    agent = loop(session, [*turns, LLMTurn(tool_call=call)])
    for _ in turns:
        agent.run_turn(state, "Rauschen")

    assert agent.run_turn(state, "Das Essen war gestern kalt.").say == [OFFER]


def test_a_complaint_only_the_model_heard_gets_the_same_offer(
    session, tenant_id, state
):
    """No trigger word: the model says it is a complaint, the core decides
    what follows, and the prompt stays as it is."""
    rule(session, tenant_id)
    call = ToolCall(name="transfer_to_team", args={"reason": "complaint"})
    agent = loop(session, [LLMTurn(tool_call=call)])

    result = agent.run_turn(state, "Das Essen war gestern kalt.")

    assert result.say == [OFFER]
    row = session.get(Call, state.call_id)
    assert row.transfer_reason is None
    assert [(c["name"], c["ok"]) for c in row.tool_calls] == [
        ("transfer_to_team", False)
    ]
    assert agent.run_turn(state, "Ja").say == [GRANTED]


def test_a_callback_the_model_chose_for_a_complaint_meets_the_rule_too(
    session, tenant_id, state
):
    rule(session, tenant_id)
    call = ToolCall(
        name="create_callback",
        args={"phone": CALLER, "reason": "complaint", "summary": "Essen kalt"},
    )

    result = loop(session, [LLMTurn(tool_call=call)]).run_turn(
        state, "Das Essen war gestern kalt."
    )

    assert result.say == [OFFER]
    assert cards(session, state) == []


def test_the_model_names_another_reason_and_no_rule_is_read(session, tenant_id, state):
    rule(session, tenant_id)
    call = ToolCall(name="transfer_to_team", args={"reason": "human_requested"})

    result = loop(session, [LLMTurn(tool_call=call)]).run_turn(
        state, "Das Essen war gestern kalt."
    )

    assert result.ended is True
    assert state.stage == "transferred"


def test_a_delivery_named_in_the_complaint_is_no_other_business(
    session, tenant_id, state
):
    """`note_intent` hears a delivery in this very sentence: that is what the
    complaint is about, not a wish the call still has to serve."""
    rule(session, tenant_id)
    call = ToolCall(name="transfer_to_team", args={"reason": "complaint"})

    result = loop(session, [LLMTurn(tool_call=call)]).run_turn(
        state, "Die Lieferung war gestern kalt."
    )

    assert result.say == [OFFER]


# -- anything but the yes ---------------------------------------------------------


@pytest.mark.parametrize(
    "answer",
    [
        "Nein.",
        "Nein danke, das reicht mir nicht.",
        "Ja, und noch eine Cola.",
        "Ja, 2 bitte.",
        "Stimmt das so?",
        "Hm, ich weiß nicht.",
        "Moment.",
    ],
)
def test_anything_but_a_plain_yes_goes_to_the_team(session, tenant_id, state, answer):
    rule(session, tenant_id)
    agent = loop(session)
    agent.run_turn(state, COMPLAINT)

    result = agent.run_turn(state, answer)

    assert result.say == [SAY_ESCALATION]
    assert_went_to_the_team(result, session, state)


def test_a_declined_offer_reaches_the_team_with_what_was_offered(
    session, tenant_id, state
):
    rule(session, tenant_id)
    agent = loop(session, now=CLOSED)
    agent.run_turn(state, COMPLAINT)

    result = agent.run_turn(state, "Nein, das reicht mir nicht.")

    assert result.say == [SAY_NOTED]
    assert result.ended is True
    card = cards(session, state)[0]
    assert card.reason == "complaint"
    assert "Testregel" in card.summary
    assert "3 Euro 45" in card.summary
    assert COMPLAINT in card.summary
    assert "das reicht mir nicht" in card.summary
    assert booked(session) == []


def test_an_allergy_named_in_the_answer_stays_off_the_card(session, tenant_id, state):
    rule(session, tenant_id)
    agent = loop(session, now=CLOSED)
    agent.run_turn(state, COMPLAINT)

    agent.run_turn(state, "Nein, ich habe eine Erdnussallergie.")

    summary = cards(session, state)[0].summary
    assert "allergie" not in summary.lower()
    assert "Testregel" in summary


def test_a_yes_to_another_sentence_books_nothing(session, tenant_id, state):
    """The phone line's "Sind Sie noch da?": a "Ja" answers that one."""
    rule(session, tenant_id)
    agent = loop(session)
    agent.run_turn(state, COMPLAINT)
    note_said(state, "Sind Sie noch da?")

    result = agent.run_turn(state, "Ja.")

    assert_went_to_the_team(result, session, state)


def test_the_truthful_answer_comes_first_in_the_answer_turn(session, tenant_id, state):
    rule(session, tenant_id)
    agent = loop(session)
    agent.run_turn(state, COMPLAINT)

    result = agent.run_turn(state, "Sind Sie ein Mensch?")

    assert result.say[0] == SAY_NOT_A_PERSON
    assert_went_to_the_team(result, session, state)


def test_a_card_that_cannot_be_written_books_nothing(session, tenant_id, state):
    """The guest's yes then moves no money, the audit row goes with the card,
    and the team takes the call."""
    rule(session, tenant_id)
    state.slots["phone"] = "keine Nummer"
    agent = loop(session)
    agent.run_turn(state, COMPLAINT)

    result = agent.run_turn(state, "Ja")

    assert result.say == [SAY_ESCALATION]
    assert_went_to_the_team(result, session, state)


def test_the_yes_keeps_what_the_guest_heard_when_the_row_changes(
    session, tenant_id, state
):
    row = rule(session, tenant_id)
    agent = loop(session)
    agent.run_turn(state, COMPLAINT)
    row.remedy_cents = 9999
    row.active = False
    session.commit()

    result = agent.run_turn(state, "Ja")

    assert result.say == [GRANTED]
    assert booked(session)[0].payload["remedy_cents"] == 345


# -- a call that ends on the offer ------------------------------------------------


def test_a_call_that_ends_on_the_offer_leaves_the_complaint_on_a_card(
    session, tenant_id, state
):
    rule(session, tenant_id)
    agent = loop(session)
    agent.run_turn(state, COMPLAINT)

    agent.offer_unanswered(state)
    agent.offer_unanswered(state)

    card = cards(session, state)
    assert len(card) == 1
    assert card[0].reason == "complaint"
    assert COMPLAINT in card[0].summary
    assert state.stage == "callback"
    assert booked(session) == []


def test_a_call_without_an_offer_leaves_no_card(session, state):
    loop(session).offer_unanswered(state)

    assert cards(session, state) == []
    assert state.stage == "start"


def test_the_text_phone_closes_an_open_offer_with_its_call(session, tenant_id):
    rule(session, tenant_id)
    tenant = resolve_tenant(session, "Testbetrieb")
    call = SimCall(
        session, tenant, llm=FakeLLM([]), now=OPEN_NOW, caller_id="0721 5551234"
    )
    assert call.say(COMPLAINT).say == [OFFER]

    assert call.finish().outcome == "callback"


# -- after the recording question (#526) ------------------------------------------


def asked(session, tenant_id) -> ConversationState:
    """The first sentence of the phone line asked for the recording (L2)."""
    return _state(session, tenant_id, recording="asked")


def consents(session) -> list[str]:
    return list(session.scalars(select(ConsentLog.answer)).all())


@pytest.mark.parametrize(
    ("answer", "logged"),
    [
        ("Nein,", "unclear"),
        ("Ja,", "unclear"),
        ("Nein danke, nicht aufnehmen.", "no"),
        # Without its comma, as speech recognition may leave it.
        ("Nein", "unclear"),
    ],
)
def test_the_answer_to_the_recording_question_leaves_the_complaint_to_its_rule(
    session, tenant_id, answer, logged
):
    """The same offer as without the question: the rules read what the
    sentence holds besides the answer. More than the answer is no explicit
    yes, so nothing is recorded."""
    rule(session, tenant_id)
    state = asked(session, tenant_id)

    result = loop(session).run_turn(
        state, f"{answer} ich bin unzufrieden, das Essen war kalt."
    )

    assert result.say == [OFFER]
    assert state.remedy_offer.complaint == "ich bin unzufrieden, das Essen war kalt."
    assert state.recording == "off"
    assert session.scalars(select(Recording)).all() == []
    assert consents(session) == [logged]


def test_the_same_sentence_without_the_question_keeps_its_no_a_doubt(
    session, tenant_id, state
):
    """Only the answer to the recording question is cut off: a no the core
    cannot place still leaves the complaint with the team."""
    rule(session, tenant_id)

    result = loop(session).run_turn(
        state, "Nein, ich bin unzufrieden, das Essen war kalt."
    )

    assert_went_to_the_team(result, session, state)


def test_a_bare_no_is_answered_alone_and_the_complaint_comes_next(session, tenant_id):
    rule(session, tenant_id)
    state = asked(session, tenant_id)
    agent = loop(session)  # no model turn prepared: it is not asked

    assert agent.run_turn(state, "Nein.").say == [SAY_WHAT_CAN_I_DO]
    assert state.remedy_offer is None
    assert consents(session) == ["no"]

    assert agent.run_turn(state, COMPLAINT).say == [OFFER]


@pytest.mark.parametrize(
    ("phrases", "offered"), [(["nicht warm"], True), (["warm"], False)]
)
def test_a_negation_of_the_complaint_stays_behind_the_answer(
    session, tenant_id, phrases, offered
):
    """The no answers the question, the "nicht" belongs to the complaint: the
    rules still read it, inside a phrase or as a doubt."""
    rule(session, tenant_id, phrases=phrases)
    state = asked(session, tenant_id)

    result = loop(session).run_turn(
        state, "Nein, ich bin unzufrieden, das Essen war nicht warm."
    )

    if offered:
        assert result.say == [OFFER]
    else:
        assert_went_to_the_team(result, session, state)


# -- no guessing ------------------------------------------------------------------


@pytest.mark.parametrize(
    "said",
    [
        "Das Essen war kalt",
        "DAS ESSEN WAR KALT!",
        "Das Essen war, ähm, kalt.",
    ],
)
def test_a_phrase_matches_as_whole_words(session, tenant_id, said):
    row = rule(session, tenant_id)

    assert matching_rule(session, tenant_id, said).id == row.id


def test_a_rule_matches_on_any_of_its_phrases(session, tenant_id):
    row = rule(session, tenant_id, phrases=["kalt", "viel zu spät"])

    assert matching_rule(session, tenant_id, "Das kam viel zu spät").id == row.id


@pytest.mark.parametrize(
    "said",
    [
        "Das Essen war eiskalt",
        "Die Kalte Platte war zu klein",
        "Das Essen war lauwarm",
        "",
    ],
)
def test_no_part_of_a_word_is_a_match(session, tenant_id, said):
    rule(session, tenant_id)

    assert matching_rule(session, tenant_id, said) is None


@pytest.mark.parametrize(
    "said",
    [
        "Das Essen war nicht kalt, aber zu salzig.",
        "Kalt war es nie.",
        "Das Essen war kalt und es gab keine Serviette.",
        "Wenn das Essen kalt ist, beschwere ich mich.",
        "Das Essen war vielleicht kalt.",
        # A no the core cannot place: only the answer to the recording question
        # is cut off before the rules read the sentence (#526).
        "Nein, das Essen war kalt.",
    ],
)
def test_a_sentence_that_rules_something_out_or_only_supposes_is_no_match(
    session, tenant_id, said
):
    rule(session, tenant_id)

    assert matching_rule(session, tenant_id, said) is None


def test_a_negation_inside_the_phrase_of_the_row_counts(session, tenant_id):
    row = rule(session, tenant_id, phrases=["nicht warm"])

    assert matching_rule(session, tenant_id, "Das war nicht warm").id == row.id


def test_two_rules_that_match_are_a_doubt(session, tenant_id):
    rule(session, tenant_id, name="Eins", phrases=["kalt"])
    rule(session, tenant_id, name="Zwei", phrases=["spät"])

    assert matching_rule(session, tenant_id, "Kalt und spät") is None
    assert matching_rule(session, tenant_id, "Viel zu spät").name == "Zwei"


def test_a_rule_switched_off_does_not_match(session, tenant_id):
    rule(session, tenant_id, active=False)

    assert matching_rule(session, tenant_id, "Das war kalt") is None


def test_a_new_row_stays_off_until_someone_switches_it_on(session, tenant_id):
    row = ComplaintRule(
        tenant_id=tenant_id,
        name="Testregel",
        phrases=["kalt"],
        remedy_cents=345,
        offer_say=OFFER,
        granted_say=GRANTED,
    )
    session.add(row)
    session.commit()
    session.refresh(row)

    assert row.active is False


@pytest.mark.parametrize("blank", ["", "   ", "?!"])
def test_a_phrase_without_a_word_never_matches(session, tenant_id, blank):
    rule(session, tenant_id, phrases=[blank])

    assert matching_rule(session, tenant_id, "Das Essen war kalt") is None


def test_a_rule_of_another_tenant_is_not_read(session, tenant_id):
    other = uuid.UUID(
        seed(session, tenant_name="Anderer Betrieb", timezone="Europe/Berlin").tenant_id
    )
    rule(session, other)

    assert matching_rule(session, tenant_id, "Das Essen war kalt") is None
    assert matching_rule(session, other, "Das Essen war kalt") is not None


def test_a_call_of_one_tenant_never_hears_the_rule_of_another(
    session, tenant_id, state
):
    other = uuid.UUID(
        seed(session, tenant_name="Anderer Betrieb", timezone="Europe/Berlin").tenant_id
    )
    rule(session, other)

    result = loop(session).run_turn(state, COMPLAINT)

    assert_went_to_the_team(result, session, state)


@pytest.mark.parametrize(
    "said",
    [
        # Something else the team has to hear.
        "Ich bin unzufrieden, das Essen war kalt, ich möchte stornieren.",
        "Ich bin unzufrieden, das Essen war kalt, ich will mit einem Menschen sprechen.",
        # Health: no fixed sentence answers it.
        "Ich bin unzufrieden, das Essen war kalt, ich habe eine Erdnussallergie.",
        "Ich bin unzufrieden, das Essen war kalt, und Erdnüsse waren drin.",
        "Ich bin unzufrieden, das Essen war kalt, mir wurde danach übel.",
        "Ich bin unzufrieden, das Essen war kalt, Spuren von Nüssen auch noch.",
        "Ich bin unzufrieden, das Essen war kalt und auch noch kontaminiert.",
        "Ich bin unzufrieden, das Essen war kalt, ich darf kein Gluten.",
        "Ich bin unzufrieden, das Essen war kalt, ich habe Zöliakie.",
        # A legal step.
        "Ich bin unzufrieden, das Essen war kalt, mein Anwalt meldet sich.",
        # Asked, not said.
        "Ich bin unzufrieden, war das Essen kalt?",
        # A wish of its own: the call ends with the remedy, and it would be
        # lost (Codex PR #479). Speech recognition sets no comma to rely on.
        "Ich bin unzufrieden, das Essen war kalt und ich möchte zwei Suppen "
        "zum Abholen bestellen.",
        "ich bin unzufrieden das essen war kalt ich möchte zwei suppen bestellen",
        "Ich möchte mich beschweren, das Essen war kalt, und zwei Suppen bestellen.",
        "ich möchte mich beschweren das essen war kalt und zwei suppen mitnehmen",
        "ich möchte suppe bestellen und mich beschweren das essen war kalt",
        "Ich bin unzufrieden, das Essen war kalt, ich hätte gern einen Tisch.",
        "Ich bin unzufrieden, das Essen war kalt, bitte einmal die 23.",
        "Ich bin unzufrieden, das Essen war kalt, ich will mein Geld zurück.",
    ],
)
def test_a_sentence_with_more_in_it_goes_to_the_team(session, tenant_id, state, said):
    rule(session, tenant_id)

    result = loop(session).run_turn(state, said)

    assert result.say == [SAY_ESCALATION]
    assert result.ended is True
    assert state.remedy_offer is None
    assert booked(session) == []


@pytest.mark.parametrize(
    "said",
    [
        # The only wish in these is the complaint itself.
        "Ich möchte mich beschweren, das Essen war kalt.",
        "ich möchte mich beschweren das essen war kalt",
        "Ich will mich über die Lieferung beschweren, das Essen war kalt.",
        "Ich möchte das reklamieren, das Essen war kalt.",
        "Ich hätte gerne eine Beschwerde, das Essen war kalt.",
        # What already happened is no wish.
        "Die Lieferung war kalt, ich bin unzufrieden.",
        "Die Bestellung war gestern kalt, ich bin unzufrieden.",
        "Ich habe gestern bestellt, das Essen war kalt, ich bin unzufrieden.",
        "Ich habe vorhin abgeholt, das Essen war kalt, das war ärgerlich.",
    ],
)
def test_a_complaint_that_only_tells_what_happened_gets_the_offer(
    session, tenant_id, state, said
):
    rule(session, tenant_id)

    assert loop(session).run_turn(state, said).say == [OFFER]


def test_a_wish_only_the_model_saw_keeps_its_handover(session, tenant_id, state):
    rule(session, tenant_id)
    call = ToolCall(name="transfer_to_team", args={"reason": "complaint"})

    result = loop(session, [LLMTurn(tool_call=call)]).run_turn(
        state, "Das Essen war gestern kalt, ich möchte zwei Suppen abholen."
    )

    assert result.ended is True
    assert state.stage == "transferred"
    assert state.remedy_offer is None


def test_the_question_for_a_person_keeps_its_truthful_answer(session, tenant_id, state):
    rule(session, tenant_id)

    result = loop(session).run_turn(
        state, "Sind Sie ein Roboter? Ich bin unzufrieden, das Essen war kalt."
    )

    assert result.say[0] == SAY_NOT_A_PERSON
    assert_went_to_the_team(result, session, state)


def test_a_withheld_number_goes_to_the_team(session, tenant_id):
    """Nobody could carry the remedy out."""
    rule(session, tenant_id)
    state = ConversationState(
        call_id=_call(session, tenant_id), tenant_id=tenant_id, caller_phone=None
    )

    result = loop(session).run_turn(state, COMPLAINT)

    assert_went_to_the_team(result, session, state)


@pytest.mark.parametrize(
    "open_business",
    [
        {"intent": "pickup"},
        {"intent": "reservation"},
        {"stage": "readback_pending"},
        {"stage": "confirmed"},
        {"stage": "callback"},
        {"cart": [{"menu_item_id": str(uuid.uuid4()), "quantity": 1}]},
        {"seen_items": {"x": {"number": "23", "name": "Suppe"}}},
        {"held_call": {"name": "draft_order", "args": {}}},
        {"allergies": [Allergy(ingredient="Erdnuss")]},
        {"allergy_talk": True},
    ],
)
def test_a_call_with_other_business_goes_to_the_team(session, tenant_id, open_business):
    """The call ends with the remedy: an order or a table of the same call
    would be lost without a word."""
    rule(session, tenant_id)
    state = _state(session, tenant_id, **open_business)

    result = loop(session).run_turn(state, COMPLAINT)

    assert result.ended is True
    assert state.remedy_offer is None
    assert booked(session) == []


def test_a_slot_the_guest_gave_is_other_business(session, tenant_id, state):
    rule(session, tenant_id)
    state.slots["party_size"] = 4

    result = loop(session).run_turn(state, COMPLAINT)

    assert_went_to_the_team(result, session, state)


def test_a_wish_from_before_keeps_the_models_handover(session, tenant_id, state):
    rule(session, tenant_id)
    state.intent = "reservation"
    call = ToolCall(name="transfer_to_team", args={"reason": "complaint"})

    result = loop(session, [LLMTurn(tool_call=call)]).run_turn(
        state, "Das Essen war gestern kalt."
    )

    assert_went_to_the_team(result, session, state)
