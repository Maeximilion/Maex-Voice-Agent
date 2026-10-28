"""The caller's allergy, read and written by the core, and their consent (T-11.1).

Legal check L13 (Maxi, 20.10.2026): the agent asks "Darf ich die Allergie als
Hinweis für die Küche notieren?"; only an explicit "ja" stores "WICHTIG: Keine
<Zutat>. Grund: Allergie", on "nein" only "ohne <Zutat>"; the consent is
logged per call. Redesign of 26.10.2026 (Maxi, #368): an allergy never travels
in free text. The core reads it from the guest's own sentence into the state
(`agent/allergies.py`), refuses a model note that names one, and writes it
onto the draft itself; before the first draft that needs it, it asks
"Wogegen?" for an allergy not read in full and then for the consent, holding
the draft until the answer. Caller sentences in German: the yes detector and
the parser read German.
"""

import uuid
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from api.agent import allergies, guards
from api.agent.consent import ALLERGY_QUESTION, consent_answer
from api.agent.dispatch import ToolResult
from api.agent.llm import FakeLLM, LLMTurn, ToolCall
from api.agent.loop import (
    SAY_CALLBACK_NUMBER,
    SAY_HUMAN_ANSWER,
    SAY_NOT_A_PERSON,
    SAY_ORDER_NOT_PLACED,
    SAY_TABLE_NOT_BOOKED,
    SUMMARY_KITCHEN_NOTE,
    SUMMARY_OPEN_ORDER,
    SUMMARY_OPEN_TABLE,
    ConversationLoop,
)
from api.agent.state import Allergy, ConversationState, apply_tool_result
from api.domain.menu.search import SAY_ALLERGY_WHICH
from api.models import Call, Callback, ConsentLog, Reservation
from scripts.seed import seed

BERLIN = ZoneInfo("Europe/Berlin")
NOW = datetime(2026, 9, 15, 8, 0, tzinfo=BERLIN)  # before opening: no team
CALLER = "+4972215551234"
REASONED = "WICHTIG: Keine Erdnuss. Grund: Allergie"
SESAME = "WICHTIG: Keine Sesam. Grund: Allergie"
TABLE = "Tisch für vier auf Müller"


@pytest.fixture
def session(migrated_db_url):
    engine = create_engine(migrated_db_url)
    with Session(engine) as s:
        yield s
    engine.dispose()


@pytest.fixture
def state(session):
    tenant_id = uuid.UUID(
        seed(session, tenant_name="Testbetrieb", timezone="Europe/Berlin").tenant_id
    )
    call = Call(
        tenant_id=tenant_id,
        external_session_id="ext",
        started_at=NOW,
        delete_after=date(2026, 9, 15),
    )
    session.add(call)
    session.commit()
    return ConversationState(call_id=call.id, tenant_id=tenant_id, caller_phone=CALLER)


def table(note: str | None = None) -> ToolCall:
    args = {
        "guest_name": "Müller",
        "phone": CALLER,
        "party_size": 4,
        "reserved_for": "2026-09-16T19:00:00+02:00",
    }
    return ToolCall("create_reservation", {**args, **({"note": note} if note else {})})


def turn(session, state, said: str, *turns: LLMTurn) -> list[str]:
    loop = ConversationLoop(session, FakeLLM(list(turns)), "system", now=NOW)
    return loop.run_turn(state, said).say


def logged(session) -> list[tuple[str, str]]:
    return [
        (row.question, row.answer)
        for row in session.scalars(select(ConsentLog).order_by(ConsentLog.answered_at))
    ]


def reservations(session) -> list[Reservation]:
    session.expire_all()
    return list(session.scalars(select(Reservation)))


def read(session, state, said: str) -> list[tuple[str | None, bool]]:
    allergies.note_allergies(session, state, said)
    return [(allergy.ingredient, allergy.own) for allergy in state.allergies]


@pytest.mark.parametrize(
    "said",
    [
        "Ja.",
        "Ja, gerne.",
        "Ja, notieren Sie das bitte.",
        "Ja, auf jeden Fall.",
        "Okay.",
    ],
)
def test_an_explicit_yes_is_consent(said):
    assert consent_answer(said) == "yes"


@pytest.mark.parametrize(
    "said", ["Nein.", "Nein, danke.", "Lieber nicht.", "Nein, nicht notieren."]
)
def test_a_plain_no_is_a_no(said):
    assert consent_answer(said) == "no"


@pytest.mark.parametrize(
    "said",
    [
        # A yes with more is the start of something else, no explicit consent.
        "Ja, und noch eine Cola.",
        "Wie bitte?",
        "Was heißt das?",
        "Notieren Sie das?",
        "Nein, doch.",
        "Ja, 2",
        "",
    ],
)
def test_anything_else_is_unclear(said):
    assert consent_answer(said) == "unclear"


# -- Reading the guest's sentence -------------------------------------------


@pytest.mark.parametrize(
    ("said", "noted"),
    [
        ("ich habe eine Erdnussallergie", [("Erdnuss", True)]),
        # The caller's "ja" counts for their own child.
        ("meine Tochter hat eine Erdnussallergie", [("Erdnuss", True)]),
        ("Die 13, mein Sohn hat eine Erdnussallergie.", [("Erdnuss", True)]),
        # Another adult's: never with the reason (L13).
        ("meine Frau hat eine Erdnussallergie", [("Erdnuss", False)]),
        ("ohne Zwiebeln, meine Frau hat eine Erdnussallergie", [("Erdnuss", False)]),
        # Codex PR #368, P1: the subject before the parser's phrase counts.
        ("er hat eine Nussallergie", [("Nuss", False)]),
        ("sie ist allergisch gegen Nüsse", [("Nüsse", False)]),
        ("unsere Oma verträgt keine Nüsse", [("Nüsse", False)]),
        ("die 13 bitte, mein Mann hat eine Nussallergie", [("Nuss", False)]),
        # Codex PR #368, P1: a later clause does not change whose it is.
        ("er hat eine Nussallergie, ich möchte Besteck dazu", [("Nuss", False)]),
        ("meine Frau hat eine Nussallergie. Ich nehme die 23", [("Nuss", False)]),
        # A person named with the removal only leaves the allergy the caller's.
        (
            "ohne Zwiebeln für meine Frau, ich habe eine Erdnussallergie",
            [("Erdnuss", True)],
        ),
        (
            "ohne Zwiebeln für meine Frau und ich habe eine Erdnussallergie",
            [("Erdnuss", True)],
        ),
        # Codex PR #368: what is said beside the allergy is no part of it.
        ("Erdnussallergie. Bitte Besteck dazu", [("Erdnuss", True)]),
        # An allergen list stays one allergy.
        (
            "Ich bin gegen Milch, Ei und Nüsse allergisch",
            [("Milch, Ei und Nüsse", True)],
        ),
        ("Die 13 bitte, Erdnussallergie", [("Erdnuss", True)]),
        ("Nuss- und Sesamallergie", [("Nuss und Sesam", True)]),
        # Codex PR #368, P1 4378680664: each sentence with its own owner.
        (
            "Ich habe eine Erdnussallergie. Meine Frau hat eine Sesamallergie.",
            [("Erdnuss", True), ("Sesam", False)],
        ),
        # In one sentence, which is whose is not read: neither keeps the
        # reason, the safe side for the other adult.
        (
            "Ich habe eine Erdnussallergie, meine Frau eine Sesamallergie.",
            [("Erdnuss und Sesam", False)],
        ),
        (
            "Ich habe eine Erdnussallergie. Sonst keine Allergien.",
            [("Erdnuss", True)],
        ),
        # Codex PR #368, P1 4378680652: the second sentence is not dropped;
        # not read in full, it is asked about.
        (
            "Erdnussallergie. Keine Milch wegen Unverträglichkeit.",
            [("Erdnuss", True), (None, True)],
        ),
    ],
)
def test_the_core_reads_the_allergy_and_whose_it_is(session, state, said, noted):
    assert read(session, state, said) == noted


@pytest.mark.parametrize(
    "said",
    [
        # Codex PR #368 (P1): more follows than the parser reads (#361).
        "Ich bin gegen Erdnüsse allergisch und gegen Sesam",
        "Erdnussallergie und Sesam auch nicht",
        "ohne Zwiebeln, gegen Erdnüsse allergisch und gegen Sesam",
        "ich habe eine Nussallergie, er möchte Besteck",
        "Ich habe eine Allergie",
        # #361: the intolerance was lost, "vertrage" only counted with a
        # negation right behind it.
        "Pho Bo, Sesam vertrage ich auch nicht",
        # Read whole, a dish or a name in front became an allergen; read
        # behind the dish, a list loses its first allergens. Where the two
        # differ, the core asks.
        "Pho Bo und Sommerrollen, Erdnussallergie",
        "Tisch für vier auf Müller, Erdnussallergie",
        "Milch, Ei und Nussallergie",
        # A sentence without an allergy word carries the list on.
        "Erdnussallergie. Und gegen Sesam.",
    ],
)
def test_an_allergy_not_read_in_full_waits_for_wogegen(session, state, said):
    assert read(session, state, said) == [(None, True)]
    assert allergies.question(state) == SAY_ALLERGY_WHICH


@pytest.mark.parametrize(
    "said",
    [
        "Pho Bo, ich habe keine Allergie.",
        "Ich habe keine Allergie und moechte Pho Bo zum Abholen.",
        "Allergien habe ich keine",
        "ohne Zwiebeln",
        "Tisch für vier auf Müller",
    ],
)
def test_a_sentence_without_an_allergy_notes_none(session, state, said):
    assert read(session, state, said) == []
    assert allergies.question(state) is None


@pytest.mark.parametrize(
    ("answer", "noted"),
    [
        ("Erdnüsse.", [("Erdnüsse", True)]),
        ("Gegen Erdnüsse und Sesam.", [("Erdnüsse und Sesam", True)]),
        ("Erdnüsse, aber ohne Zwiebeln.", [("Erdnüsse", True)]),
        # Withdrawn: no allergy after all.
        ("Keine.", []),
        ("Ich habe doch keine Allergie.", []),
        # Not read: the question stays open.
        ("Weiß ich nicht.", [(None, True)]),
        ("Keine, aber ich bin gegen Sesam allergisch.", [(None, True)]),
        # An order, not an ingredient (Review PR #139).
        ("Eine Cola bitte.", [(None, True)]),
        ("Nummer 12.", [(None, True)]),
        # A sentence of its own, not a list of ingredients.
        ("Moment, da muss ich kurz überlegen.", [(None, True)]),
        ("Erdnüsse, glaube ich.", [(None, True)]),
        # Issue #207: a hesitation is no part of the ingredient, and alone or
        # unsure it neither fills nor withdraws the allergy.
        ("Ähm, Erdnüsse.", [("Erdnüsse", True)]),
        ("Hm.", [(None, True)]),
        ("Schwer zu sagen.", [(None, True)]),
        ("Wie bitte?", [(None, True)]),
    ],
)
def test_the_answer_to_wogegen_fills_the_open_allergy(session, state, answer, noted):
    read(session, state, "Ich habe eine Allergie.")
    guards.note_said(state, "Gern. Wogegen sind Sie allergisch?")

    assert read(session, state, answer) == noted


@pytest.mark.parametrize(
    ("answer", "noted"),
    [
        # Codex PR #368, P1 4379622793: each sentence of the answer with its
        # own owner; the caller's peanut was lost, or the husband's sesame
        # kept the caller's reason.
        (
            "Erdnüsse. Mein Mann hat eine Sesamallergie.",
            [("Erdnüsse", True), ("Sesam", False)],
        ),
        (
            "Ich bin gegen Erdnüsse allergisch. Mein Mann hat eine Sesamallergie.",
            [("Erdnüsse", True), ("Sesam", False)],
        ),
        # One sentence that names more than it reads: the husband's allergy
        # is noted, the question for the caller's stays open.
        (
            "Erdnüsse, und mein Mann hat eine Sesamallergie.",
            [(None, True), ("Sesam", False)],
        ),
        ("Mein Mann ist gegen Sesam allergisch.", [("Sesam", False)]),
        ("Keine. Aber mein Mann hat eine Sesamallergie.", [("Sesam", False)]),
        # Codex PR #368, P1 4379762029: a sentence without an allergy word
        # carries the list of the one before it on.
        ("Erdnüsse. Und gegen Sesam.", [("Erdnüsse und Sesam", True)]),
        (
            "Erdnüsse. Mein Mann hat eine Sesamallergie. Und gegen Milch.",
            [("Erdnüsse", True), (None, False)],
        ),
        ("Erdnüsse. Danke.", [("Erdnüsse", True)]),
        # More than the answer and not read in full: the question stays
        # open, the model reads the sentence.
        ("Erdnüsse und bitte um 20 Uhr.", [(None, True)]),
    ],
)
def test_every_sentence_of_an_answer_keeps_its_owner(session, state, answer, noted):
    read(session, state, "Ich habe eine Allergie.")
    guards.note_said(state, SAY_ALLERGY_WHICH)

    assert read(session, state, answer) == noted


def test_the_answer_for_another_adult_keeps_the_reason_off(session, state):
    read(session, state, "Mein Mann hat eine Allergie.")
    guards.note_said(state, SAY_ALLERGY_WHICH)

    assert read(session, state, "Gegen Sesam.") == [("Sesam", False)]


def test_the_same_allergy_is_kept_once(session, state):
    read(session, state, "Ich habe eine Erdnussallergie.")
    assert read(session, state, "Ich habe eine Erdnussallergie.") == [("Erdnuss", True)]


# -- Writing it onto the draft -----------------------------------------------


def test_the_core_writes_the_allergy_onto_the_dish_and_the_rest_onto_the_order():
    """An allergy said with a dish a clear search found goes onto that line;
    one said without a dish, or with a dish the order does not hold, onto the
    note of the whole order (D24)."""
    state = ConversationState(call_id=uuid.uuid4(), tenant_id=uuid.uuid4())
    pho, ente = str(uuid.uuid4()), str(uuid.uuid4())
    state.allergies = [
        Allergy(ingredient="Erdnuss"),
        Allergy(ingredient="Sesam", own=False),
    ]
    found = {
        "match_type": "exact_number",
        "results": [{"menu_item_id": pho, "number": "13", "name": "Pho Bo"}],
        "wish": {"kind": "allergy", "ingredient": "Erdnuss", "text": REASONED},
    }
    apply_tool_result(state, "search_menu", ToolResult(ok=True, data=found))
    assert state.allergies[0].item == pho

    call = ToolCall(
        "draft_order",
        {
            "items": [
                {"menu_item_id": pho, "quantity": 1, "note": "ohne Zwiebeln"},
                {"menu_item_id": ente, "quantity": 1},
            ],
            "note": "Besteck dazu",
        },
    )
    state.allergy_consent = "yes"
    noted = allergies.noted_call(state, call).args
    assert noted["items"][0]["note"] == f"{REASONED}. Ohne Zwiebeln"
    assert "note" not in noted["items"][1]
    assert noted["note"] == "ohne Sesam, Besteck dazu"

    state.allergy_consent = "no"
    noted = allergies.noted_call(state, call).args
    assert noted["items"][0]["note"] == "ohne Erdnuss, ohne Zwiebeln"
    # The model's draft is left as it was: the state keeps it without them.
    assert call.args["items"][0]["note"] == "ohne Zwiebeln"


def test_an_allergy_of_a_dish_the_order_lost_goes_onto_the_order():
    state = ConversationState(call_id=uuid.uuid4(), tenant_id=uuid.uuid4())
    state.allergies = [Allergy(ingredient="Erdnuss", item=str(uuid.uuid4()))]
    state.allergy_consent = "no"
    call = ToolCall(
        "draft_order", {"items": [{"menu_item_id": str(uuid.uuid4()), "quantity": 1}]}
    )

    assert allergies.noted_call(state, call).args["note"] == "ohne Erdnuss"


def test_a_line_too_long_keeps_the_allergy_and_loses_the_model_s_part():
    state = ConversationState(call_id=uuid.uuid4(), tenant_id=uuid.uuid4())
    item = str(uuid.uuid4())
    state.allergies = [Allergy(ingredient="Erdnuss", item=item)]
    state.allergy_consent = "yes"
    call = ToolCall(
        "draft_order",
        {"items": [{"menu_item_id": item, "quantity": 1, "note": "x" * 190}]},
    )

    assert allergies.noted_call(state, call).args["items"][0]["note"] == REASONED


# -- The questions of the core around a draft --------------------------------


def test_the_first_draft_with_an_own_allergy_waits_for_the_question(session, state):
    said = turn(
        session,
        state,
        f"{TABLE}, ich habe eine Erdnussallergie",
        LLMTurn(tool_call=table()),
    )

    assert said == [ALLERGY_QUESTION]
    assert reservations(session) == []
    assert state.held_call is not None
    assert state.last_say == ALLERGY_QUESTION
    assert logged(session) == []


def test_after_a_yes_the_draft_carries_the_reason_and_is_read_out(session, state):
    turn(
        session,
        state,
        f"{TABLE}, ich habe eine Erdnussallergie",
        LLMTurn(tool_call=table()),
    )

    # The model is not asked: the core carries the draft out and reads it out.
    said = turn(session, state, "Ja.")

    [reservation] = reservations(session)
    assert reservation.note == REASONED
    assert reservation.status == "draft"
    assert said == [state.readback]
    assert state.readback_heard
    assert logged(session) == [(ALLERGY_QUESTION, "yes")]
    assert state.held_call is None


def test_after_a_no_only_the_removal_is_stored(session, state):
    turn(
        session,
        state,
        f"{TABLE}, ich habe eine Erdnussallergie",
        LLMTurn(tool_call=table()),
    )

    said = turn(session, state, "Nein, danke.")

    [reservation] = reservations(session)
    assert reservation.note == "ohne Erdnuss"
    assert "Allergie" not in said[0]
    assert logged(session) == [(ALLERGY_QUESTION, "no")]


def test_an_unclear_answer_drops_the_draft_and_the_model_reads_it(session, state):
    """Unclear is no consent, and the sentence was likely about something
    else: the model gets it, and the draft it makes again finds the allergy
    in the state, without its reason (Codex PR #368, P1)."""
    turn(
        session,
        state,
        f"{TABLE}, ich habe eine Erdnussallergie",
        LLMTurn(tool_call=table()),
    )

    said = turn(
        session,
        state,
        "Moment, wir sind doch fünf.",
        LLMTurn(tool_call=table()),
        LLMTurn(say="Gern."),
    )

    [reservation] = reservations(session)
    assert reservation.note == "ohne Erdnuss"
    assert said == ["Gern."]
    assert logged(session) == [(ALLERGY_QUESTION, "unclear")]


def test_a_yes_with_a_second_allergy_is_unclear_and_keeps_both(session, state):
    turn(
        session,
        state,
        f"{TABLE}, ich habe eine Erdnussallergie",
        LLMTurn(tool_call=table()),
    )

    turn(
        session,
        state,
        "Ja, und ich bin auch gegen Sesam allergisch.",
        LLMTurn(tool_call=table()),
        LLMTurn(say="Notiert."),
    )

    [reservation] = reservations(session)
    assert reservation.note == "ohne Erdnuss, ohne Sesam"
    assert logged(session) == [(ALLERGY_QUESTION, "unclear")]


@pytest.mark.parametrize("limit", ["turn", "call"])
def test_a_held_draft_that_ends_after_a_limit_goes_to_the_team(session, state, limit):
    """Codex PR #368, P1: the draft carried out after the answer takes longer
    than the guest may wait. As after any slow tool, the call goes to the team
    instead of its read-back; the draft stays a draft."""
    turn(
        session,
        state,
        f"{TABLE}, ich habe eine Erdnussallergie",
        LLMTurn(tool_call=table()),
    )
    now = [0.0]
    loop = ConversationLoop(
        session,
        FakeLLM([]),
        "system",
        now=NOW,
        clock=lambda: now[0],
        turn_deadline_seconds=10 if limit == "turn" else None,
        max_call_seconds=10 if limit == "call" else 600,
    )
    dispatch = loop._dispatch

    def slow(state, name, args):
        if name == "create_reservation":
            now[0] += 11
        return dispatch(state, name, args)

    loop._dispatch = slow  # type: ignore[method-assign]

    result = loop.run_turn(state, "Ja.")

    assert result.ended
    assert state.readback not in result.say
    assert [r.status for r in reservations(session)] == ["draft"]


def test_the_call_limit_comes_before_the_question_is_put_again(session, state):
    """Codex PR #368, P1: past the call limit, a "Ja" to "Sind Sie noch da?"
    goes to the team instead of hearing the consent question again."""
    turn(
        session,
        state,
        f"{TABLE}, ich habe eine Erdnussallergie",
        LLMTurn(tool_call=table()),
    )
    guards.note_said(state, "Sind Sie noch da?")
    now = [0.0]
    loop = ConversationLoop(
        session,
        FakeLLM([]),
        "system",
        now=NOW,
        clock=lambda: now[0],
        max_call_seconds=10,
    )
    now[0] = 11

    result = loop.run_turn(state, "Ja.")

    assert result.ended
    assert ALLERGY_QUESTION not in result.say
    assert reservations(session) == []


def test_an_answer_that_goes_to_the_team_is_logged_and_stores_nothing(session, state):
    """The question was asked, so its answer is logged, also when the sentence
    hands the call over; the draft that waited is never carried out."""
    state.slots["phone"] = CALLER
    turn(
        session,
        state,
        f"{TABLE}, ich habe eine Erdnussallergie",
        LLMTurn(tool_call=table()),
    )

    turn(session, state, "Nein, ich will mit einem Menschen sprechen.")

    assert reservations(session) == []
    assert logged(session) == [(ALLERGY_QUESTION, "unclear")]
    assert session.scalars(select(Callback)).one().reason == "human_requested"


def test_the_question_is_asked_once_per_call(session, state):
    turn(
        session,
        state,
        "Tisch auf Müller, ich habe eine Erdnussallergie",
        LLMTurn(tool_call=table()),
    )
    turn(session, state, "Ja.")

    said = turn(
        session,
        state,
        "Und gegen Sesam bin ich auch allergisch.",
        LLMTurn(tool_call=table()),
        LLMTurn(say="Notiert."),
    )

    assert said == ["Notiert."]
    assert len(logged(session)) == 1
    # Codex PR #368: both allergens on the note.
    assert reservations(session)[-1].note == f"{REASONED}. {SESAME}"


def test_an_answer_to_another_question_is_no_consent(session, state):
    """Codex PR #368, P1: the phone line asks "Sind Sie noch da?" after a
    silence. A "Ja" to that is no consent: the question is asked again."""
    turn(
        session,
        state,
        f"{TABLE}, ich habe eine Erdnussallergie",
        LLMTurn(tool_call=table()),
    )
    guards.note_said(state, "Sind Sie noch da?")

    said = turn(session, state, "Ja.")

    assert said == [ALLERGY_QUESTION]
    assert logged(session) == []
    assert reservations(session) == []
    assert state.held_call is not None

    turn(session, state, "Nein.")
    [reservation] = reservations(session)
    assert reservation.note == "ohne Erdnuss"
    assert logged(session) == [(ALLERGY_QUESTION, "no")]


def test_an_allergy_said_while_the_draft_waits_reaches_it(session, state):
    """Codex PR #368, P1 4378523387: "Ja, und ich bin auch gegen Sesam
    allergisch" after "Sind Sie noch da?" while the draft waits for the
    consent lost the sesame. The sentence feeds the state like any other;
    with more in it than a yes, it goes to the model, and the draft the model
    makes again takes its notes from the state."""
    turn(
        session,
        state,
        f"{TABLE}, ich habe eine Erdnussallergie",
        LLMTurn(tool_call=table()),
    )
    guards.note_said(state, "Sind Sie noch da?")

    assert turn(
        session,
        state,
        "Ja, und ich bin auch gegen Sesam allergisch.",
        LLMTurn(tool_call=table()),
    ) == [ALLERGY_QUESTION]
    turn(session, state, "Ja.")

    [reservation] = reservations(session)
    assert reservation.note == f"{REASONED}. {SESAME}"
    assert logged(session) == [(ALLERGY_QUESTION, "yes")]


@pytest.mark.parametrize(
    "said", ["Ja.", "Ja, ich bin noch da.", "Bin noch dran.", "Nein."]
)
def test_a_bare_answer_to_another_question_puts_the_question_again(
    session, state, said
):
    turn(
        session,
        state,
        f"{TABLE}, ich habe eine Erdnussallergie",
        LLMTurn(tool_call=table()),
    )
    guards.note_said(state, "Sind Sie noch da?")

    assert turn(session, state, said) == [ALLERGY_QUESTION]
    assert state.held_call is not None


def test_a_correction_while_the_draft_waits_goes_to_the_model(session, state):
    """Codex PR #368, P1 4382597031: "Ja, aber bitte erst am 17. September um
    20 Uhr" after "Sind Sie noch da?" only had the consent question put
    again, and the next "Ja" booked the old time. The held draft is dropped,
    the model reads the sentence, and its new draft is held instead."""
    turn(
        session,
        state,
        f"{TABLE}, ich habe eine Erdnussallergie",
        LLMTurn(tool_call=table()),
    )
    guards.note_said(state, "Sind Sie noch da?")
    later = {**table().args, "reserved_for": "2026-09-17T20:00:00+02:00"}

    said = turn(
        session,
        state,
        "Ja, aber bitte erst am 17. September um 20 Uhr.",
        LLMTurn(tool_call=ToolCall("create_reservation", later)),
    )

    assert said == [ALLERGY_QUESTION]
    assert state.held_call is not None
    assert state.held_call["args"]["reserved_for"] == later["reserved_for"]
    turn(session, state, "Ja.")
    [reservation] = reservations(session)
    assert reservation.reserved_for.isoformat().startswith("2026-09-17T18:00")
    assert reservation.note == REASONED


def test_an_allergy_not_read_in_full_is_asked_for_before_the_consent(session, state):
    said = turn(
        session,
        state,
        f"{TABLE}, ich bin gegen Erdnüsse allergisch und gegen Sesam",
        LLMTurn(tool_call=table()),
    )
    assert said == [SAY_ALLERGY_WHICH]
    assert reservations(session) == []

    assert turn(session, state, "Erdnüsse und Sesam.") == [ALLERGY_QUESTION]
    turn(session, state, "Ja.")

    [reservation] = reservations(session)
    assert reservation.note == "WICHTIG: Keine Erdnüsse und Sesam. Grund: Allergie"


def test_an_allergy_withdrawn_on_wogegen_lets_the_draft_go_without_one(session, state):
    turn(session, state, f"{TABLE}, ich habe eine Allergie", LLMTurn(tool_call=table()))

    said = turn(session, state, "Keine.")

    [reservation] = reservations(session)
    assert reservation.note is None
    assert said == [state.readback]
    assert logged(session) == []


def test_an_unread_answer_to_wogegen_drops_the_draft(session, state):
    turn(session, state, f"{TABLE}, ich habe eine Allergie", LLMTurn(tool_call=table()))

    said = turn(
        session,
        state,
        "Moment, da muss ich kurz überlegen.",
        LLMTurn(say="Gern, ich warte."),
    )

    assert said == ["Gern, ich warte."]
    assert reservations(session) == []
    assert state.held_call is None
    assert allergies.question(state) == SAY_ALLERGY_WHICH


def test_another_adult_s_allergy_is_never_asked_about(session, state):
    said = turn(
        session,
        state,
        f"{TABLE}, meine Frau hat eine Erdnussallergie",
        LLMTurn(tool_call=table()),
        LLMTurn(say="Passt das so?"),
    )

    assert said == ["Passt das so?"]
    [reservation] = reservations(session)
    assert reservation.note == "ohne Erdnuss"
    assert logged(session) == []


def test_a_note_without_an_allergy_goes_through_as_it_is(session, state):
    turn(
        session,
        state,
        f"{TABLE} mit Hochstuhl",
        LLMTurn(tool_call=table("mit Hochstuhl")),
        LLMTurn(say="Passt das so?"),
    )

    [reservation] = reservations(session)
    assert reservation.note == "mit Hochstuhl"
    assert logged(session) == []


@pytest.mark.parametrize(
    "note",
    [
        "Erdnussallergie",
        REASONED,
        "meine Frau hat eine Erdnussallergie",
        "ohne Zwiebeln, gegen Erdnüsse allergisch und gegen Sesam",
        "keine Allergie",
    ],
)
def test_a_model_note_with_an_allergy_is_refused(session, state, note):
    """The model never writes an allergy: the draft is refused with a hint,
    nothing is stored and nobody is asked for consent. Its next draft without
    it goes through, with the allergy the core read from the guest."""
    said = turn(
        session,
        state,
        f"{TABLE}, ich habe eine Erdnussallergie",
        LLMTurn(tool_call=table(note)),
        LLMTurn(say="Einen Moment."),
    )

    assert said == ["Einen Moment."]
    assert reservations(session) == []
    assert logged(session) == []
    assert state.held_call is None


def test_a_callback_card_never_quotes_an_allergy(session, state):
    """The guest's sentence goes onto the card of a handover; one that names
    an allergy is health data the caller never agreed to have written there."""
    state.slots["phone"] = CALLER

    turn(
        session,
        state,
        "Ich will einen Menschen sprechen, ich habe eine Erdnussallergie",
    )

    callback = session.scalars(select(Callback)).one()
    assert "Erdnuss" not in callback.summary
    assert "Allergie" not in callback.summary


def callback(summary: str, reason: str = "out_of_scope") -> ToolCall:
    return ToolCall(
        "create_callback", {"phone": CALLER, "reason": reason, "summary": summary}
    )


@pytest.mark.parametrize(
    "summary",
    [
        "Gast hat Erdnussallergie, bitte zurückrufen",
        "Frage zur Bestellung, verträgt keine Milch",
        "Rückruf wegen Laktoseintoleranz des Kindes",
        # Other words for it, which no kitchen note is read from (review).
        "Gast hat Zöliakie, bitte zurückrufen",
        "Sohn reagiert auf Nüsse",
        "Gast muss Erdnüsse strikt meiden",
    ],
)
def test_a_model_callback_summary_never_quotes_an_allergy(session, state, summary):
    """Repo audit of 27.10.2026, finding 2: the model's own `summary` is
    health data like a note (L13). The card is still made, with the sentence
    of its reason, as the core's own cards: a refusal the model does not
    retry would lose the guest."""
    turn(
        session,
        state,
        "Ich habe eine Erdnussallergie, rufen Sie mich bitte zurück",
        LLMTurn(tool_call=callback(summary)),
        LLMTurn(say="Das Team ruft Sie zurück."),
    )

    session.expire_all()
    [card] = session.scalars(select(Callback))
    assert card.summary == "Anruf konnte nicht automatisch abgeschlossen werden."


def test_an_ingredient_named_as_an_allergy_stays_off_the_card(session, state):
    """The ingredient the guest named as an allergy in this call is the
    allergy, whatever words the model puts around it."""
    state.allergies = [Allergy(ingredient="Sesam")]

    turn(
        session,
        state,
        "Rufen Sie mich bitte zurück",
        LLMTurn(tool_call=callback("Bitte zu Sesam in der Sauce beraten")),
        LLMTurn(say="Das Team ruft Sie zurück."),
    )

    session.expire_all()
    [card] = session.scalars(select(Callback))
    assert "Sesam" not in card.summary


def test_a_rewritten_card_keeps_its_reason_and_the_booking(session, state):
    """The card of a complaint says so, and a booking of this call stands on
    it, as on the core's own cards, or the team books a second time."""
    state.booking_say = "Abholung um 18:30 Uhr, Abholcode 42."

    turn(
        session,
        state,
        "Ich habe eine Erdnussallergie, rufen Sie mich bitte zurück",
        LLMTurn(
            tool_call=callback("Beschwerde, Gast hat Erdnussallergie", "complaint")
        ),
        LLMTurn(say="Das Team ruft Sie zurück."),
    )

    session.expire_all()
    [card] = session.scalars(select(Callback))
    assert card.summary == (
        "Kunde hat sich beschwert. Bereits gebucht: Abholung um 18:30 Uhr, "
        "Abholcode 42."
    )


def test_the_guests_own_words_for_coeliac_disease_stay_off_the_card(session, state):
    state.slots["phone"] = CALLER

    turn(session, state, "Ich will einen Menschen sprechen, ich habe Zöliakie")

    callback_row = session.scalars(select(Callback)).one()
    assert "Zöliakie" not in callback_row.summary


def test_a_model_callback_summary_without_an_allergy_stays(session, state):
    clean = "Gast möchte liefern lassen, ohne Zwiebeln"

    turn(
        session,
        state,
        "Können Sie liefern?",
        LLMTurn(tool_call=callback(clean)),
        LLMTurn(say="Das Team ruft Sie zurück."),
    )

    session.expire_all()
    assert [card.summary for card in session.scalars(select(Callback))] == [clean]


def test_a_human_question_while_the_question_waits_is_answered_first(session, state):
    """Merge of T-11.5 into T-11.1: "Sind Sie ein Mensch?" gets the truthful
    answer and the offer of the team (docs/09 L1) also while a draft waits for
    the allergy question. The draft stays held; after a no to the offer the
    question is put again, and only its own answer is consent."""
    turn(
        session,
        state,
        f"{TABLE}, ich habe eine Erdnussallergie",
        LLMTurn(tool_call=table()),
    )
    guards.note_said(state, "Sind Sie noch da?")

    assert turn(session, state, "Sind Sie ein Mensch?") == [SAY_HUMAN_ANSWER]
    assert state.held_call is not None
    assert logged(session) == []

    assert turn(session, state, "Nein.") == [ALLERGY_QUESTION]
    assert logged(session) == []

    turn(session, state, "Ja.")
    [reservation] = reservations(session)
    assert reservation.note == REASONED
    assert logged(session) == [(ALLERGY_QUESTION, "yes")]


def test_a_request_next_to_the_human_question_hears_the_truth_first(session, state):
    """The truthful sentence first, then the answer to the request: docs/09
    L1 holds in every turn. A request is more than a yes, so the model reads
    it and the held draft is dropped (Codex PR #368)."""
    turn(
        session,
        state,
        f"{TABLE}, ich habe eine Erdnussallergie",
        LLMTurn(tool_call=table()),
    )
    guards.note_said(state, "Sind Sie noch da?")

    said = turn(
        session,
        state,
        "Spreche ich mit einem Menschen? Ich will bestellen.",
        LLMTurn(say="Gern, was darf es sein?"),
    )

    assert said == [SAY_NOT_A_PERSON, "Gern, was darf es sein?"]
    assert state.held_call is None
    assert [(a.ingredient, a.own) for a in state.allergies] == [("Erdnuss", True)]


def test_a_held_replacement_takes_back_the_draft_read_out_before(session, state):
    """Codex PR #368, P1 4378680687: draft A waits for its yes, the guest adds
    an allergy, and the model's new draft waits for the consent question. A
    is no longer what the guest wants: it is dropped at once, so after an
    unclear answer no yes can confirm it without the restriction."""
    turn(session, state, TABLE, LLMTurn(tool_call=table()), LLMTurn(say="Passt das?"))
    [first] = reservations(session)
    assert state.stage == "readback_pending" and state.reservation_id == first.id

    said = turn(
        session,
        state,
        "Ich habe übrigens eine Erdnussallergie.",
        LLMTurn(tool_call=table()),
    )

    assert said == [ALLERGY_QUESTION]
    assert state.stage == "collecting"
    assert state.reservation_id is None
    assert state.readback is None

    turn(session, state, "Wie bitte?", LLMTurn(say="Passt das so?"))
    said = turn(
        session,
        state,
        "Ja.",
        LLMTurn(
            tool_call=ToolCall(
                "confirm", {"entity": "reservation", "entity_id": str(first.id)}
            )
        ),
        LLMTurn(say="Gern."),
    )
    assert [r.status for r in reservations(session)] == ["draft"]


def test_an_answer_to_wogegen_with_a_correction_goes_to_the_model(session, state):
    """Codex PR #368, P1 4379357300: "Erdnüsse, aber bitte erst übermorgen"
    answers the question and changes the date. The held draft would go out
    with the old date: the model reads the sentence instead. Not read in full
    (Codex PR #368, P1 4379762029), the answer leaves the allergy open, and
    the model's new draft, with the new date, waits for "Wogegen?" again: one
    question more, never an allergen guessed or lost."""
    turn(session, state, f"{TABLE}, ich habe eine Allergie", LLMTurn(tool_call=table()))
    later = {**table().args, "reserved_for": "2026-09-17T19:00:00+02:00"}

    said = turn(
        session,
        state,
        "Erdnüsse, aber bitte erst übermorgen.",
        LLMTurn(tool_call=ToolCall("create_reservation", later)),
    )

    assert said == [SAY_ALLERGY_WHICH]
    assert reservations(session) == []
    assert state.held_call is not None
    assert state.held_call["args"]["reserved_for"] == later["reserved_for"]

    assert turn(session, state, "Erdnüsse.") == [ALLERGY_QUESTION]
    turn(session, state, "Ja.")
    [reservation] = reservations(session)
    assert reservation.note == "WICHTIG: Keine Erdnüsse. Grund: Allergie"
    assert reservation.reserved_for.isoformat().startswith("2026-09-17")


def test_the_same_allergy_said_for_two_dishes_goes_onto_both(session, state):
    """Codex PR #368, P1 4379357317: read once from the sentence, the allergy
    was tied to the first dish only, and the child's dish lost it."""
    pho, ente = str(uuid.uuid4()), str(uuid.uuid4())
    allergies.note_allergies(
        session,
        state,
        "Die 13 bitte, ich habe eine Erdnussallergie und die 23 bitte, "
        "mein Sohn hat eine Erdnussallergie",
    )
    wish = {"kind": "allergy", "ingredient": "Erdnuss", "text": REASONED}
    found = {
        "match_type": "positions",
        "positions": [
            {
                "match_type": "exact_number",
                "results": [{"menu_item_id": item, "number": number, "name": name}],
                "wish": wish,
            }
            for item, number, name in ((pho, "13", "Pho Bo"), (ente, "23", "Ente"))
        ],
    }
    apply_tool_result(state, "search_menu", ToolResult(ok=True, data=found))
    # Searched again, the dish gets no second copy.
    apply_tool_result(state, "search_menu", ToolResult(ok=True, data=found))
    state.allergy_consent = "yes"

    call = ToolCall(
        "draft_order",
        {"items": [{"menu_item_id": item, "quantity": 1} for item in (pho, ente)]},
    )
    noted = allergies.noted_call(state, call).args

    assert [item.get("note") for item in noted["items"]] == [REASONED, REASONED]
    assert "note" not in noted
    assert len(state.allergies) == 2


def test_two_open_allergies_are_asked_for_with_their_dish():
    state = ConversationState(call_id=uuid.uuid4(), tenant_id=uuid.uuid4())
    pho, ente = str(uuid.uuid4()), str(uuid.uuid4())
    state.seen_items = {pho: {"name": "Pho Bo"}, ente: {"name": "Ente"}}
    state.allergies = [Allergy(item=pho), Allergy(item=ente)]

    assert allergies.question(state) == "Wogegen sind Sie bei Pho Bo allergisch?"
    state.allergies[0].ingredient = "Erdnuss"
    assert allergies.question(state) == SAY_ALLERGY_WHICH


def test_an_allergy_said_after_the_read_back_takes_the_draft_back(session, state):
    """Codex PR #368, P1 4379960053: draft A was read out, then the guest
    names an allergy. A does not carry it: it is dropped at once, so a model
    that only repeats A's read-back cannot have it confirmed."""
    turn(session, state, TABLE, LLMTurn(tool_call=table()), LLMTurn(say="Passt das?"))
    [first] = reservations(session)
    read_back = state.readback
    guards.note_said(state, read_back)

    turn(
        session,
        state,
        "Ich habe übrigens eine Erdnussallergie.",
        LLMTurn(say=read_back),
    )

    assert state.stage == "collecting"
    assert state.reservation_id is None
    turn(
        session,
        state,
        "Ja.",
        LLMTurn(
            tool_call=ToolCall(
                "confirm", {"entity": "reservation", "entity_id": str(first.id)}
            )
        ),
        LLMTurn(say="Einen Moment."),
    )
    assert [r.status for r in reservations(session)] == ["draft"]


def test_an_allergy_said_again_keeps_the_draft_that_carries_it(session, state):
    turn(
        session,
        state,
        f"{TABLE}, ich habe eine Erdnussallergie",
        LLMTurn(tool_call=table()),
    )
    turn(session, state, "Ja.")
    assert state.stage == "readback_pending"

    turn(
        session,
        state,
        "Wie gesagt, ich habe eine Erdnussallergie.",
        LLMTurn(say=state.readback),
    )

    assert state.stage == "readback_pending"
    assert len(state.allergies) == 1


def test_the_same_answer_for_two_dishes_keeps_both(session, state):
    """Codex PR #368, P1 4380083176: two dishes with an allergy whose
    ingredient is asked for, "Erdnuss" to each question: the second answer
    was taken for the first and its dish lost the restriction."""
    pho, ente = str(uuid.uuid4()), str(uuid.uuid4())
    state.seen_items = {pho: {"name": "Pho Bo"}, ente: {"name": "Ente"}}
    state.allergies = [Allergy(item=pho), Allergy(item=ente)]

    guards.note_said(state, allergies.question(state) or "")
    read(session, state, "Erdnuss.")
    guards.note_said(state, allergies.question(state) or "")
    read(session, state, "Erdnuss.")

    assert allergies.question(state) == ALLERGY_QUESTION
    state.allergy_consent = "no"
    call = ToolCall(
        "draft_order",
        {"items": [{"menu_item_id": item, "quantity": 1} for item in (pho, ente)]},
    )
    noted = allergies.noted_call(state, call).args
    assert [item.get("note") for item in noted["items"]] == [
        "ohne Erdnuss",
        "ohne Erdnuss",
    ]


@pytest.mark.parametrize("order", [("13", "23"), ("23", "13")])
def test_the_search_order_never_decides_whose_allergy_a_dish_gets(
    session, state, order
):
    """Codex PR #368, P1 4380178537: the caller's and the wife's peanut
    allergy, each said with a dish. Tied by the order of the searches, the
    wife's dish could get the caller's reason. Neither is tied to a dish:
    both go onto the whole order, the wife's without the reason."""
    items = {"13": str(uuid.uuid4()), "23": str(uuid.uuid4())}
    allergies.note_allergies(
        session,
        state,
        "Die 13 bitte, ich habe eine Erdnussallergie. "
        "Die 23 bitte, meine Frau hat eine Erdnussallergie.",
    )
    wish = {"kind": "allergy", "ingredient": "Erdnuss", "text": REASONED}
    for number in order:
        found = {
            "match_type": "exact_number",
            "results": [{"menu_item_id": items[number], "number": number}],
            "wish": wish,
        }
        apply_tool_result(state, "search_menu", ToolResult(ok=True, data=found))
    state.allergy_consent = "yes"

    call = ToolCall(
        "draft_order",
        {"items": [{"menu_item_id": item, "quantity": 1} for item in items.values()]},
    )
    noted = allergies.noted_call(state, call).args

    assert [item.get("note") for item in noted["items"]] == [None, None]
    assert noted["note"] == f"{REASONED}. Ohne Erdnuss"


@pytest.mark.parametrize(
    ("said", "bare"),
    [
        ("Ja.", True),
        ("Ja, ich bin noch da.", True),
        ("Nein.", True),
        # Codex PR #368, P1 4382730105: digits are always more than a yes.
        ("Ja, bitte 20:00.", False),
        ("Ja, 4 Personen.", False),
        ("Ja, aber bitte erst um acht.", False),
        ("Ja, und noch eine Cola.", False),
        ("", False),
    ],
)
def test_a_bare_answer_is_a_plain_yes_or_no(said, bare):
    from api.agent.consent import bare_answer

    assert bare_answer(said) is bare


def test_a_time_in_the_answer_to_wogegen_goes_to_the_model(session, state):
    """Codex PR #368, P1 4382914460: "Erdnüsse, um 20:00." resumed the held
    draft with 19:00. The model reads it, its new draft waits for "Wogegen?"
    again, and the answer then goes out with the new time."""
    turn(session, state, f"{TABLE}, ich habe eine Allergie", LLMTurn(tool_call=table()))
    later = {**table().args, "reserved_for": "2026-09-16T20:00:00+02:00"}

    said = turn(
        session,
        state,
        "Erdnüsse, um 20:00.",
        LLMTurn(tool_call=ToolCall("create_reservation", later)),
    )

    assert said == [SAY_ALLERGY_WHICH]
    assert state.held_call is not None
    assert state.held_call["args"]["reserved_for"] == later["reserved_for"]
    assert turn(session, state, "Erdnüsse.") == [ALLERGY_QUESTION]
    turn(session, state, "Ja.")
    [reservation] = reservations(session)
    assert reservation.reserved_for.isoformat().startswith("2026-09-16T18:00")
    assert reservation.note == "WICHTIG: Keine Erdnüsse. Grund: Allergie"


def test_wogegen_unanswered_twice_goes_to_the_team(session, state):
    """#207: "Weiß ich nicht." held every new draft behind the same question
    until the call ran out of time. After two unanswered "Wogegen?" the team
    takes the call; the card never names the allergy (L13)."""
    said = turn(
        session, state, f"{TABLE}, ich habe eine Allergie", LLMTurn(tool_call=table())
    )
    assert said == [SAY_ALLERGY_WHICH]
    assert turn(session, state, "Weiß ich nicht.", LLMTurn(tool_call=table())) == [
        SAY_ALLERGY_WHICH
    ]

    said = turn(session, state, "Keine Ahnung.", LLMTurn(tool_call=table()))

    assert said == [SAY_TABLE_NOT_BOOKED, SAY_CALLBACK_NUMBER]
    assert state.callback_waits == (
        f"{SUMMARY_KITCHEN_NOTE} {SUMMARY_OPEN_TABLE} 4 Personen, "
        "16.09.2026 19:00 Uhr, Name Müller."
    )
    assert state.held_call is None
    assert reservations(session) == []


def test_an_answer_that_fills_an_allergy_starts_the_count_again(session, state):
    """Review of #207: the count ran over the whole call, so a guest who
    answered every question was handed over at the third."""
    said = turn(
        session,
        state,
        f"{TABLE}. Ich habe eine Allergie. Meine Frau hat auch eine Allergie.",
        LLMTurn(tool_call=table()),
    )
    assert allergies.asked_which(said[-1])
    for answer in ("Erdnuss.", "Sesam."):
        said = turn(session, state, answer, LLMTurn(tool_call=table()))
        assert state.which_asks == 0
    assert state.callback_waits is None
    assert state.held_call is not None


def test_a_question_put_in_between_costs_no_answer(session, state):
    """Review of #207: "Sind Sie noch da?" from the phone line, answered
    with a bare yes, puts "Wogegen?" again and is no answer to it."""
    turn(session, state, f"{TABLE}, ich habe eine Allergie", LLMTurn(tool_call=table()))
    for _ in range(3):
        guards.note_said(state, "Sind Sie noch da?")
        assert turn(session, state, "Ja.") == [SAY_ALLERGY_WHICH]
    assert state.which_asks == 0


def test_an_answered_wogegen_starts_the_count_again(session, state):
    turn(session, state, f"{TABLE}, ich habe eine Allergie", LLMTurn(tool_call=table()))
    turn(session, state, "Weiß ich nicht.", LLMTurn(tool_call=table()))

    assert turn(session, state, "Erdnüsse.") == [ALLERGY_QUESTION]

    assert state.which_asks == 0


INGREDIENTS = ["Erdnuss", "Sesam", "Senf", "Sellerie", "Lupine", "Haselnuss"]


def test_many_allergies_share_one_notice(state):
    """#207: six allergies said one by one gave a note of 235 characters, the
    draft failed validation and no order could be made."""
    state.allergies = [Allergy(ingredient=i, own=True) for i in INGREDIENTS]
    state.allergy_consent = "yes"
    call = allergies.noted_call(
        state, ToolCall("draft_order", {"items": [], "note": "Bitte klingeln"})
    )

    assert call.args["note"] == (
        "WICHTIG: Keine Erdnuss, Sesam, Senf, Sellerie, Lupine und Haselnuss. "
        "Grund: Allergie. Bitte klingeln"
    )
    assert allergies.fits(call, call)


def test_many_allergies_without_consent_share_one_ohne(state):
    many = [*INGREDIENTS, "Walnuss", "Cashew", "Pistazie", "Mandel", "Paranuss"]
    state.allergies = [Allergy(ingredient=f"{i}kerne", own=True) for i in many]
    state.allergy_consent = "no"
    call = allergies.noted_call(state, ToolCall("draft_order", {"items": []}))

    note = call.args["note"]
    assert note.startswith("ohne Erdnusskerne, Sesamkerne, ")
    assert note.endswith(" und Paranusskerne")
    assert note.count("ohne") == 1
    assert allergies.fits(call, call)


def test_a_note_that_never_fits_goes_to_the_team(session, state):
    """Too many allergies even for one notice: the team takes the call before
    anything is stored, never a note with an allergy cut off (#207)."""
    state.allergies = [
        Allergy(ingredient=f"Zutat mit langem Namen {n:02}", own=True)
        for n in range(25)
    ]
    state.allergy_consent = "yes"

    said = turn(session, state, TABLE, LLMTurn(tool_call=table()))

    assert said == [SAY_TABLE_NOT_BOOKED, SAY_CALLBACK_NUMBER]
    assert state.callback_waits is not None
    assert state.callback_waits.startswith(SUMMARY_KITCHEN_NOTE)
    assert reservations(session) == []


@pytest.mark.parametrize("between", ["Sind Sie ein Roboter?", "Wie bitte?"])
def test_a_question_of_the_guest_costs_no_answer(session, state, between):
    """Codex PR #414: a question of the guest's own after "Wogegen?" counted
    as an answer that left the allergy open."""
    turn(session, state, f"{TABLE}, ich habe eine Allergie", LLMTurn(tool_call=table()))
    turn(session, state, "Weiß ich nicht.", LLMTurn(tool_call=table()))

    turn(session, state, between, LLMTurn(say="Wogegen sind Sie allergisch?"))

    assert state.which_asks == 1


def test_a_corrected_draft_goes_onto_the_card(session, state):
    """Codex PR #414: a correction the model sent without a `cart` reached the
    card with the quantity of the older cart."""
    loop = ConversationLoop(session, FakeLLM([]), "system", now=NOW)
    dish = str(uuid.uuid4())
    state.seen_items[dish] = {"number": "13", "name": "Pho Bo"}
    state.cart = [{"menu_item_id": dish, "quantity": 1}]
    draft = ToolCall("draft_order", {"items": [{"menu_item_id": dish, "quantity": 2}]})

    loop._note_to_team(state, draft)

    assert state.cart == [{"menu_item_id": dish, "quantity": 2}]
    assert state.callback_waits is not None
    assert "2x 13 Pho Bo" in state.callback_waits


def test_a_long_note_of_the_model_is_left_to_the_validation(state):
    """Review of #207: only notes the core wrote decide a handover. A note
    the model wrote too long on a line without an allergy stays its own."""
    state.allergies = [Allergy(ingredient="Erdnuss", own=True, item="a")]
    state.allergy_consent = "yes"
    written = ToolCall(
        "draft_order",
        {
            "items": [
                {"menu_item_id": "a", "quantity": 1},
                {"menu_item_id": "b", "quantity": 1, "note": "x" * 250},
            ]
        },
    )

    assert allergies.fits(allergies.noted_call(state, written), written)


def test_the_model_s_part_stays_next_to_a_shared_notice(state):
    state.allergies = [Allergy(ingredient=i, own=True) for i in INGREDIENTS]
    state.allergy_consent = "yes"
    call = allergies.noted_call(
        state, ToolCall("draft_order", {"items": [], "note": "Bitte klingeln"})
    )

    assert call.args["note"].endswith(". Bitte klingeln")


def test_an_order_not_yet_in_the_cart_goes_onto_the_card(session, state):
    """Review of #207: a draft the model sent without a cart was neither on
    the card nor named to the guest when its allergy went to the team."""
    loop = ConversationLoop(session, FakeLLM([]), "system", now=NOW)
    draft = ToolCall(
        "draft_order",
        {"items": [{"menu_item_id": str(uuid.uuid4()), "quantity": 2}]},
    )

    result = loop._note_to_team(state, draft)

    assert result.say == [SAY_ORDER_NOT_PLACED, SAY_CALLBACK_NUMBER]
    assert state.callback_waits is not None
    assert f"{SUMMARY_OPEN_ORDER} " in state.callback_waits
