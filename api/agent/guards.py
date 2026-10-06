"""What the core enforces itself, whatever the model returns (CLAUDE.md §2).

The system prompt asks the model to follow the hard rules. A prompt is no
enforcement: a real model can return `confirm` after a "Nein", or put the first
of several candidates into an order. The loop therefore asks here before it
dispatches a tool call, and a refused call goes back to the model as a failed
tool result with a `hint`. It costs one of the turn's tool hops, so a model
that insists ends in the handoff to the team, not in a booking.

- Rule 3: `confirm` only when the guest's current sentence is an explicit yes
  to the draft that was read back before this turn.
- Rule 2: `draft_order` only with dishes from a clear match of `search_menu`,
  or from candidates the guest has been asked about.
- Rule 1 sits in `state.apply_state_patch`: a model writes only guest details.
"""

from dataclasses import dataclass

from api.agent.consent import is_yes
from api.agent.dispatch import ToolResult
from api.agent.llm import ToolCall
from api.agent.state import ConversationState

# For the model, part of what it reads next to the prompt: German like the prompt.
HINT_CONFIRM = (
    "Abgelehnt: kein ausdrückliches Ja des Gastes auf den zuletzt vorgelesenen "
    "Entwurf. Erst den readback vorlesen, dann auf ein klares Ja warten."
)
HINT_ITEM = (
    "Abgelehnt: eine menu_item_id stammt nicht aus einem eindeutigen Treffer von "
    "search_menu in diesem Anruf. Erst suchen; bei mehreren Treffern den Gast "
    "wählen lassen."
)


@dataclass(frozen=True)
class TurnStart:
    """What held when the guest began to speak."""

    # The draft that was read back before this turn, if one waits for its yes.
    readback_id: str | None


def begin_turn(state: ConversationState) -> TurnStart:
    """Call once per guest turn, before the model. The guest now answers what
    was offered in the turn before, so those candidates may enter an order."""
    state.known_item_ids |= state.offered_item_ids
    state.offered_item_ids = set()
    return TurnStart(readback_id=_readback_id(state))


def refusal(
    state: ConversationState, start: TurnStart, user_text: str, call: ToolCall
) -> ToolResult | None:
    """None lets the call through to `dispatch`; otherwise the failed result
    the model gets instead."""
    if call.name == "confirm":
        return None if _may_confirm(state, start, user_text, call) else _CONFIRM
    if call.name == "draft_order" and not _all_known(state, call):
        return _ITEM
    return None


_CONFIRM = ToolResult(ok=False, error_code="conflict", hint=HINT_CONFIRM)
_ITEM = ToolResult(ok=False, error_code="invalid_input", hint=HINT_ITEM)


def _readback_id(state: ConversationState) -> str | None:
    if state.stage != "readback_pending":
        return None
    draft = state.order_id or state.reservation_id
    return str(draft) if draft else None


def _may_confirm(
    state: ConversationState, start: TurnStart, user_text: str, call: ToolCall
) -> bool:
    """The draft must be the one read back before this turn and still the
    pending one: a draft built or replaced inside the turn was never read to
    the guest, so a yes in the same sentence is no consent to it."""
    return (
        start.readback_id is not None
        and _readback_id(state) == start.readback_id
        and str(call.args.get("entity_id")) == start.readback_id
        and is_yes(user_text)
    )


def _all_known(state: ConversationState, call: ToolCall) -> bool:
    """Which candidate the guest's answer means stays with the model and is
    measured by the evals; here only an id nobody was asked about is stopped.
    Arguments that are no list of items are left to the validation in dispatch."""
    items = call.args.get("items")
    if not isinstance(items, list):
        return True
    return all(
        str(item.get("menu_item_id")) in state.known_item_ids
        for item in items
        if isinstance(item, dict)
    )
