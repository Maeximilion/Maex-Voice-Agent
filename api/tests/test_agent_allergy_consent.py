"""The caller's consent to the allergy as a kitchen note, held by the core (T-11.1).

Legal check L13 (Maxi, 20.10.2026): the agent asks "Darf ich die Allergie als
Hinweis für die Küche notieren?"; only an explicit "ja" stores "WICHTIG: Keine
<Zutat>. Grund: Allergie", on "nein" only "ohne <Zutat>"; the consent is
logged per call. The core asks before the first draft whose note would carry
the reason, holds that draft until the answer, and carries it out with its
notes as the answer allows. Caller sentences in German: the yes detector and
the parser read German.
"""

import uuid
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from api.agent import guards
from api.agent.consent import ALLERGY_QUESTION, consent_answer
from api.agent.llm import FakeLLM, LLMTurn, ToolCall
from api.agent.loop import ConversationLoop
from api.agent.state import ConversationState
from api.models import Call, Callback, ConsentLog, Reservation
from scripts.seed import seed

BERLIN = ZoneInfo("Europe/Berlin")
NOW = datetime(2026, 9, 15, 8, 0, tzinfo=BERLIN)  # before opening: no team
CALLER = "+4972215551234"
REASONED = "WICHTIG: Keine Erdnuss. Grund: Allergie"


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


def table(note: str | None) -> ToolCall:
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


def test_the_first_note_with_the_reason_waits_for_the_question(session, state):
    said = turn(
        session,
        state,
        "Tisch für vier auf Müller, ich habe eine Erdnussallergie",
        LLMTurn(tool_call=table(REASONED)),
    )

    assert said == [ALLERGY_QUESTION]
    assert reservations(session) == []
    assert state.held_call is not None
    assert state.last_say == ALLERGY_QUESTION
    assert logged(session) == []


def test_after_a_yes_the_draft_keeps_the_reason_and_is_read_out(session, state):
    turn(
        session,
        state,
        "Tisch für vier auf Müller, Erdnussallergie",
        LLMTurn(tool_call=table(REASONED)),
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
        "Tisch für vier auf Müller, Erdnussallergie",
        LLMTurn(tool_call=table(REASONED)),
    )

    said = turn(session, state, "Nein, danke.")

    [reservation] = reservations(session)
    assert reservation.note == "ohne Erdnuss"
    assert "Allergie" not in said[0]
    assert logged(session) == [(ALLERGY_QUESTION, "no")]


def test_an_unclear_answer_drops_the_draft_and_the_model_reads_it(session, state):
    """Unclear is no consent, and the sentence was likely about something
    else: the model gets it, and the draft it makes again has no reason."""
    turn(
        session,
        state,
        "Tisch für vier auf Müller, Erdnussallergie",
        LLMTurn(tool_call=table(REASONED)),
    )

    said = turn(
        session,
        state,
        "Moment, wir sind doch fünf.",
        LLMTurn(tool_call=table(REASONED)),
        LLMTurn(say="Gern."),
    )

    [reservation] = reservations(session)
    assert reservation.note == "ohne Erdnuss"
    assert said == ["Gern."]
    assert logged(session) == [(ALLERGY_QUESTION, "unclear")]


@pytest.mark.parametrize("limit", ["turn", "call"])
def test_a_held_draft_that_ends_after_a_limit_goes_to_the_team(session, state, limit):
    """Codex PR #368, P1: the draft carried out after the answer takes longer
    than the guest may wait. As after any slow tool, the call goes to the team
    instead of its read-back; the draft stays a draft."""
    turn(
        session,
        state,
        "Tisch für vier auf Müller, Erdnussallergie",
        LLMTurn(tool_call=table(REASONED)),
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


def test_an_answer_that_goes_to_the_team_is_logged_and_stores_nothing(session, state):
    """The question was asked, so its answer is logged, also when the sentence
    hands the call over; the draft that waited is never carried out."""
    state.slots["phone"] = CALLER
    turn(
        session,
        state,
        "Tisch für vier auf Müller, Erdnussallergie",
        LLMTurn(tool_call=table(REASONED)),
    )

    turn(session, state, "Nein, ich will mit einem Menschen sprechen.")

    assert reservations(session) == []
    assert logged(session) == [(ALLERGY_QUESTION, "unclear")]
    assert session.scalars(select(Callback)).one().reason == "human_requested"


def test_the_question_is_asked_once_per_call(session, state):
    turn(
        session,
        state,
        "Tisch auf Müller, Erdnussallergie",
        LLMTurn(tool_call=table(REASONED)),
    )
    turn(session, state, "Ja.")

    sesame = "WICHTIG: Keine Sesam. Grund: Allergie"
    said = turn(
        session,
        state,
        "Und gegen Sesam bin ich auch allergisch.",
        LLMTurn(tool_call=table(f"{REASONED}, {sesame}")),
        LLMTurn(say="Notiert."),
    )

    assert said == ["Notiert."]
    assert len(logged(session)) == 1
    # Codex PR #368: both allergens stay on the note.
    assert reservations(session)[-1].note == f"{REASONED}. {sesame}"


def test_an_answer_to_another_question_is_no_consent(session, state):
    """Codex PR #368, P1: the phone line asks "Sind Sie noch da?" after a
    silence. A "Ja" to that is no consent: the question is asked again."""
    turn(
        session,
        state,
        "Tisch für vier auf Müller, Erdnussallergie",
        LLMTurn(tool_call=table(REASONED)),
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


def test_a_note_in_the_guest_s_words_takes_the_fixed_wording(session, state):
    turn(
        session,
        state,
        "Tisch für vier auf Müller",
        LLMTurn(tool_call=table("Erdnussallergie")),
    )
    turn(session, state, "Ja.")

    [reservation] = reservations(session)
    assert reservation.note == REASONED


def test_another_adult_s_allergy_is_never_asked_about(session, state):
    said = turn(
        session,
        state,
        "Tisch für vier auf Müller, meine Frau hat eine Erdnussallergie",
        LLMTurn(tool_call=table("meine Frau hat eine Erdnussallergie")),
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
        "Tisch für vier auf Müller mit Hochstuhl",
        LLMTurn(tool_call=table("mit Hochstuhl")),
        LLMTurn(say="Passt das so?"),
    )

    [reservation] = reservations(session)
    assert reservation.note == "mit Hochstuhl"
    assert logged(session) == []


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


def test_an_order_draft_carries_its_notes_as_the_answer_allows():
    """Line notes and the note of the whole order alike."""
    state = ConversationState(call_id=uuid.uuid4(), tenant_id=uuid.uuid4())
    call = ToolCall(
        "draft_order",
        {
            "items": [
                {"menu_item_id": "a", "quantity": 1, "note": REASONED},
                {"menu_item_id": "b", "quantity": 1, "note": "ohne Koriander"},
            ],
            "note": "Sesamallergie",
        },
    )
    assert guards.waits_for_consent(state, call)

    state.allergy_consent = "no"
    stored = guards.consented_call(state, call).args
    assert not guards.waits_for_consent(state, call)
    assert [item.get("note") for item in stored["items"]] == [
        "ohne Erdnuss",
        "ohne Koriander",
    ]
    assert stored["note"] == "ohne Sesam"

    state.allergy_consent = "yes"
    stored = guards.consented_call(state, call).args
    assert stored["items"][0]["note"] == REASONED
    assert stored["note"] == "WICHTIG: Keine Sesam. Grund: Allergie"
