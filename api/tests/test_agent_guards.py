"""agent/guards.py: the core holds hard rules 1 to 3, whatever the model returns.

No database: the guards read the conversation state and the guest's sentence.
The way through the loop is in test_agent_loop.py.
"""

import uuid

import pytest

from api.agent.dispatch import ToolResult
from api.agent.guards import begin_turn, refusal
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


def hit(item_id):
    return {"menu_item_id": item_id, "number": "47", "name": "Ente", "sold_out": False}


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

    answer = begin_turn(state)

    assert refusal(state, answer, "die knusprige", draft(PHO)) is None


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
