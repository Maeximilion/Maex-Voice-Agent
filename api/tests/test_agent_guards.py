"""agent/guards.py: the core holds hard rules 1 to 3, whatever the model returns.

No database: the guards read the conversation state and the guest's sentence.
The way through the loop is in test_agent_loop.py.
"""

import uuid

import pytest

from api.agent.consent import is_yes
from api.agent.dispatch import ToolResult
from api.agent.guards import begin_turn, note_said, refusal
from api.agent.llm import ToolCall
from api.agent.state import ConversationState, apply_state_patch, apply_tool_result

ENTE = str(uuid.uuid4())
PHO = str(uuid.uuid4())
SUPPE = str(uuid.uuid4())


def new_state(**fields):
    return ConversationState(call_id=uuid.uuid4(), tenant_id=uuid.uuid4(), **fields)


def read_back(entity="reservation"):
    """A state in which a draft was read to the guest in an earlier turn."""
    draft_id = uuid.uuid4()
    state = new_state(stage="readback_pending", **{f"{entity}_id": draft_id})
    return state, str(draft_id)


def confirm(entity_id, entity="reservation"):
    return ToolCall("confirm", {"entity": entity, "entity_id": entity_id})


CARD = {
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
    state = new_state()

    assert refusal(state, begin_turn(state), "Ja", confirm(str(uuid.uuid4())))


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
    ],
)
def test_yes_is_a_sentence_that_is_nothing_but_the_assent(text, yes):
    assert is_yes(text) is yes


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
    state = new_state()
    searched(state, {"match_type": "exact_number", "results": [hit(ENTE)]})

    assert ENTE not in str(state.to_prompt_json())


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
    state = new_state()

    assert refusal(state, begin_turn(state), "Hallo", call) is None


# --- Rule 1: a model writes only what a guest can say into the slots ---


def test_state_patch_keeps_what_a_guest_names():
    state = new_state()
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
