"""Guest remarks are plain notes the model writes (Maxi, 15.11.2026, #736, #763).

A remark on a dish goes on its line, one for the whole order on the order, and
an allergy is written by the model as well ("WICHTIG !!! Kunde ist allergisch
auf Nüsse"). The core asks no consent question and reads no allergy list; it
keeps one safety net: a guest who spoke of their health gets no draft without
a note that names it; the first goes back to the model once per turn with the
guest's words (`guards.unnoted_allergy`), the next gets them from the core
(`guards.with_the_guests_words`). Allergen answers stay with the database (L17).
Caller sentences in German: the core parses German.
"""

from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from api.agent import guards
from api.agent.escalation import arms_the_net
from api.agent.guards import SAY_ALLERGY_NOTED, SAY_ASK_ALLERGY
from api.agent.llm import FakeLLM, LLMError, LLMTurn, ToolCall
from api.agent.loop import (
    SAY_ALLERGENS_BEYOND,
    SUMMARY_NOTE_TO_CLEAR,
    ConversationLoop,
    _taken_back,
)
from api.agent.state import ConversationState, note_guest_details
from api.domain.menu import search_menu
from api.domain.menu.importer import apply, parse
from api.models import Call, Callback, Order, OrderItem, Reservation
from api.tests.test_agent_allergens_beyond import CALLER, CLOSED, run, state_for
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
    (Maxi as data protection officer, L13). The turn names an allergy, so the
    guest hears a core sentence only (#798)."""
    state = state_for(session, known_tenant, phone=CALLER)

    result, _ = run(
        session,
        state,
        "Einmal die Pho Bo auf Müller, ich habe eine Nussallergie.",
        search(),
        draft(session, known_tenant, line_note=line_note, order_note=order_note),
        LLMTurn(say=SAY_ALLERGY_NOTED),
    )

    assert result.say == [SAY_ALLERGY_NOTED]
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
        LLMTurn(say=SAY_ALLERGY_NOTED),
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
        LLMTurn(say=SAY_ALLERGY_NOTED),
    )

    assert result.say == [SAY_ALLERGY_NOTED]
    assert drafts_logged(session, state) == ["invalid_input", None]
    # The core wrote the guest's own words on the order (Maxi, 15.11.2026).
    assert stored(session) == (
        "WICHTIG !!! Gast sagte: "
        "Einmal die Pho Bo auf Müller, ich habe eine Nussallergie",
        [None],
    )
    assert state.order_note == stored(session)[0]


def test_an_allergy_said_in_an_earlier_turn_still_arms_it(session, known_tenant):
    state = state_for(session, known_tenant, phone=CALLER)
    run(session, state, "Ich habe eine Nussallergie.", LLMTurn(say=SAY_ALLERGY_NOTED))

    run(
        session,
        state,
        "Einmal die Pho Bo auf Müller.",
        search(),
        draft(session, known_tenant),
        draft(session, known_tenant, line_note=ALLERGY_NOTE),
        LLMTurn(say=SAY_ALLERGY_NOTED),
    )

    assert drafts_logged(session, state) == ["invalid_input", None]


@pytest.mark.parametrize(
    "said",
    [
        "Ich habe keine Allergie.",
        "Ich bin nicht allergisch.",
        "Einmal die Pho Bo auf Müller.",
    ],
)
def test_a_denial_or_no_allergy_does_not_arm_it(session, known_tenant, said):
    """A denial with more around it ("Die 13, ich habe keine Allergie") arms
    the net: generous on purpose, the guest's words go on the note."""
    state = state_for(session, known_tenant, phone=CALLER)
    note_guest_details(state, "Auf den Namen Müller.")

    run(
        session,
        state,
        said,
        search(),
        draft(session, known_tenant),
        LLMTurn(say="Passt das so?"),
    )

    assert state.allergy_said == []
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
        LLMTurn(say=SAY_ALLERGY_NOTED),
    )

    assert session.scalars(select(Reservation.note)).all() == [ALLERGY_NOTE]


@pytest.mark.parametrize(
    ("said", "note"),
    [
        ("Ich habe eine Nussallergie.", ALLERGY_NOTE),
        ("Meine Frau hat eine Sesamallergie.", "WICHTIG !!! Allergie (Frau): Sesam"),
        ("Ich habe eine Laktoseunverträglichkeit.", "Laktoseintoleranz"),
        ("Ich vertrage keine Milch.", "verträgt keine Milch, Unverträglichkeit"),
        # No allergen letter to compare: any health note will do.
        ("Ich habe eine Kiwiallergie.", "WICHTIG !!! Allergie: Kiwi"),
    ],
)
def test_a_note_that_names_the_allergy_passes_the_net(said, note):
    state = _named()
    state.allergy_said = [said]
    call = ToolCall("draft_order", {"items": [{"menu_item_id": "x", "note": note}]})
    assert guards.unnoted_allergy(state, call) is None


@pytest.mark.parametrize(
    "note",
    [
        None,
        "ohne Nüsse",
        "WICHTIG: ohne Nüsse",
        # Only the example of the hint repeated names nothing.
        "WICHTIG !!! Kunde ist allergisch auf …",
        "WICHTIG !!! Kunde ist allergisch auf <X>",
    ],
)
def test_a_note_that_does_not_name_it_is_caught(note):
    """The words are the retention job's: a note that passes is deleted in
    time (L13). The hint quotes the guest: the model keeps nothing."""
    state = _named()
    args = {"items": [{"menu_item_id": "x", "quantity": 1}]}
    if note is not None:
        args["note"] = note
    refused = guards.unnoted_allergy(state, ToolCall("draft_order", args))
    assert refused is not None and refused.error_code == "invalid_input"
    assert (refused.hint or "").startswith(guards.HINT_UNNOTED_ALLERGY)
    assert "„Ich habe eine Nussallergie“" in (refused.hint or "")
    assert guards.unnoted_allergy(state, ToolCall("search_menu", {})) is None


@pytest.mark.parametrize(
    "said",
    [
        "Ich habe eine Nussallergie.",
        "Meine Frau ist allergisch gegen Sesam.",
        "Ich habe eine Laktoseunverträglichkeit.",
        "Ich habe Zöliakie.",
        "Ich darf keine Nüsse essen.",
        "Ich habe keine Allergie. Mein Mann aber gegen Sesam.",
        "Keine Allergie, mein Mann gegen Sesam",
        "Ich bin nicht allergisch, meine Tochter schon, gegen Nüsse.",
    ],
)
def test_a_health_word_arms_the_net(said):
    """Generous on purpose (Maxi, 15.11.2026): a false arm costs one hop."""
    assert arms_the_net(said)


@pytest.mark.parametrize(
    "said",
    [
        "Ich habe keine Allergie.",
        "Nein, keine Allergie.",
        "Welche Allergene hat die 13?",
        "Die 13 bitte.",
    ],
)
def test_a_bare_denial_or_no_health_word_does_not_arm_it(said):
    assert not arms_the_net(said)


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
    return ConversationState(
        call_id=uuid4(), tenant_id=uuid4(), allergy_said=["Ich habe eine Nussallergie."]
    )


def test_the_answer_to_the_models_allergy_question_is_kept(session, known_tenant):
    """The model asks "Wogegen?" itself; "Gegen Erdnüsse." names no allergy
    word, and the model of the next turn must still see it."""
    state = state_for(session, known_tenant, phone=CALLER)
    run(
        session,
        state,
        "Ich habe eine Allergie.",
        LLMTurn(say=SAY_ASK_ALLERGY),
    )

    _, llm = run(session, state, "Gegen Erdnüsse.", LLMTurn(say=SAY_ALLERGY_NOTED))

    assert state.allergy_said == ["Ich habe eine Allergie.", "Gegen Erdnüsse."]
    assert llm.calls[0][1]["allergy_said"] == state.allergy_said


@pytest.mark.parametrize("answer", ["Keine.", "Nein.", "Ich habe keine Allergie."])
def test_a_bare_denial_to_the_question_takes_the_own_back(
    session, known_tenant, answer
):
    """`allergie_0012`: "Wogegen?" - "Keine.": nothing is left to note."""
    state = state_for(session, known_tenant, phone=CALLER)
    run(
        session,
        state,
        "Ich habe eine Allergie.",
        LLMTurn(say=SAY_ASK_ALLERGY),
    )

    run(session, state, answer, LLMTurn(say=SAY_ALLERGY_NOTED))

    assert state.allergy_said == []


@pytest.mark.parametrize(
    ("named", "asked", "answer"),
    [
        ("Ich habe eine Erdnussallergie.", "Haben Sie sonst noch Allergien?", "Nein."),
        ("Ich habe eine Kiwiallergie.", "Haben Sie sonst noch Allergien?", "Nein."),
        ("Ich habe Zöliakie.", SAY_ASK_ALLERGY, "Danke, nichts."),
    ],
)
def test_a_bare_no_keeps_an_allergy_the_guest_named(
    session, known_tenant, named, asked, answer
):
    """A "no" to a question about a further allergy takes back nothing the
    guest named: only a sentence without its allergen ("Ich habe eine
    Allergie.") is cleared (ledger #764 batch review, P1)."""
    state = state_for(session, known_tenant, phone=CALLER)
    run(session, state, named, LLMTurn(say=SAY_ALLERGY_NOTED))
    run(session, state, "Ich möchte etwas abholen.", LLMTurn(say=asked))

    run(session, state, answer, LLMTurn(say="Was darf ich Ihnen bringen?"))

    assert state.allergy_said == [named]


NUTS = "Ich habe eine Nussallergie."
SESAME = "Ich habe eine Sesamallergie."
WIFE = "Meine Frau hat eine Sesamallergie."
NO_NUTS = "Nein, keine Nussallergie."


@pytest.mark.parametrize(
    ("kept", "denial", "left"),
    [
        # A bare "no" after a named allergy denies a further one (#764).
        (["Ich habe eine Allergie.", "Gegen Erdnüsse."], "Nein.", 2),
        (["Ich habe eine Tomatenallergie."], "Nein.", 1),
        (["Ich habe eine Allergie, gegen Erdnüsse."], "Nein.", 1),
        # Nothing named: the "no" takes it back (`allergie_0012`), also next
        # to a dish or a question that names an allergen.
        (["Die Pizza mit Käse ohne Zwiebeln, ich habe eine Allergie."], "Keine.", 0),
        (["Ich habe keine Allergie. Ist da Erdnuss drin?"], "Nein.", 0),
        (["Ich habe eine Allergie.", "Ja."], "Keine.", 0),
        # A denial that names the allergen takes back only what names that.
        ([NUTS], NO_NUTS, 0),
        ([SESAME, NUTS], NO_NUTS, 1),
    ],
)
def test_what_a_bare_denial_takes_back(kept, denial, left):
    """`left`: how many of the kept sentences, from the first, stay."""
    assert _taken_back(kept, denial) == kept[:left]


def test_a_denial_never_takes_back_another_persons_allergy():
    assert _taken_back([NUTS, WIFE], NO_NUTS) == [WIFE]


@pytest.mark.parametrize("asked", [None, SAY_ASK_ALLERGY])
def test_a_denial_takes_no_one_elses_back(session, known_tenant, asked):
    """ "Nein." or "Ich habe keine Allergie" after another person's names only
    the caller: the sentence stays, also as the answer to the model's own
    question (Codex PR #788, P1)."""
    state = state_for(session, known_tenant, phone=CALLER)
    run(
        session,
        state,
        "Meine Frau hat eine Sesamallergie.",
        LLMTurn(say=asked or SAY_ALLERGY_NOTED),
    )

    run(
        session,
        state,
        "Nein." if asked else "Ich habe keine Allergie.",
        LLMTurn(say=SAY_ALLERGY_NOTED),
    )

    assert state.allergy_said == ["Meine Frau hat eine Sesamallergie."]


def test_the_yes_to_a_read_back_with_an_allergy_note_is_no_answer_about_it(
    session, known_tenant
):
    state = state_for(session, known_tenant, phone=CALLER)
    state.stage = "readback_pending"
    state.readback = (
        "Einmal 13 Pho Bo, WICHTIG !!! Kunde ist allergisch auf Nüsse. Passt das so?"
    )
    guards.note_said(state, state.readback)

    run(session, state, "Ja, bitte.", LLMTurn(say="Gut."))

    assert state.allergy_said == []


def test_a_newly_armed_sentence_and_an_answer_about_it_drop_the_read_back(
    session, known_tenant
):
    """The answer "Gegen Erdnüsse." after the model's question names what the
    read-back lacks: a yes must not confirm it (Codex PR #788, P1)."""
    state = state_for(session, known_tenant, phone=CALLER)
    run(
        session,
        state,
        "Einmal die Pho Bo auf Müller.",
        search(),
        draft(session, known_tenant),
        LLMTurn(say=SAY_ASK_ALLERGY),
    )
    assert state.stage == "readback_pending"

    run(session, state, "Gegen Erdnüsse.", LLMTurn(say=SAY_ALLERGY_NOTED))
    assert state.stage != "readback_pending"


def test_the_core_writes_the_guests_words_on_a_table_cut_to_its_field(
    session, known_tenant
):
    state = state_for(session, known_tenant, phone=CALLER)
    table = {
        "guest_name": "Müller",
        "phone": CALLER,
        "party_size": 2,
        "reserved_for": "2026-09-16T19:00:00+02:00",
        "note": "Fensterplatz",
    }
    said = "Ein Tisch für zwei auf Müller, ich habe Zöliakie " + "und so weiter " * 40

    run(
        session,
        state,
        said,
        LLMTurn(tool_call=ToolCall("create_reservation", table)),
        LLMTurn(tool_call=ToolCall("create_reservation", table)),
        LLMTurn(say=SAY_ALLERGY_NOTED),
    )

    [note] = session.scalars(select(Reservation.note)).all()
    assert note.startswith("WICHTIG !!! Gast sagte: Ein Tisch für zwei")
    # Cut to the field (the schema strips the space the cut left).
    assert len(note) >= guards.NOTE_MAX_FOR["create_reservation"] - 1
    assert state.table_note == note


@pytest.mark.parametrize(
    "later", ["Und mein Sohn hat eine Erdnussallergie.", "Gegen Erdnüsse."]
)
def test_a_cut_that_drops_a_later_sentence_leaves_the_draft_to_the_team(later):
    """A long first sentence must not push a later one off the 200 characters
    of an order note: the team takes the call instead (ledger #764 review)."""
    state = _named()
    state.allergy_said = [
        "Ja hallo, ich möchte gern was zum Abholen bestellen, und ich muss "
        "vorher sagen, ich habe eine ziemlich starke Laktoseintoleranz, das "
        "ist wirklich wichtig für mich.",
        later,
    ]

    assert guards.with_the_guests_words(state, ToolCall("draft_order", {})) is None


LONG = (
    "Ja hallo, ich möchte gern was zum Abholen bestellen, und ich muss vorher "
    "sagen, ich habe eine ziemlich starke Laktoseintoleranz, das ist wirklich "
    "wichtig für mich, also bitte wirklich gut aufpassen."
)


def _noted(*said: str) -> str | None:
    state = _named()
    state.allergy_said = list(said)
    noted = guards.with_the_guests_words(state, ToolCall("draft_order", {}))
    return None if noted is None else noted.args["note"]


def test_a_cut_that_keeps_the_allergy_word_but_drops_the_allergen_goes_to_the_team():
    """ "allergisch" alone tells the kitchen nothing: the allergens the
    sentence names must survive the cut too (ledger #764 review)."""
    said = "Ich bin allergisch, und zwar " + "wirklich " * 20 + "gegen Erdnüsse."

    assert _noted(said) is None


def test_a_cut_may_drop_a_kept_yes_but_not_the_answer_itself():
    """ "Ja, genau." behind a long sentence carries nothing; an answer such as
    "Gegen Erdnüsse!" keeps all but its last mark."""
    noted = f"WICHTIG !!! Gast sagte: {LONG}"[:200].rstrip()
    assert _noted(LONG, "Ja, genau.") == noted and "Laktose" in noted
    answer = "Gegen " + "Erdnüsse und " * 10 + "Mandeln!"
    room = 200 - len("WICHTIG !!! Gast sagte: Ich habe eine Allergie. ")

    assert _noted("Ich habe eine Allergie.", answer[:room] + "!") is not None


def test_an_answer_alone_takes_the_field_when_it_fits():
    """Only "Gegen Erdnüsse." kept, the model's note too long: the answer
    takes the field whole, though it has no health word (ledger #764)."""
    state = _named()
    state.allergy_said = ["Gegen Erdnüsse."]
    call = ToolCall("draft_order", {"items": [], "note": "bitte klingeln " * 13})

    noted = guards.with_the_guests_words(state, call)

    assert noted is not None
    assert noted.args["note"] == "WICHTIG !!! Gast sagte: Gegen Erdnüsse"


def test_the_guests_whole_words_take_the_field_from_a_long_model_note():
    """The guest's sentences fit alone, only not next to the model's note:
    they take the field whole, the last one without its full stop."""
    state = _named()
    state.allergy_said = ["Ich habe eine Allergie.", "Gegen Erdnüsse."]
    call = ToolCall("draft_order", {"items": [], "note": "bitte klingeln " * 13})

    noted = guards.with_the_guests_words(state, call)

    assert noted is not None
    assert noted.args["note"] == (
        "WICHTIG !!! Gast sagte: Ich habe eine Allergie. Gegen Erdnüsse"
    )


def test_the_guests_words_go_behind_the_models_order_note():
    state = _named()
    call = ToolCall("draft_order", {"items": [], "note": "bitte klingeln"})

    noted = guards.with_the_guests_words(state, call)
    assert noted is not None

    assert noted.args["note"] == (
        "bitte klingeln. WICHTIG !!! Gast sagte: Ich habe eine Nussallergie"
    )


@pytest.mark.parametrize("note", ["nussfrei", "keine Spuren von Nüssen"])
def test_a_note_that_promises_a_dish_free_of_an_allergen_goes_back(
    session, known_tenant, note
):
    """L17: read back, the note would promise what no database value holds."""
    state = state_for(session, known_tenant, phone=CALLER)

    run(
        session,
        state,
        "Einmal die Pho Bo auf Müller.",
        search(),
        draft(session, known_tenant, line_note=note),
        draft(session, known_tenant, line_note="ohne Zwiebeln"),
        LLMTurn(say="Passt das so?"),
    )

    assert drafts_logged(session, state) == ["invalid_input", None]
    assert stored(session) == (None, ["ohne Zwiebeln"])


def test_a_refused_draft_stays_on_the_card_when_the_model_fails(session, known_tenant):
    """The net sent the draft back, then the model failed: the outage card
    still names the order the guest asked for."""

    class Failing(FakeLLM):
        def next_turn(self, *args, **kwargs):
            if not self._turns:
                raise LLMError("down")
            return super().next_turn(*args, **kwargs)

    state = state_for(session, known_tenant, phone=CALLER)
    llm = Failing([search(), draft(session, known_tenant)])
    result = ConversationLoop(session, llm, "system", now=CLOSED).run_turn(
        state, "Einmal die Pho Bo auf Müller, ich habe eine Nussallergie."
    )

    assert result.ended
    card = session.scalars(select(Callback.summary)).one()
    assert "1x 13 Pho Bo" in card
    assert "Nuss" not in card
    # The allergy is on no note yet: the team is told to ask (ledger #764).
    assert card.count(SUMMARY_NOTE_TO_CLEAR) == 1


@pytest.mark.parametrize(
    "said", ["Ich habe eine Nussallergie.", "Ich reagiere auf Nüsse."]
)
def test_an_allergy_on_no_note_marks_the_card_without_naming_it(
    session, known_tenant, said
):
    """An allergy said before any dish, then the model fails: the card tells
    the team to ask about a kitchen note, never which allergy (L13)."""
    state = state_for(session, known_tenant, phone=CALLER)
    run(session, state, said, LLMTurn(say=SAY_ALLERGY_NOTED))

    class Down(FakeLLM):
        def next_turn(self, *args, **kwargs):
            raise LLMError("down")

    ConversationLoop(session, Down([]), "system", now=CLOSED).run_turn(
        state, "Was haben Sie denn heute?"
    )

    card = session.scalars(select(Callback.summary)).one()
    assert SUMMARY_NOTE_TO_CLEAR in card
    assert "Nuss" not in card


def test_order_and_table_notes_reach_the_model(session, known_tenant):
    state = state_for(session, known_tenant, phone=CALLER)
    run(
        session,
        state,
        "Einmal die Pho Bo auf Müller, bitte alles vegan halten.",
        search(),
        draft(session, known_tenant, order_note="bitte alles vegan halten"),
        LLMTurn(say="Passt das so?"),
    )

    assert state.to_prompt_json()["order_note"] == "bitte alles vegan halten"


def test_a_newly_armed_sentence_drops_the_read_back(session, known_tenant):
    state = state_for(session, known_tenant, phone=CALLER)
    run(
        session,
        state,
        "Einmal die Pho Bo auf Müller.",
        search(),
        draft(session, known_tenant),
        LLMTurn(say="Passt das so?"),
    )
    assert state.stage == "readback_pending"

    run(session, state, "Und meine Frau hat Zöliakie.", LLMTurn(say=SAY_ALLERGY_NOTED))
    assert state.stage != "readback_pending"


def test_guests_words_that_lose_the_allergy_in_the_cut_go_to_the_team(
    session, known_tenant
):
    """The allergy at the end of a sentence longer than the note: cut, the
    note would keep the preamble and drop the allergy (Codex PR #788, P1).
    The team takes the call, nothing is stored."""
    state = state_for(session, known_tenant, phone=CALLER)
    table = {
        "guest_name": "Müller",
        "phone": CALLER,
        "party_size": 2,
        "reserved_for": "2026-09-16T19:00:00+02:00",
    }
    said = (
        "Ein Tisch für zwei auf Müller "
        + "und so weiter " * 40
        + "und ich habe eine Erdnussallergie"
    )

    result, _ = run(
        session,
        state,
        said,
        LLMTurn(tool_call=ToolCall("create_reservation", table)),
        LLMTurn(tool_call=ToolCall("create_reservation", table)),
    )

    assert result.ended or result.say
    assert session.scalars(select(Reservation)).all() == []
    assert state.handoff_summary is not None
    assert "Erdnuss" not in state.handoff_summary


@pytest.mark.parametrize(
    "said",
    [
        "Ich reagiere auf Erdbeeren.",
        "Ich darf keine Erdbeeren essen.",
    ],
)
def test_a_bare_no_to_a_further_allergy_keeps_one_named_any_way(said):
    """After a sentence that names what the guest reacts to, also in the
    words only the cards read (`_ingredients`), a "Nein." to "Haben Sie sonst
    noch Allergien?" denies a further one (#940 review; ledger #764 for the
    wish parser's). An allergen in front of the health part stays no allergy
    ("Die Pizza mit Käse, ich habe eine Allergie.", `guards.reacts_to`)."""
    assert _taken_back([said], "Nein.") == [said]


PEANUT = "Einmal die Pho Bo auf Müller, ich habe eine Erdnussallergie."
PEANUT_NOTE = "WICHTIG !!! Allergie: Erdnuss"
LATER_SESAME = "Ach, und meine Frau verträgt keinen Sesam."


def test_a_second_allergy_said_after_the_read_back_reaches_a_note(
    session, known_tenant
):
    """#929: a second allergy said after the read-back must not pass on the
    first allergy's note. The redraft with only the old note goes back once
    with the new sentence in the hint; the model notes it on the dish."""
    state = state_for(session, known_tenant, phone=CALLER)
    run(
        session,
        state,
        PEANUT,
        search(),
        draft(session, known_tenant, line_note=PEANUT_NOTE),
        LLMTurn(say=SAY_ALLERGY_NOTED),
    )
    both = f"{PEANUT_NOTE}. WICHTIG !!! Allergie (Frau): Sesam"

    run(
        session,
        state,
        LATER_SESAME,
        draft(session, known_tenant, line_note=PEANUT_NOTE),
        draft(session, known_tenant, line_note=both),
        LLMTurn(say=SAY_ALLERGY_NOTED),
    )

    assert state.allergy_said == [PEANUT, LATER_SESAME]
    assert drafts_logged(session, state) == [None, "invalid_input", None]
    assert state.cart[0].get("note") == both


def test_a_second_allergy_the_model_leaves_out_twice_gets_the_guests_words(
    session, known_tenant
):
    """Sent twice without the sesame, the core writes the guest's words on the
    order: the sesame reaches the kitchen, never dropped (#929)."""
    state = state_for(session, known_tenant, phone=CALLER)
    run(
        session,
        state,
        PEANUT,
        search(),
        draft(session, known_tenant, line_note=PEANUT_NOTE),
        LLMTurn(say=SAY_ALLERGY_NOTED),
    )

    run(
        session,
        state,
        LATER_SESAME,
        draft(session, known_tenant, line_note=PEANUT_NOTE),
        draft(session, known_tenant, line_note=PEANUT_NOTE),
        LLMTurn(say=SAY_ALLERGY_NOTED),
    )

    assert drafts_logged(session, state) == [None, "invalid_input", None]
    assert state.order_note is not None and "Sesam" in state.order_note


def _peanut_drafted(session, known_tenant) -> ConversationState:
    state = state_for(session, known_tenant, phone=CALLER)
    run(
        session,
        state,
        PEANUT,
        search(),
        draft(session, known_tenant, line_note=PEANUT_NOTE),
        LLMTurn(say=SAY_ALLERGY_NOTED),
    )
    return state


def test_the_model_may_ask_for_which_dish_a_later_allergy_is(session, known_tenant):
    """Maxi (#929): unclear for which dish, the model asks with the fixed
    question, a core sentence of the allergy turn. The answer names a dish,
    not an allergy: it is not kept for the net (deep review), and the draft
    that notes the sesame passes."""
    state = _peanut_drafted(session, known_tenant)

    asked, _ = run(
        session,
        state,
        LATER_SESAME,
        draft(session, known_tenant, line_note=PEANUT_NOTE),
        LLMTurn(say=guards.SAY_ASK_ALLERGY_DISH),
    )
    assert asked.say == [guards.SAY_ASK_ALLERGY_DISH]
    assert state.stage != "readback_pending"

    both = f"{PEANUT_NOTE}. WICHTIG !!! Allergie (Frau): Sesam"
    run(
        session,
        state,
        "Für die Pho Bo.",
        draft(session, known_tenant, line_note=both),
        LLMTurn(say=SAY_ALLERGY_NOTED),
    )

    assert state.allergy_said == [PEANUT, LATER_SESAME]
    assert drafts_logged(session, state) == [None, "invalid_input", None]
    assert state.cart[0].get("note") == both


def test_after_the_dish_question_a_draft_without_it_gets_the_guests_words(
    session, known_tenant
):
    """The hint was given the turn before: the answer turn's draft without the
    sesame gets the guest's words at once, never the question again each
    turn (deep review)."""
    state = _peanut_drafted(session, known_tenant)
    run(
        session,
        state,
        LATER_SESAME,
        draft(session, known_tenant, line_note=PEANUT_NOTE),
        LLMTurn(say=guards.SAY_ASK_ALLERGY_DISH),
    )

    run(
        session,
        state,
        "Für die Pho Bo.",
        draft(session, known_tenant, line_note=PEANUT_NOTE),
        LLMTurn(say=SAY_ALLERGY_NOTED),
    )

    assert drafts_logged(session, state) == [None, "invalid_input", None]
    assert state.order_note is not None and "keinen Sesam" in state.order_note


def test_the_dish_question_leaves_an_open_allergen_question_open(session, known_tenant):
    """Only "Worauf genau ...?" is answered by an allergen; the dish question
    must not clear the letters a turn left open (deep review)."""
    state = state_for(session, known_tenant, phone=CALLER)

    run(
        session,
        state,
        "Ich habe eine Erdnussallergie, ist Sesam in der Pho Bo?",
        LLMTurn(say=guards.SAY_ASK_ALLERGY_DISH),
    )

    assert "N" in state.allergen_question_codes


def _two(*notes: tuple[str | None, str | None]) -> ToolCall:
    """A draft of one line per (line note, order note) pair; the order note
    of the last pair counts."""
    args: dict = {"items": []}
    for line, whole in notes:
        args["items"].append({"menu_item_id": "x", "quantity": 1, "note": line})
        if whole is not None:
            args["note"] = whole
    return ToolCall("draft_order", args)


def _peanut_then_sesame() -> ConversationState:
    state = _named()
    state.allergy_said = [PEANUT, LATER_SESAME]
    return state


def test_the_net_wants_every_allergen_the_guest_named_on_a_note():
    state = _peanut_then_sesame()

    refused = guards.unnoted_allergy(state, _two((PEANUT_NOTE, None)))

    assert refused is not None and refused.error_code == "invalid_input"
    hint = refused.hint or ""
    # Only the sentence no note covers, and the fixed question about the dish.
    assert "„Ach, und meine Frau verträgt keinen Sesam“" in hint
    assert "Erdnussallergie" not in hint
    assert guards.SAY_ASK_ALLERGY_DISH in hint


@pytest.mark.parametrize(
    "call",
    [
        _two((f"{PEANUT_NOTE}. WICHTIG !!! Allergie (Frau): Sesam", None)),
        _two((PEANUT_NOTE, None), ("WICHTIG !!! Allergie (Frau): Sesam", None)),
        _two((PEANUT_NOTE, "WICHTIG !!! Frau verträgt keinen Sesam")),
        _two((None, f"WICHTIG !!! Gast sagte: {PEANUT} {LATER_SESAME}")),
    ],
)
def test_the_net_passes_when_some_health_note_names_each_allergen(call):
    assert guards.unnoted_allergy(_peanut_then_sesame(), call) is None


def test_an_answer_to_the_question_counts_with_all_it_names():
    """ "Gegen Sesam." has no health word; it answers the question and names
    what the kitchen must know."""
    state = _named()
    state.allergy_said = [PEANUT, "Ich habe noch eine Allergie.", "Gegen Sesam."]

    refused = guards.unnoted_allergy(state, _two((PEANUT_NOTE, None)))

    assert refused is not None
    assert "„Gegen Sesam“" in (refused.hint or "")


@pytest.mark.parametrize(
    "later",
    [
        # Coeliac disease names no letter but gluten (deep review).
        "Und meine Frau hat Zöliakie.",
        # An ingredient without a letter, by its word.
        "Ach und meine Frau hat eine Kiwiallergie.",
        # The allergen in a sentence of its own beside the health one.
        "Meine Frau hat eine Allergie. Sesam.",
        "Sesam darf meine Frau nicht essen, sie ist allergisch.",
    ],
)
def test_a_later_allergy_without_its_letter_on_the_first_note_goes_back(later):
    state = _named()
    state.allergy_said = [PEANUT, later]

    refused = guards.unnoted_allergy(state, _two((PEANUT_NOTE, None)))

    assert refused is not None
    assert later.rstrip(".") in (refused.hint or "")


@pytest.mark.parametrize(
    ("later", "note"),
    [
        ("Und meine Frau hat Zöliakie.", "WICHTIG !!! Zöliakie (Frau)"),
        ("Und meine Frau hat Zöliakie.", "WICHTIG !!! Frau: kein Gluten, Allergie"),
        ("Ach und meine Frau hat eine Kiwiallergie.", "WICHTIG !!! Allergie: Kiwi"),
    ],
)
def test_coeliac_disease_and_an_ingredient_pass_on_a_note_that_names_them(later, note):
    state = _named()
    state.allergy_said = [PEANUT, later]

    call = _two((PEANUT_NOTE, None), (note, None))

    assert guards.unnoted_allergy(state, call) is None


def test_an_ingredient_counts_only_as_a_word_of_its_own():
    """ "Erbsen" is not on a note that names "Kichererbsen" (gap sweep)."""
    state = _named()
    state.allergy_said = [
        "Ich habe eine Kichererbsenallergie.",
        "Meine Frau ist allergisch gegen Erbsen.",
    ]

    call = _two(("WICHTIG !!! Kichererbsenallergie", None))

    assert guards.unnoted_allergy(state, call) is not None


def test_a_note_without_a_health_word_does_not_count_for_the_letters():
    """ "ohne Sesam" is no health note: the retention job would keep it (L13),
    and the kitchen reads it as a wish."""
    call = _two((PEANUT_NOTE, None), ("ohne Sesam", None))
    assert guards.unnoted_allergy(_peanut_then_sesame(), call) is not None


@pytest.mark.parametrize(
    ("note", "passes"),
    [
        # A denied allergy says the opposite of the wife's (deep review).
        ("WICHTIG !!! Allergie: Erdnuss, keine Sesamallergie", False),
        # "kein Sesam" is what the kitchen must do: it covers it.
        ("WICHTIG !!! Erdnussallergie, kein Sesam", True),
    ],
)
def test_a_note_counts_what_it_asks_not_what_it_denies(note, passes):
    call = _two((note, None))
    assert (guards.unnoted_allergy(_peanut_then_sesame(), call) is None) is passes


@pytest.mark.parametrize(
    "said",
    [
        # A denied allergen asks for no note.
        "Ich habe keine Sesamallergie, aber eine Erdnussallergie.",
        # A dish name of the call is no allergen the guest named.
        "Die Nudeln mit Garnelen, ich habe eine Erdnussallergie.",
        # Nor a wish before the part about health: no cheese allergy.
        "Die 13 mit extra Käse, ich habe eine Erdnussallergie.",
        # Nor a wish, a question or an allowance behind it (deep review).
        "Ich habe eine Erdnussallergie, bitte extra Sesam.",
        "Ich habe eine Erdnussallergie, ist Sesam in der 13?",
        "Ich habe eine Erdnussallergie, Sesam ist kein Problem.",
    ],
)
def test_letters_the_guest_did_not_name_as_theirs_ask_for_no_note(said):
    state = _named()
    state.allergy_said = [said]
    state.seen_items = {"x": {"name": "Nudeln mit Garnelen", "number": "47"}}

    assert guards.unnoted_allergy(state, _two((PEANUT_NOTE, None))) is None


@pytest.mark.parametrize(
    "said",
    [
        # Another person's denial takes back none of the caller's allergy.
        [
            "Die 13, ich habe eine Erdnussallergie.",
            "Meine Frau verträgt keinen Sesam.",
            "Meine Frau hat aber keine Erdnussallergie.",
        ],
        [
            "Die 13, ich habe eine Erdnussallergie.",
            "Meine Frau hat keine Erdnussallergie, aber sie verträgt keinen Sesam.",
        ],
        # Nor does the caller's denial take back another person's allergy.
        [
            "Mein Sohn hat eine Erdnussallergie.",
            "Meine Frau verträgt keinen Sesam.",
            "Ich habe keine Erdnussallergie.",
        ],
    ],
)
def test_a_denial_takes_back_only_the_same_persons_allergy(said):
    """Verify pass of the deep review: the peanut must still reach a note."""
    state = _named()
    state.allergy_said = said

    call = _two(("WICHTIG !!! Frau verträgt keinen Sesam", None))

    refused = guards.unnoted_allergy(state, call)
    assert refused is not None
    assert "Erdnussallergie" in (refused.hint or "")


def test_the_hint_asks_for_the_guests_words():
    """A wish in the quoted sentence must not become an allergy: the model
    writes the guest's words, never its reading of them (deep review)."""
    refused = guards.unnoted_allergy(_peanut_then_sesame(), _two((PEANUT_NOTE, None)))

    assert refused is not None
    assert "Worte des Gastes" in (refused.hint or "")


def test_a_table_gets_no_question_about_a_dish():
    """A reservation has no dish to ask about: the hint asks for the guest's
    words on the table (deep review)."""
    table = ToolCall("create_reservation", {"note": PEANUT_NOTE})

    refused = guards.unnoted_allergy(_peanut_then_sesame(), table)

    assert refused is not None
    assert guards.SAY_ASK_ALLERGY_DISH not in (refused.hint or "")
    assert "an den Tisch" in (refused.hint or "")


@pytest.mark.parametrize(
    "block",
    [
        "WICHTIG !!! Gast sagte:",
        "Wichtig!!! Gast sagte:",
        "WICHTIG !!! gast sagte :",
        "WICHTIG !!! Der Gast sagte:",
    ],
)
def test_the_guests_words_replace_their_earlier_block_never_twice(block):
    """A redraft carries the core's earlier "Gast sagte" block, also as a
    model copied it; the new one takes its place, so no sentence is noted
    twice and the cut keeps room (#929, deep review)."""
    state = _peanut_then_sesame()
    earlier = f"bitte klingeln. {block} {PEANUT.rstrip('.')}"
    call = ToolCall("draft_order", {"items": [], "note": earlier})

    noted = guards.with_the_guests_words(state, call)

    assert noted is not None
    assert noted.args["note"] == (
        f"bitte klingeln. WICHTIG !!! Gast sagte: {PEANUT} "
        "Ach, und meine Frau verträgt keinen Sesam"
    )
