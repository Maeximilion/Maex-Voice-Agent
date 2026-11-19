"""agent/guards.py: the core holds hard rules 1 to 3, whatever the model returns.

No database: the guards read the conversation state and the guest's sentence.
The way through the loop is in test_agent_loop.py.
"""

import uuid

import pytest

from api.agent.consent import is_yes
from api.agent.dispatch import ToolResult
from api.agent.guards import (
    ADDITIVE_CLAIM,
    ALLERGEN_CLAIM,
    HINT_CALLBACK_PHONE,
    HINT_CONFIRM,
    HINT_GUEST_PHONE,
    HINT_NO_DRAFT,
    NOTE_CLAIM,
    SAY_ALLERGENS_OFFER,
    SAY_ALLERGY_NOTED,
    SAY_ASK_ALLERGY,
    allergen_claim,
    begin_allergen_turn,
    begin_turn,
    claims_in_a_note,
    core_only,
    false_booking,
    is_open,
    leaves_a_question,
    note_said,
    note_tool,
    order_changed,
    refusal,
    sticky,
    table_says,
    take_cart,
    unlicensed,
)
from api.agent.llm import ToolCall
from api.agent.loop import _note_allergy_said
from api.agent.state import (
    ConversationState,
    apply_state_patch,
    apply_tool_result,
)
from api.domain.menu.details import say_allergens

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
@pytest.mark.parametrize(
    "said",
    ["Ja, passt so.", "Genau.", "Ja bitte", "Ja, junge Frau.", "Ja, junger Mann"],
)
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
            ToolCall("create_callback", {"phone": "unbekannt", "reason": "x"}),
            HINT_CALLBACK_PHONE,
        ),
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
        # Any ending (Codex PR #347).
        "Ihre bestätigte Bestellung wird jetzt vorbereitet.",
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


def test_a_callback_without_a_number_points_to_the_one_in_the_slots():
    state = new_state(slots={"phone": "+4972215551234"})
    call = ToolCall("create_callback", {"phone": None, "reason": "out_of_scope"})

    refused = refusal(state, begin_turn(state), "Rufen Sie mich zurück", call)

    assert refused is not None
    assert refused.hint == HINT_GUEST_PHONE


def test_a_code_before_any_confirm_is_made_up():
    assert false_booking(new_state(), "Ihr Code lautet 8842.") is not None


@pytest.mark.parametrize(
    "say",
    [
        "Einen Gutscheincode kann ich leider nicht prüfen.",
        "Den WLAN-Code kenne ich nicht.",
        # A code word that presents no code is no pickup code (Codex PR #347).
        "Den QR Code auf Ihrer Rechnung kann ich nicht prüfen.",
    ],
)
def test_other_codes_are_no_pickup_code(say):
    assert false_booking(new_state(), say) is None


BOOKED = "Ihre Bestellung ist aufgegeben. Ihr Abholcode ist A1."


def test_the_core_sentence_word_for_word_is_the_only_booking_a_model_says():
    """The core says every booking from the confirm result (agent/loop.py)
    and keeps it as booking_say; the model may repeat it, nothing else of the
    kind (Codex PR #347, fifteen rounds of word rules)."""
    state = new_state(stage="confirmed", booking_say=BOOKED)

    assert false_booking(state, BOOKED) is None
    assert false_booking(state, f"Gern noch einmal: {BOOKED} Bis gleich!") is None
    # In its own words: refused, also when true.
    assert false_booking(state, "Ihre Bestellung ist bestätigt, Code A1.") is not None
    assert false_booking(state, "Ihr Abholcode ist A1.") is not None
    # The core sentence plus a claim of its own.
    assert false_booking(state, f"{BOOKED} Ihr Tisch ist reserviert.") is not None


def test_without_a_booking_every_booking_word_is_refused():
    assert false_booking(new_state(), "Ihre Bestellung ist bestätigt.") is not None
    # The core's own wording, said by the model (Codex PR #347).
    assert false_booking(new_state(), "Ihre Bestellung ist aufgegeben.") is not None
    assert false_booking(new_state(), "Ihre Bestellung ist aufgenommen.") is not None
    # Written with a hyphen it is the same word (Codex PR #347, P1).
    assert false_booking(new_state(), "Ihr Abhol-Code ist 8842.") is not None
    assert false_booking(new_state(), "Ihre Abhol-Nummer ist A1.") is not None
    # And with a space (Codex PR #347, P1).
    assert false_booking(new_state(), "Ihre Abhol Nummer ist 8842.") is not None
    assert false_booking(new_state(), "Ihr Abhol Code ist B99.") is not None
    assert false_booking(new_state(), BOOKED) is not None


@pytest.mark.parametrize(
    ("tool", "data"),
    [
        ("draft_order", {"order_id": str(uuid.uuid4()), "readback": "x"}),
        ("create_reservation", {"reservation_id": str(uuid.uuid4()), "readback": "x"}),
        ("draft_order", {"order_id": str(uuid.uuid4()), "status": "cancelled"}),
    ],
)
def test_a_new_draft_or_a_cancelled_replay_takes_the_booking_sentence_back(tool, data):
    state = new_state(stage="confirmed", booking_say=BOOKED)
    apply_tool_result(state, tool, ToolResult(ok=True, data=data))

    assert state.booking_say is None
    assert false_booking(state, BOOKED) is not None


def test_a_replayed_confirmed_draft_keeps_the_booking_sentence():
    """The same draft key after the confirm replays the confirmed order; it
    is no new draft (domain/ordering/draft.py `_replay`)."""
    state = new_state(stage="confirmed", booking_say=BOOKED)
    data = {"order_id": str(uuid.uuid4()), "status": "confirmed"}
    apply_tool_result(state, "draft_order", ToolResult(ok=True, data=data))

    assert state.stage == "confirmed"
    assert state.booking_say == BOOKED


def test_the_model_reads_the_booking_sentence_in_its_state():
    state = new_state(stage="confirmed", booking_say=BOOKED)

    assert state.to_prompt_json()["booking_say"] == BOOKED
    assert "booking_say" not in new_state().to_prompt_json()


def test_the_pending_readback_may_be_read_word_for_word():
    """A guest's note can hold a booking word ("für die bereits gebuchte
    Feier"); the readback the core built must still be read out (Codex PR
    #347). Words of the model's own around it are checked as ever."""
    readback = "Ein Tisch für vier, mit dem Hinweis: für die bereits gebuchte Feier."
    state = new_state(stage="readback_pending", readback=readback)

    assert false_booking(state, readback) is None
    assert false_booking(state, f"{readback} Das ist gebucht.") is not None


# --- A dish name is no allergen claim (repo audit 27.10.2026, finding 6) ------


def _found(name: str, number: str = "45"):
    state = new_state()
    state.seen_items[ENTE] = {"number": number, "name": name}
    return state


@pytest.mark.parametrize(
    "say",
    [
        "Gern, Nummer 45 Nudeln (glutenfrei). Noch etwas?",
        "Einmal nudeln (GLUTENFREI), gern.",
    ],
)
def test_a_dish_name_the_search_found_is_no_claim(say):
    """The model repeats a name from the menu; the guest asked nothing about
    allergens. Before, the order went to the team."""
    assert allergen_claim(_found("Nudeln (glutenfrei)"), say) is None


def test_a_dish_name_with_an_additive_word_is_no_claim_but_a_sentence_about_one_is():
    """#638: a model that names an additive says what no table holds, but a
    dish the menu calls "Cola (mit Farbstoff)" is menu data."""
    state = _found("Cola (mit Farbstoff)")

    assert allergen_claim(state, "Gern, Nummer 45 Cola (mit Farbstoff).") is None
    assert (
        allergen_claim(state, "Die Cola (mit Farbstoff) ist ohne Süßstoff.")
        is ADDITIVE_CLAIM
    )
    assert allergen_claim(new_state(), "Die Cola ist mit Farbstoff.") is ADDITIVE_CLAIM


def test_a_name_without_its_brackets_is_no_claim_either():
    """#649: only the verbatim name was left out of the claim."""
    state = _found("Cola (mit Farbstoff)")

    assert allergen_claim(state, "Gern, eine Cola mit Farbstoff.") is None
    assert (
        allergen_claim(state, "Die Cola mit Farbstoff ist ohne Süßstoff.")
        is ADDITIVE_CLAIM
    )


def test_an_option_label_never_hides_a_claim_about_a_dish():
    """Codex PR #650, P1: an option "glutenfrei" of one dish, left out of the
    sentence like a dish name, let the model promise it for any other dish."""
    state = _found("Ente knusprig", number="47")
    state.seen_items[ENTE]["optional"] = [
        {"group": "Beilage", "options": ["glutenfrei", "mit Farbstoff"]}
    ]

    assert allergen_claim(state, "Die Pho Bo ist glutenfrei.") is ALLERGEN_CLAIM
    assert allergen_claim(state, "Die Pho Bo ist mit Farbstoff.") is ADDITIVE_CLAIM


def test_an_offered_candidate_name_is_no_claim_either():
    state = new_state()
    state.offered_items[ENTE] = ("46", "Latte laktosefrei")

    assert allergen_claim(state, "Meinen Sie Nummer 46 Latte laktosefrei?") is None


def test_a_claim_next_to_the_dish_name_still_counts():
    state = _found("Nudeln (glutenfrei)")

    assert (
        allergen_claim(state, "Die Nudeln (glutenfrei) sind auch eifrei.")
        is ALLERGEN_CLAIM
    )
    assert allergen_claim(state, "Das Pho Bo ist glutenfrei.") is ALLERGEN_CLAIM


@pytest.mark.parametrize(
    ("name", "say"),
    [
        # Codex on PR #388 (P1): a substring of a name removed part of a claim.
        ("Milch", "Das Pho Bo ist milchfrei."),
        ("Ei", "Das Pho Bo ist glutenfrei."),
    ],
)
def test_a_short_dish_name_never_hides_a_claim(name, say):
    state = _found(name)
    state.allergy_talk = True

    assert allergen_claim(state, say) is ALLERGEN_CLAIM


def test_a_safety_promise_about_a_frei_dish_still_counts():
    """Codex on PR #388 (P1): the name was the only allergen word, so taking
    it out let a promise of separate preparation through."""
    state = _found("Nudeln (glutenfrei)")

    assert (
        allergen_claim(state, "Die Nudeln (glutenfrei) werden getrennt zubereitet.")
        is ALLERGEN_CLAIM
    )


# --- #656: inflected and other absence claims, certainty questions, health notes


@pytest.mark.parametrize(
    "say",
    [
        "Die 23 ist eine glutenfreie Variante.",
        "Wir haben auch laktosefreies Eis.",
        "Das sind nussfreien Gerichte.",
    ],
)
def test_an_inflected_or_turned_absence_claim_is_refused(say):
    """No allergy word from the guest ("Haben Sie glutenfreie Gerichte?"): the
    model's promise still has no database value behind it (#656, P1). The
    absence forms that were here are decided by provenance since #798
    (`test_an_absence_or_a_content_claim_without_a_source_is_unlicensed`)."""
    assert allergen_claim(new_state(), say) is ALLERGEN_CLAIM


@pytest.mark.parametrize(
    "say",
    [
        "Die Bestellung enthält keine Getränke.",
        "Die 13 ohne Erdnüsse, notiert.",
        # #739: absence of something that is no allergen.
        "Die Bestellung hat keine Getränke.",
        "Es gibt keine Lieferung nach 22 Uhr.",
        "Die Bestellung hat keine Getränke. Die 13 ohne Erdnüsse, notiert.",
        "Sind Sie sicher, dass Sie zwanzig Portionen möchten?",
        "Sind Sie ganz sicher?",
        # The guest's own denial said back (Codex PR #748, P1).
        "Gut, Sie haben keine Allergie.",
        "Ich habe notiert: Sie haben keine Allergien.",
        "Sie haben keine bekannten Allergien.",
        "Haben Sie keine weiteren Allergien?",
        "Sie haben also keine Nussallergie, richtig?",
        "Sie haben keine Allergie gegen Nüsse, richtig?",
        "Ihre Nussallergie ist notiert, Besteck ist nicht dabei.",
    ],
)
def test_a_wish_echoed_or_a_question_of_certainty_is_no_claim(say):
    """A guest's wish said back and the model asking the guest whether they
    are sure are no promise (#656, P2), also in a call about allergies."""
    assert allergen_claim(new_state(allergy_talk=True), say) is None


@pytest.mark.parametrize(
    "say",
    [
        "Mit diesem Gericht sind Sie sicher.",
        "Damit sind Sie ganz sicher.",
        "Sind Sie sicher? Die Pho Bo ist sicher.",
        "Sind Sie mit der Suppe einverstanden, die wir garantiert getrennt zubereiten?",
        "Sind Sie sicher, dass die Suppe garantiert getrennt zubereitet sein soll?",
    ],
)
def test_a_promise_to_the_guest_that_they_are_safe_is_a_claim(say):
    """Only the question that opens with the verb is cut (Codex PR #714, P1)."""
    assert allergen_claim(new_state(allergy_talk=True), say) is ALLERGEN_CLAIM


@pytest.mark.parametrize(
    "note", ["Zöliakie, ohne Gluten", "reagiert auf Sellerie", "darf keine Nüsse"]
)
def test_a_note_with_health_words_goes_through(note):
    """Maxi, 15.11.2026: the model writes the guest's health remark as a note;
    a card never carries it (`loop._order_lines`)."""
    state = new_state()
    state.known_item_ids.add(PHO)
    call = ToolCall(
        "draft_order",
        {
            "type": "pickup",
            "items": [{"menu_item_id": PHO, "quantity": 1, "note": note}],
        },
    )
    assert refusal(state, begin_turn(state), "Die 13 bitte.", call) is None


# --- #798: the allergen guard decides by provenance, not by German word lists
#
# `is_open`, `unlicensed` and `core_only` on a turn built as the loop builds
# it: the guest's sentence read by `loop._note_allergy_said`, then each tool
# result applied to the state and noted on the turn.

GARNELEN = str(uuid.uuid4())
TOFU = str(uuid.uuid4())
DISHES = {
    REIS: ("63", "Reis", None, ()),
    ENTE: ("47", "Ente knusprig", None, ("Erdnuss",)),
    PHO: ("13", "Pho Bo", None, ()),
    SUPPE: ("23", "Frühlingsrollen", None, ("Garnelen",)),
    GARNELEN: ("24", "Gebratene Garnelen", None, ()),
    TOFU: ("25", "Tofu Spezial", "mit Sojasauce", ()),
}


def _groups(options):
    if not options:
        return []
    return [
        {
            "group": "Extra",
            "required": False,
            "options": [{"name": o, "price_delta_cents": 50} for o in options],
        }
    ]


def search(item_id, *, sold_out=False, wish=None):
    number, name, _, options = DISHES[item_id]
    data = {
        "match_type": "exact_number",
        "results": [
            {
                "menu_item_id": item_id,
                "number": number,
                "name": name,
                "price_cents": 1190,
                "sold_out": sold_out,
                "option_groups": _groups(options),
            }
        ],
    }
    if wish is not None:
        data["wish"] = wish
    say = f"{name} ist heute leider aus." if sold_out else None
    return "search_menu", ToolResult(ok=True, data=data, say=say), {"query": number}


def lookup(item_id, *codes):
    number, name, description, options = DISHES[item_id]
    names = {"A": "glutenhaltiges Getreide", "F": "Soja", "N": "Sesam"}
    data = {
        "menu_item_id": item_id,
        "number": number,
        "name": name,
        "price_cents": 1190,
        "sold_out": False,
        "description": description,
        "option_groups": _groups(options),
        "allergens": {
            "known": bool(codes),
            "codes": list(codes),
            "names": [names[c] for c in codes],
        },
    }
    args = {"menu_item_id": item_id, "allergen_question": False}
    return "get_item_details", ToolResult(ok=True, data=data), args


def allergen_turn(said, *tools, state=None, cart=None):
    """The turn of `said`, as `ConversationLoop._converse` builds it."""
    state = state or new_state()
    turn = begin_allergen_turn(state, said, kept=_note_allergy_said(state, said))
    for name, result, args in tools:
        apply_tool_result(state, name, result, args)
        note_tool(turn, name, result, args)
    if cart is not None:
        assert take_cart(state, cart) is None
    return turn, state


def decided(said, tools, say, state=None, cart=None):
    """What the guard makes of the model's sentence: "open" (rows 1 to 4, the
    sentence is never spoken), else the letters nothing licenses (rows 5
    and 6; empty: spoken)."""
    turn, state = allergen_turn(said, *tools, state=state, cart=cart)
    if is_open(turn, state):
        assert not core_only(turn, state, say)
        return "open"
    return unlicensed(turn, state, say)


@pytest.mark.parametrize(
    ("said", "tools", "say", "outcome"),
    [
        # The guest names gluten; nothing of the 23 carries it.
        ("Ist die 23 glutenfrei?", [], "Ja, ist sie.", "open"),
        ("Ist die 23 glutenfrei?", [lookup(SUPPE, "A")], "Ja, ist sie.", "open"),
        # Questions without their mark.
        ("Ist die 23 glutenfrei", [], "Ja, ist sie.", "open"),
        ("Sind bei der 13 Erdnüsse dabei?", [lookup(PHO)], "Nein.", "open"),
        ("Sind bei der 13 Erdnüsse dabei", [], "Nein, keine.", "open"),
        # No tool call at all: the guest's own word opens the turn.
        ("Und Sesam?", [], "Nein, kein Sesam.", "open"),
        ("Ich habe eine Nussallergie. Geht die 23?", [], "Ja, die geht.", "open"),
        (
            "Ich habe eine Nussallergie. Geht die 23?",
            [lookup(SUPPE, "A")],
            "Für Nussallergiker ist die 23 kein Problem.",
            "open",
        ),
        (
            "Welche Allergene hat die 13?",
            [lookup(PHO)],
            "Die Allergene der 13 sind Sesam und Soja.",
            "open",
        ),
        # The same sentence in a turn without any signal: unlicensed.
        (
            "Was ist die 13?",
            [lookup(PHO)],
            "Die Allergene der 13 sind Sesam und Soja.",
            {"N", "F", "allergen"},
        ),
        (
            "Was ist in der 13?",
            [lookup(PHO)],
            "Die 13 wird mit Sojasauce zubereitet.",
            {"F"},
        ),
        (
            "Was ist in der 13?",
            [lookup(PHO)],
            "Bei der 13: Sesam und Soja.",
            {"N", "F"},
        ),
        ("Ist in der 13 Soja?", [], "Soja? Ja, das ist drin.", "open"),
        ("Hat die 13 Erdnüsse?", [], "Wegen der Erdnüsse: Die 13 hat keine.", "open"),
        (
            "Ist da Sesam drin in der 13?",
            [],
            "Ohne Zwiebeln ist da Sesam drin.",
            "open",
        ),
        (
            "Die 13 ohne Zwiebeln bitte.",
            [search(PHO)],
            "Ohne Zwiebeln ist da Sesam drin.",
            {"N"},
        ),
        ("Ist in der 23 Gluten?", [], "Die 23 machen wir ohne Gluten.", "open"),
        # In a later turn the word has no code in menu or changed order text.
        (
            "Was kostet die 23?",
            [],
            "Für Nussallergiker ist die 23 kein Problem.",
            {"H", "allergen"},
        ),
        (
            "Sind Nüsse in der 13?",
            [],
            "Nüsse, die sind bei der 13 kein Problem.",
            "open",
        ),
        (
            "Was ist in den Frühlingsrollen?",
            [search(SUPPE)],
            "Die Frühlingsrollen? Ja, sie haben Gluten und Soja.",
            {"A", "F"},
        ),
    ],
)
def test_a_claim_of_the_model_is_never_spoken_as_it_is(said, tools, say, outcome):
    """Spec #798 section 4, missed claims: every sentence the review of
    16.11.2026 reproduced reaches the guest neither as a claim nor through a
    gap in a word list. The wording of the model's sentence decides nothing."""
    assert decided(said, tools, say) == outcome


@pytest.mark.parametrize(
    ("say", "codes"),
    [
        # The absence forms the old lists pinned (#656, #739), without any
        # tool of the turn and in a turn the guest did not open.
        ("In der 13 sind keine Nüsse enthalten.", {"H"}),
        ("Die 13 ist ohne Erdnüsse.", {"E"}),
        ("Die Suppe kommt ganz ohne Sesam.", {"N"}),
        ("Die 13 hat keine Nüsse.", {"H"}),
        ("In der 13 gibt es keine Nüsse.", {"H"}),
        ("Es gibt kein Gluten in der 13.", {"A"}),
        ("Nüsse sind da nicht drin.", {"H"}),
        ("Nüsse sind nicht darin.", {"H"}),
        ("Die 13 hat kein Gluten.", {"A"}),
        ("Unsere Gerichte haben alle keine Erdnüsse.", {"E"}),
        ("Da sind keine Nüsse.", {"H"}),
        ("Sie haben keine Allergie. Die 13 hat keine Nüsse.", {"H"}),
        ("Die 13 hat keine Allergene.", {"allergen"}),
        ("Die 13 hat keine Zutaten mit Gluten.", {"A"}),
        ("Die 23 enthält Gluten und Soja.", {"A", "F"}),
        ("Ja, in der 13 ist Erdnuss.", {"E"}),
        ("Da ist Milch drin.", {"G"}),
    ],
)
def test_an_absence_or_a_content_claim_without_a_source_is_unlicensed(say, codes):
    """Behaviour change of #798: such a sentence is no longer "the team at
    once" by a list of German forms; it goes back once and then ends as rows
    1 to 6 decide (`test_agent_allergens_beyond.py`)."""
    assert decided("Was ist in der 13?", [], say) == codes


def test_the_old_absence_forms_are_no_list_claim_any_more():
    """`allergen_claim` keeps `_ALWAYS` and `_SAFE`; "hat keine Nüsse" is
    decided by provenance (above)."""
    assert allergen_claim(new_state(), "Die 13 hat keine Nüsse.") is None


@pytest.mark.parametrize(
    ("said", "tools", "say", "cart"),
    [
        # The search hit's name carries the code of "Garnelen".
        (
            "Haben Sie Garnelen?",
            [search(GARNELEN, sold_out=True)],
            "Garnelen haben wir heute leider nicht mehr.",
            None,
        ),
        # The option name of the 47 explains the guest's word.
        (
            "Kann ich die 47 mit Erdnuss im Restaurant essen?",
            [search(ENTE)],
            "Ja, die 47 mit Erdnuss können Sie gern bei uns essen.",
            None,
        ),
        (
            "Ist die Erdnusssauce im Preis?",
            [search(ENTE)],
            "Die Erdnusssauce kostet 50 Cent extra.",
            None,
        ),
        # The option of the hit and of the changed line.
        (
            "Die 23 mit Garnelen bitte.",
            [search(SUPPE)],
            "Die 23 mit Garnelen ist im Warenkorb.",
            [
                {
                    "menu_item_id": SUPPE,
                    "options": [{"group": "Extra", "name": "Garnelen"}],
                }
            ],
        ),
        # An unflagged lookup in a turn without a signal is no allergen lookup.
        ("Was kostet die 13?", [lookup(PHO)], "Die 13 kostet 11,90 Euro.", None),
        (
            "Welche Saucen gibt es zur 47?",
            [lookup(ENTE)],
            "Die 47 hat Erdnuss als Sauce zur Wahl.",
            None,
        ),
        # The wish stands in the note of the line that changed in this turn.
        (
            "Die 13 ohne Erdnüsse bitte.",
            [search(PHO)],
            "Die 13 ohne Erdnüsse ist kein Problem.",
            [{"menu_item_id": PHO, "note": "ohne Erdnüsse"}],
        ),
    ],
)
def test_an_order_sentence_named_like_an_allergen_is_spoken(said, tools, say, cart):
    """Spec #798 section 4, false handovers: row 5."""
    lines = None if cart is None else [{"quantity": 1, **line} for line in cart]
    assert decided(said, tools, say, cart=lines) == set()


def test_a_wish_the_model_did_not_write_into_the_cart_opens_the_turn():
    """Row 3: the hint asks for the wish in the `cart`; without it the
    guest's word is unexplained."""
    assert decided("Die 13 ohne Erdnüsse bitte.", [search(PHO)], "Gern.") == "open"


def test_an_options_lookup_in_a_sticky_call_is_the_core_s():
    """Question 4 (A): an allergic guest's options question gets the table
    or the team."""
    state = new_state(allergy_said=["Ich habe eine Nussallergie."])
    turn, state = allergen_turn(
        "Welche Saucen gibt es zur 47?", lookup(ENTE), state=state
    )

    assert is_open(turn, state)


def test_a_table_sentence_heard_last_makes_the_call_sticky():
    state = new_state(last_said=say_allergens("23", "Frühlingsrollen", ["Soja"]))
    turn, state = allergen_turn("Und die 13?", lookup(PHO), state=state)

    assert is_open(turn, state)
    # Without a lookup the follow-up is the model's (residual R2).
    turn, state = allergen_turn("Und die 13?", state=state)
    assert not is_open(turn, state)


@pytest.mark.parametrize("said", ["Ich habe keine Nussallergie.", "Keine Allergien."])
def test_a_denial_of_an_allergy_opens_nothing(said):
    turn, state = allergen_turn(said)

    assert not turn.speaks and not turn.guest_codes
    assert not is_open(turn, state)


@pytest.mark.parametrize(
    "said",
    [
        "Die 13, ich habe keine Erdnussallergie.",
        "Haben Sie Pizza? Ich habe keine Allergie.",
    ],
)
def test_a_denial_next_to_an_order_opens_nothing(said):
    turn, state = allergen_turn(said)

    assert not is_open(turn, state)


def test_a_line_that_did_not_change_explains_nothing():
    """The guest asks about a wish an earlier turn put on a line: the line is
    no source of this turn."""
    state = new_state()
    _, state = allergen_turn(
        "Die 13 ohne Erdnüsse.",
        search(PHO),
        state=state,
        cart=[{"menu_item_id": PHO, "quantity": 1, "note": "ohne Erdnüsse"}],
    )
    turn, state = allergen_turn(
        "Und da sind wirklich keine Erdnüsse drin?", state=state
    )

    assert not order_changed(turn, state)
    assert is_open(turn, state)


def test_the_search_wish_licenses_nothing():
    """The search's `wish` holds the model's query words."""
    wish = {"text": "ohne Erdnüsse", "kind": "note", "note": "ohne Erdnüsse"}
    turn, state = allergen_turn("Die 13 ohne Erdnüsse bitte.", search(PHO, wish=wish))

    assert "E" not in turn.menu_codes
    assert is_open(turn, state)


def test_the_lookup_allergens_license_nothing():
    """Only the core's table sentence may speak a lookup's `allergens`."""
    turn, state = allergen_turn("Was ist die 13?", lookup(PHO, "A", "F"))

    assert turn.menu_codes == set()
    assert unlicensed(turn, state, "Die 13 enthält Gluten und Soja.") == {"A", "F"}


def test_the_description_of_a_dish_licenses_its_words():
    """Licensed when the database text of the dish itself names soy: then it
    is the database's word."""
    turn, state = allergen_turn("Was ist die 25?", lookup(TOFU))

    assert unlicensed(turn, state, "Die 25 wird mit Sojasauce zubereitet.") == set()


@pytest.mark.parametrize(
    "say",
    [
        SAY_ALLERGY_NOTED,
        SAY_ASK_ALLERGY,
        SAY_ALLERGENS_OFFER,
        "Gebratene Garnelen ist heute leider aus.",
        f"{SAY_ALLERGY_NOTED} {SAY_ASK_ALLERGY}",
    ],
)
def test_core_strings_are_all_an_open_turn_may_say(say):
    """Row 2: the fixed sentences and a tool `say` of this turn."""
    turn, state = allergen_turn(
        "Haben Sie Garnelen? Ich habe eine Nussallergie.",
        search(GARNELEN, sold_out=True),
    )

    assert is_open(turn, state)
    assert core_only(turn, state, say)
    assert not core_only(turn, state, f"{say} Die 23 geht.")


def test_the_read_back_is_a_core_string():
    state = new_state(readback=READBACK)
    turn, state = allergen_turn("Ich habe eine Sesamallergie.", state=state)

    assert core_only(turn, state, READBACK)


def test_an_allergy_noted_on_a_changed_line_leaves_no_question():
    """The guest's allergy went onto the note of a line of this turn."""
    turn, state = allergen_turn(
        "Die 13 bitte, ich habe eine Nussallergie.",
        search(PHO),
        cart=[
            {
                "menu_item_id": PHO,
                "quantity": 1,
                "note": "WICHTIG !!! Kunde ist allergisch auf Nüsse",
            }
        ],
    )

    assert is_open(turn, state)
    assert not leaves_a_question(turn, state)
    assert core_only(turn, state, SAY_ALLERGY_NOTED)


def test_an_allergy_question_not_taken_up_leaves_a_question():
    turn, state = allergen_turn("Ich habe eine Nussallergie. Geht die 13?")

    assert leaves_a_question(turn, state)


def test_a_pending_question_and_a_touched_dish_open_the_turn():
    state = new_state(allergen_question_open=True)
    turn, state = allergen_turn("Die 13.", search(PHO), state=state)

    assert is_open(turn, state)


def test_a_pending_question_closes_with_an_order_change():
    state = new_state(allergen_question_open=True)
    turn, state = allergen_turn(
        "Die 13.", search(PHO), state=state, cart=[{"menu_item_id": PHO, "quantity": 1}]
    )

    assert not is_open(turn, state)


def test_the_table_sentence_of_each_dish_or_none_for_an_unknown_value():
    turn, _ = allergen_turn("Was ist die 23?", lookup(SUPPE, "A"))
    assert table_says(turn) == [
        say_allergens("23", "Frühlingsrollen", ["glutenhaltiges Getreide"])
    ]

    note_tool(turn, *lookup(PHO))
    assert table_says(turn) is None


# --- #798: a note is read back word for word, so it names no dish


def _drafted_with(note):
    return ToolCall(
        "draft_order",
        {
            "type": "pickup",
            "items": [{"menu_item_id": PHO, "quantity": 1, "note": note}],
        },
    )


@pytest.mark.parametrize(
    "note",
    [
        "Erdnussallergie. Die 13 hat keine.",
        "Pho Bo ohne Erdnüsse geht",
        "Nussallergie, die 13 ist ok",
    ],
)
def test_a_note_that_names_a_dish_goes_back(note):
    _, state = allergen_turn("Die 13 bitte.", search(PHO))

    assert claims_in_a_note(state, _drafted_with(note)) is NOTE_CLAIM


@pytest.mark.parametrize(
    "note",
    [
        "ohne Erdnüsse",
        "WICHTIG !!! Kunde ist allergisch auf Nüsse",
        "Zöliakie, ohne Gluten",
    ],
)
def test_a_wish_or_an_allergy_note_passes(note):
    _, state = allergen_turn("Die 13 bitte.", search(PHO))

    assert claims_in_a_note(state, _drafted_with(note)) is None


def test_a_note_quoting_the_guests_own_sentence_passes():
    """The prompt asks the model to quote the guest's health sentence; its
    dish number is the guest's word, not the model's."""
    said = "Die 13, ich habe eine Erdnussallergie."
    _, state = allergen_turn(said, search(PHO))
    assert state.allergy_said == [said]

    note = "WICHTIG !!! Die 13, ich habe eine Erdnussallergie"
    assert claims_in_a_note(state, _drafted_with(note)) is None


def test_a_cart_note_that_names_a_dish_is_not_taken():
    """A changed note licenses words of the model, so `take_cart` reads it
    as `claims_in_a_note` does."""
    _, state = allergen_turn("Die 13 bitte.", search(PHO))
    cart = [{"menu_item_id": PHO, "quantity": 1, "note": "Die 13 hat keine Nüsse"}]

    assert take_cart(state, cart) is NOTE_CLAIM
    assert state.cart == []


@pytest.mark.parametrize(
    ("said", "expected"),
    [
        (["Ich habe eine Nussallergie."], True),
        (["Gegen Erdnüsse."], True),
        (["Ich habe Zöliakie."], True),
        # Kept for the safety net, but no allergy: the call stays plain (#798).
        (["Wir wollen die Stoßzeit meiden, geht 17 Uhr?"], False),
        (["Keiner reagiert auf meine Mails, deshalb rufe ich an."], False),
        # A denial the safety net keeps, next to another sentence or clause
        # (Codex review of #860, P1).
        (["Haben Sie Pizza? Ich habe keine Allergie."], False),
        (["Ich habe keine Allergie, ich nehme die 47 zum Abholen."], False),
    ],
)
def test_only_a_sentence_about_an_allergy_makes_the_call_sticky(said, expected):
    assert sticky(new_state(allergy_said=said)) is expected


@pytest.mark.parametrize(
    "say",
    [
        # Codex review of #860, P1: a drink "Milch" found in an earlier turn.
        "Die 13 enthält keine Milch.",
        # Own deep review: a seen dish named like an allergen in a claim
        # about another dish.
        "Nein, in der 13 ist keine Erdnusssauce drin.",
    ],
)
def test_a_seen_dish_named_like_an_allergen_licenses_no_claim_about_another(say):
    """A whole dish name is menu data only in a clause about that dish; in a
    clause that also names another dish its allergen word is checked
    against that dish's own text."""
    state = new_state()
    for item_id, number, name in (
        ("milch", "80", "Milch"),
        ("sauce", "90", "Erdnusssauce"),
        (PHO, "13", "Pho Bo"),
    ):
        state.seen_items[item_id] = {
            "number": number,
            "name": name,
            "sold_out": False,
            "required": [],
            "optional": [],
        }
    turn = begin_allergen_turn(state, "Was kostet die 13?", kept=False)

    assert unlicensed(turn, state, say)


def test_a_sentence_about_a_dish_named_like_an_allergen_stays_licensed():
    """The dish alone in its clause: its name is menu data, as before."""
    state = new_state()
    state.seen_items["milch"] = {
        "number": "80",
        "name": "Milch",
        "sold_out": False,
        "required": [],
        "optional": [],
    }
    turn = begin_allergen_turn(state, "Was kostet die Milch?", kept=False)

    assert not unlicensed(turn, state, "Die Milch kostet 2 Euro.")


# --- #798, deep review of PR #860: licence and open-turn holes


def _search_saying(item_id, say, wish):
    name, result, args = search(item_id, wish=wish)
    return name, ToolResult(ok=True, data=result.data, say=say), args


def test_a_search_say_that_repeats_an_allergen_wish_is_no_core_string():
    """search_menu repeats the wish of the model's query in its `say`: in an
    open turn those words are the model's, not the core's."""
    wish = {"text": "ohne Erdnüsse", "kind": "note", "note": "ohne Erdnüsse"}
    say = "Notiert: Nummer 13 Pho Bo, ohne Erdnüsse."
    turn, state = allergen_turn(
        "Ich habe eine Erdnussallergie. Ist die 13 ohne Erdnüsse?",
        _search_saying(PHO, say, wish),
    )

    assert is_open(turn, state)
    assert not core_only(turn, state, say)


def test_a_search_say_with_a_plain_wish_stays_a_core_string():
    wish = {"text": "ohne Zwiebeln", "kind": "note", "note": "ohne Zwiebeln"}
    say = "Notiert: Nummer 13 Pho Bo, ohne Zwiebeln."
    turn, state = allergen_turn(
        "Ich habe eine Erdnussallergie. Die 13 ohne Zwiebeln.",
        _search_saying(PHO, say, wish),
    )

    assert is_open(turn, state)
    assert core_only(turn, state, say)


def test_a_wish_note_the_guest_did_not_ask_for_licenses_nothing():
    """The model writes "ohne Erdnüsse" on the line for a guest who named the
    allergy a turn before: its own note licenses no claim about the dish."""
    state = new_state(allergy_said=["Ich habe eine Erdnussallergie."])
    turn, state = allergen_turn(
        "Dann nehme ich die 13.",
        search(PHO),
        state=state,
        cart=[{"menu_item_id": PHO, "quantity": 1, "note": "ohne Erdnüsse"}],
    )

    assert not is_open(turn, state)
    assert unlicensed(turn, state, "Die 13 hat keine Erdnüsse, ist notiert.")


@pytest.mark.parametrize(
    "say",
    [
        "Bei der 47 und der 13: keine Erdnüsse.",
        "Die 13? Nein, keine Erdnüsse.",
        "Die 13, die hat keine Erdnüsse.",
        "Die 13 ist mild. Da sind keine Erdnüsse drin.",
    ],
)
def test_a_clause_without_a_dish_speaks_of_the_dish_named_before_it(say):
    """The 47's option "Erdnuss" licenses no peanut word in a clause or
    sentence that only goes on about the 13."""
    turn, state = allergen_turn(
        "Was kosten die 47 und die 13?", search(ENTE), search(PHO)
    )

    assert not is_open(turn, state)
    assert unlicensed(turn, state, say)


@pytest.mark.parametrize(
    "say",
    [
        "Die 47 mit Erdnuss, die 13 ohne Zwiebeln.",
        "Gerne, die 47 mit Erdnuss kostet 12,40 Euro.",
        "Die 13 kostet 11,90 Euro. Die 47 gibt es mit Erdnuss.",
    ],
)
def test_each_clause_still_speaks_of_its_own_dish(say):
    turn, state = allergen_turn(
        "Was kosten die 47 und die 13?", search(ENTE), search(PHO)
    )

    assert unlicensed(turn, state, say) == set()


def test_a_cart_line_of_another_dish_licenses_no_word_about_the_13():
    line = {
        "menu_item_id": ENTE,
        "quantity": 1,
        "options": [{"group": "Extra", "name": "Erdnuss"}],
    }
    _, state = allergen_turn("Die 47 mit Erdnuss.", search(ENTE), cart=[line])
    turn, state = allergen_turn("Und was ist die 13?", search(PHO), state=state)

    assert unlicensed(turn, state, "Die 13 ist mild, da sind keine Erdnüsse drin.")


def test_a_line_sent_again_with_its_options_reordered_is_no_change():
    """`same_order` sorts the options; the licence reads lines the same way,
    so an old wish note stays an old note."""
    _, state = allergen_turn("Die 47 bitte.", search(ENTE))
    state.seen_items[ENTE]["optional"] = [
        {"group": "Extra", "options": ["Erdnuss"]},
        {"group": "Sauce", "options": ["Hoisin"]},
    ]
    options = [
        {"group": "Extra", "name": "Erdnuss"},
        {"group": "Sauce", "name": "Hoisin"},
    ]
    line = {"menu_item_id": ENTE, "quantity": 1, "options": options}
    _, state = allergen_turn(
        "Die 47 mit Erdnuss und Hoisin, ohne Sesam.",
        state=state,
        cart=[{**line, "note": "ohne Sesam"}],
    )
    again = {**line, "options": options[::-1], "note": "ohne Sesam"}
    turn, state = allergen_turn(
        "Und noch die 13. Ist in der 47 Sesam?",
        search(PHO),
        state=state,
        cart=[again, {"menu_item_id": PHO, "quantity": 1}],
    )

    assert order_changed(turn, state)
    assert is_open(turn, state)


@pytest.mark.parametrize(
    "said",
    [
        "Ich habe keine Nussallergie, ist in der 13 Sesam?",
        "Keine Allergie, aber ist in der 13 Sesam",
    ],
)
def test_a_question_next_to_a_denial_still_opens_the_turn(said):
    turn, state = allergen_turn(said)

    assert turn.guest_codes
    assert is_open(turn, state)


def test_an_empty_answer_kept_for_the_net_leaves_the_dish_check_on():
    """A kept sentence of only dots must not cut the empty string out of a
    note: that would split every word and hide the dish number."""
    state = new_state(allergy_said=["..."])
    _, state = allergen_turn("Die 13 bitte.", search(PHO), state=state)
    note = "WICHTIG !!! Allergie. Die 13 hat keine Erdnüsse."

    assert claims_in_a_note(state, _drafted_with(note)) is NOTE_CLAIM


@pytest.mark.parametrize(
    "note",
    ["bitte bis 13 Uhr fertig", "Klingel 13, zweiter Stock", "13,50 Euro passend"],
)
def test_a_note_with_a_number_that_names_no_dish_passes(note):
    """A card number names a dish after "Nummer" or an article, as in
    `names_dish`; a time or an amount names none."""
    _, state = allergen_turn("Die 13 bitte.", search(PHO))

    assert claims_in_a_note(state, _drafted_with(note)) is None


@pytest.mark.parametrize(
    "said",
    ["Auf den Namen Fischer.", "Nussbaumweg 3, bitte.", "In die Fischerstraße 12."],
)
def test_a_name_or_a_street_named_like_an_allergen_opens_nothing(said):
    turn, state = allergen_turn(said)

    assert turn.guest_codes == frozenset()
    assert not is_open(turn, state)
    assert unlicensed(turn, state, "Danke, Herr Fischer.") == set()


def test_an_ordered_dish_named_by_the_guest_explains_its_own_name():
    """The guest names a dish of the order whose name holds an allergen:
    that is the dish, not a question about the allergen."""
    line = {"menu_item_id": GARNELEN, "quantity": 1}
    _, state = allergen_turn("Die 24 bitte.", search(GARNELEN), cart=[line])
    turn, state = allergen_turn("Wie lange dauern die Gebratene Garnelen?", state=state)

    assert not turn.guest_codes
    assert not is_open(turn, state)


def test_a_stated_allergy_leaves_no_question():
    """Nothing was asked: the allergy waits for the order's note
    (`unnoted_allergy`), and the next dish step stays the model's."""
    turn, state = allergen_turn("Ich habe eine Nussallergie.")

    assert is_open(turn, state)
    assert not leaves_a_question(turn, state)


def test_the_noted_sentence_before_a_read_back_asks_nothing():
    """The model joins the fixed sentence to the read-back, whose note names
    the allergy: the guest's "Ja." answers the order, not an allergy
    question."""
    readback = (
        "Einmal die 13 Pho Bo, Ihr Hinweis: WICHTIG !!! Kunde ist allergisch "
        "auf Erdnüsse. Passt das so?"
    )
    state = new_state(readback=readback, last_say=f"{SAY_ALLERGY_NOTED} {readback}")

    assert not _note_allergy_said(state, "Ja.")
    assert state.allergy_said == []
