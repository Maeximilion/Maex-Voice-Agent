"""agent/guards.py: the core holds hard rules 1 to 3, whatever the model returns.

No database: the guards read the conversation state and the guest's sentence.
The way through the loop is in test_agent_loop.py.
"""

import uuid

import pytest

from api.agent.consent import is_yes
from api.agent.dispatch import ToolResult
from api.agent.guards import (
    HINT_CALLBACK_PHONE,
    HINT_CONFIRM,
    HINT_GUEST_PHONE,
    HINT_NO_DRAFT,
    begin_turn,
    false_booking,
    note_said,
    refusal,
    take_cart,
)
from api.agent.llm import ToolCall
from api.agent.state import ConversationState, apply_state_patch, apply_tool_result

ENTE = str(uuid.uuid4())
PHO = str(uuid.uuid4())
SUPPE = str(uuid.uuid4())
REIS = str(uuid.uuid4())


def new_state(**fields):
    return ConversationState(call_id=uuid.uuid4(), tenant_id=uuid.uuid4(), **fields)


READBACK = (
    "Ein Tisch für vier Personen morgen um sieben Uhr, auf den Namen Müller. "
    "Passt das so?"
)


def read_back(entity="reservation"):
    """A state in which a draft was read to the guest in an earlier turn."""
    draft_id = uuid.uuid4()
    state = new_state(
        stage="readback_pending",
        readback=READBACK,
        readback_heard=True,
        **{f"{entity}_id": draft_id},
    )
    return state, str(draft_id)


def drafted():
    """A state right after the draft tool answered: nothing was said yet."""
    state = new_state()
    draft_id = str(uuid.uuid4())
    apply_tool_result(
        state,
        "create_reservation",
        ToolResult(ok=True, data={"reservation_id": draft_id, "readback": READBACK}),
    )
    return state, draft_id


def confirm(entity_id, entity="reservation"):
    return ToolCall("confirm", {"entity": entity, "entity_id": entity_id})


CARD = {
    REIS: ("63", "Reis"),
    ENTE: ("47", "Ente knusprig"),
    PHO: ("13", "Pho Bo"),
    SUPPE: ("12", "Wan-Tan-Suppe"),
}
OFFER = "Meinen Sie Nummer 47 Ente knusprig oder Nummer 13 Pho Bo?"


def hit(item_id):
    number, name = CARD[item_id]
    return {"menu_item_id": item_id, "number": number, "name": name, "sold_out": False}


def searched(state, data):
    apply_tool_result(state, "search_menu", ToolResult(ok=True, data=data))


def draft(*item_ids):
    items = [{"menu_item_id": i, "quantity": 1} for i in item_ids]
    return ToolCall("draft_order", {"type": "pickup", "items": items})


# --- Rule 3: confirm only after an explicit yes to the draft that was read back ---


@pytest.mark.parametrize("entity", ["reservation", "order"])
@pytest.mark.parametrize("said", ["Ja, passt so.", "Genau.", "Ja bitte"])
def test_confirm_after_a_yes_to_the_read_back_draft_is_let_through(entity, said):
    state, draft_id = read_back(entity)
    start = begin_turn(state)

    assert refusal(state, start, said, confirm(draft_id, entity)) is None


@pytest.mark.parametrize(
    "said",
    [
        "Nein, das stimmt nicht",
        "Ja, aber ohne Ente",
        "Moment, wie viel kostet das?",
        "Das passt so nicht",
        "",
        # From the review of PR #222: a word of ordering is no assent.
        "Ich hätte gerne noch eine Suppe",
        "Okay, noch eine Suppe",
        "Ja, und noch eine Cola",
        "Gern.",
    ],
)
def test_confirm_without_an_explicit_yes_is_refused(said):
    """The case from the review: the guest rejects the readback, a fallible
    model returns `confirm` with the id from the state anyway."""
    state, draft_id = read_back()
    start = begin_turn(state)

    refused = refusal(state, start, said, confirm(draft_id))

    assert refused is not None
    assert refused.ok is False
    assert refused.error_code == "conflict"
    assert refused.hint
    assert refused.say is None  # for the model, nothing the guest should hear


def test_confirm_in_the_turn_that_created_the_draft_is_refused():
    """ "Ja, guten Tag, einen Tisch für vier": the yes came before the draft
    existed, so it is no consent to it."""
    state = new_state()
    start = begin_turn(state)
    draft_id = uuid.uuid4()
    apply_tool_result(
        state,
        "create_reservation",
        ToolResult(ok=True, data={"reservation_id": str(draft_id)}),
    )

    assert refusal(state, start, "Ja, guten Tag", confirm(str(draft_id))) is not None


def test_confirm_of_another_id_than_the_read_back_one_is_refused():
    state, _ = read_back()
    start = begin_turn(state)

    assert refusal(state, start, "Ja", confirm(str(uuid.uuid4()))) is not None


def test_confirm_after_the_draft_was_replaced_in_the_same_turn_is_refused():
    """The guest says yes, the model builds a new draft and confirms that one:
    the new draft was never read back."""
    state, old_id = read_back("order")
    start = begin_turn(state)
    new_id = str(uuid.uuid4())
    apply_tool_result(
        state, "draft_order", ToolResult(ok=True, data={"order_id": new_id})
    )

    assert refusal(state, start, "Ja", confirm(new_id, "order")) is not None
    assert refusal(state, start, "Ja", confirm(old_id, "order")) is not None


def test_confirm_after_a_failed_correction_is_refused():
    """A correction that fails drops the readback (state.py); a yes in the same
    turn must not confirm the draft the guest just corrected."""
    state, draft_id = read_back("order")
    start = begin_turn(state)
    apply_tool_result(
        state, "draft_order", ToolResult(ok=False, error_code="invalid_input")
    )

    assert refusal(state, start, "Ja", confirm(draft_id, "order")) is not None


def test_confirm_without_any_draft_is_refused():
    """With no draft at all the hint names the tool that makes one: "read it
    back first" pointed nowhere, and a real model confirmed made-up ids."""
    state = new_state()

    refused = refusal(state, begin_turn(state), "Ja", confirm(str(uuid.uuid4())))

    assert refused is not None
    assert refused.hint == HINT_NO_DRAFT


def test_confirm_of_a_draft_not_read_back_keeps_the_readback_hint():
    state, draft_id = drafted()

    refused = refusal(state, begin_turn(state), "Ja", confirm(draft_id))

    assert refused is not None
    assert refused.hint == HINT_CONFIRM


@pytest.mark.parametrize(
    ("text", "yes"),
    [
        # Nothing but the assent.
        ("Ja", True),
        ("Ja, passt so.", True),
        ("Ja, gerne.", True),
        ("Ja bitte, vielen Dank!", True),
        ("Genau so, danke schön.", True),
        ("Ja, das ist richtig so.", True),
        ("Okay, alles klar.", True),
        ("Stimmt so.", True),
        ("Ja, in Ordnung.", True),
        ("Ja, passt, kein Problem.", True),
        ("Ja, nicht schlecht.", True),
        ("Jawohl", True),
        # A yes word next to anything else is the start of a change.
        ("Ich hätte gerne noch eine Suppe", False),
        ("Gerne", False),
        ("Okay, noch eine Suppe", False),
        ("Ja, und noch eine Cola", False),
        ("Ja, guten Tag, einen Tisch für vier", False),
        ("Ja, zweimal", False),
        ("Ja, 2", False),
        ("Ja, die 23", False),
        ("Genau, auf Müller", False),
        ("Ja, das ist nicht gut", False),
        ("Ja, kein Reis", False),
        ("Passt mir morgen besser", False),
        ("Richtig, aber keine Ente.", False),
        ("Nein, ja doch nicht", False),
        ("Im Januar", False),
        ("", False),
        # A question asks whether it is right, it does not say so (#251).
        ("Ist das richtig?", False),
        ("Stimmt das so?", False),
        ("Passt das?", False),
        ("Ja?", False),
        ("Ja, passt das so?", False),
        # Speech recognition drops the question mark: verb before "das" or "es"
        # is the word order of a question.
        ("Ist das richtig", False),
        ("Ist das okay", False),
        ("Stimmt das so", False),
        ("Passt das", False),
        ("Ist es so richtig", False),
        ("Ja ist das richtig", False),
        # "alles" after "ist" asks as well.
        ("Ist alles richtig", False),
        ("Ist alles in Ordnung", False),
        ("Ja ist alles korrekt", False),
        # The same words as a statement stay a yes.
        ("Das ist richtig.", True),
        ("Das stimmt so.", True),
        ("Das passt.", True),
        ("Ja, das passt so.", True),
        ("Passt.", True),
        ("Stimmt.", True),
        ("Ja, es ist alles richtig.", True),
        ("Genau, das ist es.", True),
        ("Ja, das stimmt alles.", True),
        ("Ja, es passt alles.", True),
        ("Ja, es ist alles richtig so.", True),
        # "Passt alles" and "Stimmt alles" are a yes with the "es" left out,
        # the first one word for word from the eval targets (abholung_0064).
        ("Ja, passt alles.", True),
        ("Passt alles", True),
        ("Stimmt alles so", True),
        # Punctuation is not spoken: a comma between verb and subject changes
        # nothing about the question (Codex, PR #258). The end of a sentence
        # does: what follows is a statement of its own.
        ("Stimmt, das so", False),
        ("Ist, das richtig", False),
        ("Ja, stimmt, das so", False),
        ("Stimmt. Das passt.", True),
        ("Ja. Das stimmt so.", True),
        # With another word in the first place the verb comes second: a
        # statement, whatever follows the verb (Codex, PR #258).
        ("Natürlich ist das richtig.", True),
        ("Ja, natürlich passt das so.", True),
        ("Genau so ist das richtig.", True),
        ("Sicher stimmt das.", True),
        # An assent or a thanks in front stands outside the clause.
        ("Genau, ist das richtig", False),
        ("Danke, passt das so", False),
        # No verb at all: "Alles richtig" asks as often as it answers, and the
        # text has no voice (Codex, PR #258). A yes only with a word that
        # cannot ask next to it.
        ("Alles richtig", False),
        ("Alles korrekt so", False),
        ("Alles okay", False),
        ("Alles so richtig, danke", False),
        ("Ja, alles richtig", True),
        ("Genau, alles korrekt so.", True),
        ("Alles richtig. Passt.", True),
        # No subject: "Ist so richtig" is "Ist das so richtig?" as much as
        # "Das ist so richtig" (Codex, PR #258). The same rule.
        ("Ist so richtig", False),
        ("Ist richtig", False),
        ("Ist okay so", False),
        ("Danke, ist okay", False),
        ("Ja, ist okay.", True),
        ("Ja, ist so richtig.", True),
        # Verbs that are an assent themselves need no subject.
        ("Passt so.", True),
    ],
)
def test_yes_is_a_sentence_that_is_nothing_but_the_assent(text, yes):
    assert is_yes(text) is yes


# --- Rule 3: a yes confirms only what the guest heard (#251) ---------------------


def test_confirm_of_a_draft_that_was_never_read_out_is_refused():
    """The model drafts, then asks something else. The guest's "Ja" answers
    that question, not a table and a time they never heard."""
    state, draft_id = drafted()
    note_said(state, "Darf es sonst noch etwas sein?")

    start = begin_turn(state)

    assert start.readback_id is None
    assert refusal(state, start, "Ja", confirm(draft_id))


def test_confirm_after_the_sentence_was_spoken_is_let_through():
    state, draft_id = drafted()
    note_said(state, READBACK)

    assert refusal(state, begin_turn(state), "Ja", confirm(draft_id)) is None


BODY = READBACK.removesuffix(" Passt das so?")


@pytest.mark.parametrize(
    ("said", "heard"),
    [
        (READBACK, True),
        (f"Ich wiederhole: {READBACK}", True),
        # In front of the read-back: polite words, or a sentence that has ended.
        (f"Gerne, {READBACK}", True),
        (f"Gerne, zur Kontrolle: {READBACK}", True),
        (f"Also {READBACK}", True),
        # A lead-in is a whole phrase. One word of it in front of the draft
        # changes what is said: "Noch ein Tisch ..." is another table (Codex,
        # PR #258).
        (f"Noch {READBACK}", False),
        (f"Also noch {READBACK}", False),
        (f"Ich wiederhole noch {READBACK}", False),
        # A sentence of its own in front may say anything about the draft, and
        # punctuation is no proof that it does not (Codex, PR #258).
        (f"Wir haben bis 22 Uhr geöffnet. {READBACK}", False),
        (f"Das ist falsch: {READBACK}", False),
        (f"Das ist falsch: {BODY}.", False),
        # Words that run into the read-back change what it says (Codex, PR
        # #258): "Einmal statt zweimal ..." is another order.
        (f"Leider nicht {READBACK}", False),
        (f"Statt {READBACK}", False),
        (f"Nicht wie besprochen {READBACK}", False),
        (READBACK.upper(), True),
        (READBACK.replace(",", "").replace(".", " "), True),
        # The closing question may be the model's own; the content may not.
        (f"{BODY} Ist das so richtig?", True),
        (f"{BODY} Stimmt das?", True),
        # Without the question for the yes, a "Ja" answers nothing.
        (BODY, False),
        # A question of filler words alone asks something else.
        (f"{BODY}. Ist es sicher?", False),
        # An assent word in another role: "richtig" means "really" here, and
        # the question is about safety (Codex, PR #258). Only fixed forms ask
        # for the yes.
        (f"{BODY}. Ist es richtig sicher?", False),
        (f"{BODY}. Passt das wirklich so?", False),
        (f"{BODY}. Stimmt das so und passt es?", False),
        (f"{BODY}. Passt es so?", True),
        (f"{BODY}. Ist das korrekt?", True),
        (f"{BODY}. Ist das in Ordnung so?", False),
        # Another question after the read-back: the yes would answer that one.
        (f"{READBACK} Möchten Sie noch einen Hochstuhl?", False),
        (f"{BODY} Möchten Sie noch einen Hochstuhl?", False),
        (f"{READBACK} Und zu trinken?", False),
        # The name left out, a number changed, half of it: not the draft.
        ("Ein Tisch für vier Personen morgen um sieben Uhr. Passt das so?", False),
        (READBACK.replace("vier", "zwei"), False),
        (READBACK.replace("Müller", "Möller"), False),
        ("Ein Tisch für vier Personen. Passt das so?", False),
        ("Passt das so?", False),
        ("", False),
    ],
)
def test_the_guest_heard_the_draft_when_its_words_were_spoken(said, heard):
    state, _ = drafted()

    note_said(state, said)

    assert state.readback_heard is heard


def test_a_read_back_that_is_one_question_has_to_be_spoken_whole():
    """Nothing to cut when the sentence has no part before its question."""
    state, _ = drafted()
    state.readback = "Zweimal die 47 zur Abholung, richtig?"

    note_said(state, "Richtig?")
    assert not state.readback_heard

    note_said(state, "Zweimal die 47 zur Abholung, richtig?")
    assert state.readback_heard


def test_only_the_last_sentence_counts_as_the_read_back():
    """Read out, then a side question answered: the guest's next "Ja" follows
    the answer, not the draft. The model has to read it out again, and gets the
    sentence for it."""
    state, draft_id = drafted()
    note_said(state, READBACK)
    assert state.readback_heard

    note_said(state, "Wir haben bis 22 Uhr geöffnet.")

    assert not state.readback_heard
    assert refusal(state, begin_turn(state), "Ja", confirm(draft_id))
    assert state.to_prompt_json()["readback"] == READBACK

    note_said(state, f"Wir haben bis 22 Uhr geöffnet. {READBACK}")
    assert refusal(state, begin_turn(state), "Ja", confirm(draft_id))

    note_said(state, f"Gern. {READBACK}")
    assert refusal(state, begin_turn(state), "Ja", confirm(draft_id)) is None


def test_a_booked_draft_is_not_handed_to_the_model_again():
    """After the yes the closing sentence is not the read-back. The sentence of
    the booked draft must not go back to the model as one it still has to read
    out (Codex, PR #258)."""
    state, _ = drafted()
    note_said(state, READBACK)
    apply_tool_result(state, "confirm", ToolResult(ok=True, data={}))

    note_said(state, "Vielen Dank, bis morgen.")

    assert state.readback is None
    assert "readback" not in state.to_prompt_json()


def test_the_sentence_goes_to_the_model_only_while_a_draft_waits():
    state, _ = drafted()
    state.stage = "collecting"

    assert "readback" not in state.to_prompt_json()


def test_a_new_draft_has_to_be_read_out_again():
    state, _ = read_back()
    assert state.readback_heard

    apply_tool_result(
        state,
        "create_reservation",
        ToolResult(
            ok=True,
            data={"reservation_id": str(uuid.uuid4()), "readback": "Ein neuer Satz."},
        ),
    )

    assert state.readback == "Ein neuer Satz."
    assert not state.readback_heard


def test_a_dropped_draft_takes_its_sentence_with_it():
    """A new slot check after the read-back drops the draft (state.py); the old
    sentence must not count as heard for whatever comes next."""
    state, _ = read_back()

    apply_tool_result(state, "check_slot", ToolResult(ok=True, data={}))

    assert state.readback is None
    assert not state.readback_heard


def test_the_model_gets_the_sentence_until_the_guest_heard_it():
    """After a refused `confirm` the model has to read the draft out, and it
    keeps nothing between turns. Once heard, the sentence is not sent again."""
    state, _ = drafted()
    assert state.to_prompt_json()["readback"] == READBACK

    note_said(state, READBACK)

    assert "readback" not in state.to_prompt_json()


# --- Rule 2: an order only takes ids from a clear match or the guest's choice ---


@pytest.mark.parametrize("match_type", ["exact_number", "alias", "fuzzy_single"])
def test_draft_with_an_id_from_a_clear_match_is_let_through(match_type):
    state = new_state()
    start = begin_turn(state)
    searched(state, {"match_type": match_type, "results": [hit(ENTE)]})

    assert refusal(state, start, "die 47", draft(ENTE)) is None


def test_draft_with_a_candidate_of_an_ambiguous_result_is_refused():
    """The case from the review: `search_menu` offers several dishes and the
    model copies the first id into the order without the guest choosing."""
    state = new_state()
    start = begin_turn(state)
    searched(state, {"match_type": "ambiguous", "results": [hit(ENTE), hit(PHO)]})

    refused = refusal(state, start, "die Ente", draft(ENTE))

    assert refused is not None
    assert refused.ok is False
    assert refused.error_code == "invalid_input"
    assert refused.hint


def test_candidate_is_usable_once_the_guest_has_answered_the_offer():
    """The offer is put to the guest; the id may enter an order from the turn
    in which the guest answers. Which candidate the answer means stays with
    the model and is measured by the evals; the readback and its yes follow."""
    state = new_state()
    start = begin_turn(state)
    searched(state, {"match_type": "ambiguous", "results": [hit(ENTE), hit(PHO)]})
    assert refusal(state, start, "die Ente", draft(PHO)) is not None
    note_said(state, OFFER)

    answer = begin_turn(state)

    assert refusal(state, answer, "die knusprige", draft(PHO)) is None
    assert refusal(state, answer, "die knusprige", draft(ENTE)) is None


def test_candidate_the_guest_never_heard_stays_unusable():
    """From the review of PR #222: the model does not pass the offer on and
    asks for something else. The guest answers that, and no candidate may
    enter an order: nobody was asked to choose."""
    state = new_state()
    begin_turn(state)
    searched(state, {"match_type": "ambiguous", "results": [hit(ENTE), hit(PHO)]})
    note_said(state, "Unter welcher Nummer erreichen wir Sie?")

    answer = begin_turn(state)

    assert refusal(state, answer, "0721 5551234", draft(ENTE)) is not None
    assert refusal(state, answer, "0721 5551234", draft(PHO)) is not None


@pytest.mark.parametrize(
    "said",
    [
        "Meinen Sie Ente knusprig?",  # by name
        "Meinen Sie die ENTE KNUSPRIG?",
        "Meinen Sie Nummer 47?",  # by card number
        "Nr. 47, richtig?",
    ],
)
def test_only_the_candidate_that_was_named_becomes_usable(said):
    state = new_state()
    begin_turn(state)
    searched(state, {"match_type": "ambiguous", "results": [hit(ENTE), hit(PHO)]})
    note_said(state, said)

    answer = begin_turn(state)

    assert refusal(state, answer, "ja", draft(ENTE)) is None
    assert refusal(state, answer, "ja", draft(PHO)) is not None


@pytest.mark.parametrize(
    "said",
    [
        "Um 13 Uhr oder um 47 nach?",  # a number without "Nummer" is no card number
        "Das macht 13,47 Euro.",
        "Macht 13 Euro 47, abholbereit in etwa 20 Minuten.",  # as the read-back says it
        "Nummer 147 habe ich nicht.",
        "Ente haben wir.",  # half a name names nothing
    ],
)
def test_number_or_word_in_passing_names_no_candidate(said):
    state = new_state()
    begin_turn(state)
    searched(state, {"match_type": "ambiguous", "results": [hit(ENTE), hit(PHO)]})
    note_said(state, said)

    answer = begin_turn(state)

    assert refusal(state, answer, "ja", draft(ENTE)) is not None
    assert refusal(state, answer, "ja", draft(PHO)) is not None


@pytest.mark.parametrize(
    "said",
    [
        "Welchen Preis möchten Sie wissen?",  # the case from the review
        "Die Reise dauert zwanzig Minuten.",
        "Wir haben auch Reisnudeln.",
        "Milchreis ist heute aus.",
    ],
)
def test_dish_name_inside_another_word_names_no_candidate(said):
    """From the review of PR #222: "reis" stands in "Preis". A name counts only
    as whole words, or a sentence about something else makes a dish usable
    that the guest never heard."""
    state = new_state()
    begin_turn(state)
    searched(state, {"match_type": "ambiguous", "results": [hit(REIS), hit(PHO)]})
    note_said(state, said)

    answer = begin_turn(state)

    assert refusal(state, answer, "ja", draft(REIS)) is not None


@pytest.mark.parametrize(
    "said",
    [
        "Meinen Sie Reis?",
        "Reis, richtig?",
        "Meinen Sie „Reis“ oder Pho Bo?",
        "Also REIS.",
        "Reis",
    ],
)
def test_dish_name_as_a_whole_word_names_the_candidate(said):
    state = new_state()
    begin_turn(state)
    searched(state, {"match_type": "ambiguous", "results": [hit(REIS), hit(PHO)]})
    note_said(state, said)

    answer = begin_turn(state)

    assert refusal(state, answer, "ja", draft(REIS)) is None


def test_offer_spoken_a_turn_later_still_counts():
    """An open allergy question goes first and the offer follows in the next
    turn, never both at once (Codex PR #139). The guest heard the offer then,
    so its candidates are usable from the turn after that, not before."""
    state = new_state()
    begin_turn(state)
    searched(state, {"match_type": "ambiguous", "results": [hit(ENTE), hit(PHO)]})
    note_said(state, "Wogegen sind Sie allergisch?")
    allergy = begin_turn(state)
    assert refusal(state, allergy, "Erdnüsse", draft(ENTE)) is not None
    note_said(state, OFFER)

    answer = begin_turn(state)

    assert refusal(state, answer, "die Ente", draft(ENTE)) is None


def test_draft_with_an_id_no_search_delivered_is_refused():
    state = new_state()
    start = begin_turn(state)
    searched(state, {"match_type": "exact_number", "results": [hit(ENTE)]})

    assert refusal(state, start, "die 47 und Suppe", draft(ENTE, SUPPE)) is not None


def test_sentence_with_several_positions_counts_each_part_by_its_own_match():
    state = new_state()
    start = begin_turn(state)
    searched(
        state,
        {
            "match_type": "positions",
            "positions": [
                {
                    "query": "die 47",
                    "ok": True,
                    "match_type": "exact_number",
                    "results": [hit(ENTE)],
                },
                {
                    "query": "Suppe",
                    "ok": True,
                    "match_type": "ambiguous",
                    "results": [hit(SUPPE), hit(PHO)],
                },
                {"query": "Dings", "ok": False, "error_code": "not_found"},
            ],
        },
    )

    assert refusal(state, start, "x", draft(ENTE)) is None
    assert refusal(state, start, "x", draft(ENTE, SUPPE)) is not None


def test_failed_search_makes_nothing_usable():
    state = new_state()
    start = begin_turn(state)
    apply_tool_result(
        state, "search_menu", ToolResult(ok=False, error_code="not_found")
    )

    assert refusal(state, start, "x", draft(ENTE)) is not None


def test_item_ids_never_go_into_the_prompt_state():
    """The core's memory is not the model's. A dish reaches the next turn only
    as a line of the order the core took (test_agent_cart.py)."""
    state = new_state()
    searched(state, {"match_type": "exact_number", "results": [hit(ENTE)]})
    searched(state, {"match_type": "ambiguous", "results": [hit(SUPPE), hit(PHO)]})

    assert ENTE not in str(state.to_prompt_json())
    assert PHO not in str(state.to_prompt_json())


@pytest.mark.parametrize(
    "call",
    [
        ToolCall("get_service_status"),
        ToolCall("search_menu", {"query": "die 47"}),
        ToolCall("check_slot", {"party_size": 4}),
        ToolCall("create_callback", {"phone": "+4972215551234"}),
        ToolCall("transfer_to_team", {"reason": "complaint"}),
        # Malformed arguments are for the validation in dispatch.py.
        ToolCall("draft_order", {"items": "die 47"}),
        ToolCall("draft_order", {}),
    ],
)
def test_other_calls_pass_the_guards(call):
    state = new_state(caller_phone="+4972215551234")

    assert refusal(state, begin_turn(state), "Hallo", call) is None


# --- Rule 1: a model writes only what a guest can say into the slots ---


def test_state_patch_keeps_what_a_guest_names():
    """Name and number only as the guest said them: test_agent_guest_details.py."""
    state = new_state(caller_phone="+4972215551234", said_words={"mueller": 1})
    patch = {
        "party_size": 4,
        "reserved_for": "2026-09-16T19:00:00+02:00",
        "guest_name": "Müller",
        "phone": "+4972215551234",
        "note": "mit Hochstuhl",
    }

    assert apply_state_patch(state, patch) == patch
    assert state.slots == patch


def test_state_patch_drops_facts_from_a_tool_result():
    """Seen with a local model: `open` and `closes_at` from get_service_status
    copied into the slots, from where the next prompt would present them as
    something the guest said."""
    state = new_state(slots={"phone": "+4972215551234"})

    applied = apply_state_patch(
        state,
        {"open": True, "closes_at": "22:00", "price_cents": 1290, "party_size": 4},
    )

    assert applied == {"party_size": 4}
    assert state.slots == {"phone": "+4972215551234", "party_size": 4}


@pytest.mark.parametrize(
    ("call", "hint"),
    [
        (
            ToolCall("create_callback", {"phone": None, "reason": "out_of_scope"}),
            HINT_CALLBACK_PHONE,
        ),
        (
            ToolCall("create_callback", {"phone": "", "reason": "out_of_scope"}),
            HINT_CALLBACK_PHONE,
        ),
        (ToolCall("create_callback", {"reason": "out_of_scope"}), HINT_CALLBACK_PHONE),
        (
            ToolCall("create_reservation", {"guest_name": None, "party_size": 2}),
            HINT_GUEST_PHONE,
        ),
        (
            ToolCall("draft_order", {"customer": {"name": None, "phone": None}}),
            HINT_GUEST_PHONE,
        ),
    ],
)
def test_a_booking_without_a_number_gets_the_phone_hint(call, hint):
    """The validation said only "phone: Input should be a valid string", and a
    real model sent create_callback without a number four times in a row."""
    state = new_state()

    refused = refusal(state, begin_turn(state), "Bitte rufen Sie zurück", call)

    assert refused is not None
    assert refused.hint == hint


def test_a_draft_without_a_customer_is_left_to_the_validation():
    state = new_state()
    call = ToolCall("draft_order", {"items": []})

    assert refusal(state, begin_turn(state), "Das wars", call) is None


@pytest.mark.parametrize(
    "say",
    [
        "Ihre Abholung ist bestätigt. Der Abholcode lautet 8842.",
        "Ihr Tisch ist reserviert.",
        "Das ist gebucht.",
        "Ihr Abholcode ist A12.",
        # Only the asking clause is exempt (Codex PR #347).
        "Ihre Bestellung ist bestätigt, möchten Sie noch etwas?",
        # A negation is not read: which words it reaches is grammar, and a
        # word rule for it let these through (Codex PR #347).
        "Das war nicht schwer, Ihr Tisch ist reserviert.",
        "Sie müssen nicht mehr warten und Ihr Tisch ist reserviert.",
        "Ihr Tisch ist nicht nur reserviert, sondern auch schon vorbereitet.",
        # So a denial is refused too, and so is a question; it costs the
        # model one hop.
        "Es ist noch nichts gebucht.",
        "Ihre Bestellung ist noch nicht bestätigt. Passt das so?",
        "Haben Sie bereits reserviert?",
        "Ihre Bestellung ist bestätigt und möchten Sie noch etwas?",
        "Möchten Sie den Abholcode per SMS?",
    ],
)
def test_a_claimed_booking_before_confirm_is_refused(say):
    assert false_booking(new_state(), say) is not None


@pytest.mark.parametrize(
    "say",
    [
        "Möchten Sie einen Tisch reservieren?",
        "Soll ich das so bestätigen?",
    ],
)
def test_a_sentence_without_a_booking_word_passes(say):
    assert false_booking(new_state(), say) is None


def test_while_a_draft_waits_nothing_is_said_to_be_booked():
    """After a confirmed order a new table draft waits for its yes: "Das ist
    auch gebucht" may mean the table (Codex PR #347). Strict: also the true
    claim about the order waits until the draft is decided."""
    state = new_state(booked={"order"}, stage="readback_pending")

    assert false_booking(state, "Das ist auch gebucht.") is not None
    assert false_booking(state, "Ihre Bestellung ist bestätigt.") is not None


def test_the_claim_verb_does_not_name_a_table():
    """ "reserviert" is the claim: after a confirmed order this closing is
    about the order alone (Codex PR #347)."""
    state = new_state(booked={"order"})

    assert false_booking(state, "Ihre Abholung ist für 18 Uhr reserviert.") is None
    assert false_booking(state, "Ihr Tisch ist reserviert.") is not None


def test_a_booked_table_is_no_booked_order():
    """After a confirmed table a pickup code is still made up (Codex PR #347)."""
    state = new_state(booked={"reservation"})

    assert false_booking(state, "Ihr Tisch ist reserviert.") is None
    assert false_booking(state, "Ihre Abholung ist bestätigt, Code 8842.") is not None


def test_a_new_draft_takes_back_the_booking_of_its_kind():
    state = new_state(booked={"order", "reservation"})
    apply_tool_result(
        state,
        "draft_order",
        ToolResult(ok=True, data={"order_id": str(uuid.uuid4()), "readback": "x"}),
    )

    assert state.booked == {"reservation"}
    assert false_booking(state, "Ihre Bestellung ist bestätigt.") is not None


def test_a_callback_without_a_number_points_to_the_one_in_the_slots():
    state = new_state(slots={"phone": "+4972215551234"})
    call = ToolCall("create_callback", {"phone": None, "reason": "out_of_scope"})

    refused = refusal(state, begin_turn(state), "Rufen Sie mich zurück", call)

    assert refused is not None
    assert refused.hint == HINT_GUEST_PHONE


def test_after_a_confirm_the_booking_may_be_named():
    state = new_state(booked={"order"}, stage="confirmed", pickup_code="A12")

    assert false_booking(state, "Ihr Abholcode ist A12.") is None
    assert false_booking(state, "Ihre Bestellung ist bestätigt.") is None


@pytest.mark.parametrize(
    "say",
    [
        # Names nothing: always refused, also right after a confirm, since
        # the same guest sentence may have asked for a table as well
        # (Codex PR #347).
        "Das ist gebucht.",
        # Names a table that is not booked; the period after "Nr." does not
        # hide the table (Codex PR #347).
        "Ihr Tisch Nr. 7 ist reserviert.",
        # Names an order and a table: both must be booked.
        "Ihre Bestellung ist bestätigt, möchten Sie noch einen Tisch reservieren?",
    ],
)
def test_after_an_order_alone_these_claims_are_refused(say):
    state = new_state(booked={"order"}, stage="confirmed")

    assert false_booking(state, say) is not None


def confirmed_order(code="A1"):
    state = new_state()
    order_id = str(uuid.uuid4())
    apply_tool_result(
        state, "draft_order", ToolResult(ok=True, data={"order_id": order_id})
    )
    apply_tool_result(state, "confirm", ToolResult(ok=True, data={"pickup_code": code}))
    return state


def test_only_the_confirmed_pickup_code_may_be_named():
    """After A1, "B99" sends the guest to the counter with a code that finds no
    order (Codex PR #347)."""
    state = confirmed_order("A1")

    assert false_booking(state, "Ihr Abholcode ist A1.") is None
    assert false_booking(state, "Ihr Abholcode ist B99.") is not None
    assert false_booking(state, "Ihr Code ist A1 oder B99.") is not None


def test_a_code_before_any_confirm_is_made_up():
    assert false_booking(new_state(), "Ihr Code lautet 8842.") is not None


def test_a_new_draft_takes_back_the_pickup_code():
    state = confirmed_order("A1")
    apply_tool_result(
        state, "draft_order", ToolResult(ok=True, data={"order_id": str(uuid.uuid4())})
    )

    assert state.pickup_code is None
    assert false_booking(state, "Ihr Abholcode ist A1.") is not None


def test_automatisch_names_no_table():
    state = confirmed_order("A1")

    assert false_booking(state, "Ihre Bestellung wurde automatisch bestätigt.") is None


def test_a_replayed_confirmed_draft_keeps_the_booking():
    """The same draft_order key after the confirm replays the confirmed order
    (domain/ordering/draft.py `_replay`); it is no new draft (Codex PR #347)."""
    state = confirmed_order("A1")
    order_id = str(state.order_id)
    apply_tool_result(
        state,
        "draft_order",
        ToolResult(ok=True, data={"order_id": order_id, "status": "confirmed"}),
    )

    assert state.stage == "confirmed"
    assert state.booked == {"order"}
    assert false_booking(state, "Ihr Abholcode ist A1.") is None


def test_a_new_cart_after_a_confirmed_order_is_not_booked():
    """A second order in the call: the first one's booking does not speak for
    it, while the confirmed order repeated as cart is no new one (Codex PR
    #347)."""
    first = [{"menu_item_id": PHO, "quantity": 1}]
    state = new_state(known_item_ids={PHO, REIS}, cart=first)
    apply_tool_result(
        state, "draft_order", ToolResult(ok=True, data={"order_id": str(uuid.uuid4())})
    )
    state.cart = first
    apply_tool_result(state, "confirm", ToolResult(ok=True, data={"pickup_code": "A1"}))

    assert take_cart(state, first) is None
    assert false_booking(state, "Ihre Bestellung ist bestätigt.") is None

    assert take_cart(state, [{"menu_item_id": REIS, "quantity": 2}]) is None
    assert false_booking(state, "Ihre Bestellung ist bestätigt.") is not None
    assert state.pickup_code is None


@pytest.mark.parametrize(
    "say",
    [
        "Einen Gutscheincode kann ich leider nicht prüfen.",
        "Den WLAN-Code kenne ich nicht.",
    ],
)
def test_other_codes_are_no_pickup_code(say):
    assert false_booking(new_state(), say) is None


def test_a_new_table_request_after_a_confirmed_table_is_not_booked():
    """After one confirmed table the guest asks for another: "Ihr Tisch ist
    reserviert" must wait for that one's confirm (Codex PR #347)."""
    state = new_state(booked={"reservation"}, stage="confirmed")
    apply_tool_result(
        state, "check_slot", ToolResult(ok=True, data={"available": True})
    )

    assert false_booking(state, "Ihr Tisch ist reserviert.") is not None
