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
  or from candidates of an unclear match that the guest heard by name. The
  same holds for the order a model keeps across turns (`cart`, `take_cart`):
  a dish nobody searched for never gets into the state, where the next turn
  would read it as found.
- Rule 1 sits in `state.apply_state_patch`: a model writes only guest details.
  Of an order line it writes dish, quantity, options and note; number, name
  and the options still open are added by the state from the search result.
"""

import re
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from api.agent.consent import is_yes
from api.agent.dispatch import ToolResult, validation_hint
from api.agent.llm import ToolCall
from api.agent.state import (
    ConversationState,
    cart_lines,
    order_corrected,
    replace_cart,
)

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
HINT_CART_ITEM = (
    "Abgelehnt: `cart` nennt eine menu_item_id, die nicht aus einem eindeutigen "
    "Treffer von search_menu in diesem Anruf stammt. Erst suchen; bei mehreren "
    "Treffern den Gast wählen lassen. Die Bestellung im Zustand ist unverändert."
)
HINT_CART_FORM = (
    "Abgelehnt: `cart` ist keine Liste von Positionen wie `items` von "
    "draft_order (menu_item_id, quantity, optional options und note). Die "
    "Bestellung im Zustand ist unverändert."
)


@dataclass(frozen=True)
class TurnStart:
    """What held when the guest began to speak."""

    # The draft that was read back before this turn, if one waits for its yes.
    readback_id: str | None


def begin_turn(state: ConversationState) -> TurnStart:
    """Call once per guest turn, before the model."""
    return TurnStart(readback_id=_readback_id(state))


def note_said(state: ConversationState, say: str) -> None:
    """Call with the sentence that ends a turn. A candidate of an unclear
    search may enter an order only once the guest heard it: by its full name
    or as "Nummer <card number>". A model that does not pass the offer on and
    asks for something else makes no candidate usable (Codex PR #222, P1).

    A candidate that was not named yet stays offered: the offer may come a
    turn later, as when an open allergy question goes first and the two
    questions are never asked at once (Codex PR #139)."""
    heard = say.lower()
    named = {
        item_id
        for item_id, (number, name) in state.offered_items.items()
        if _named(heard, number.lower(), name.lower())
    }
    state.known_item_ids |= named
    state.offered_items = {
        item_id: label
        for item_id, label in state.offered_items.items()
        if item_id not in named
    }


def _named(heard: str, number: str, name: str) -> bool:
    """A bare number is no card number: "um 13 Uhr" or "13,47 Euro" name no
    dish. Half a name names none either, and a name counts only as whole
    words: "Reis" is not named by "Preis" or "Reisnudeln" (Codex PR #222)."""
    if name and re.search(rf"(?<!\w){re.escape(name)}(?!\w)", heard):
        return True
    return bool(number) and (
        re.search(rf"\b(nummer|nr\.?)\s*{re.escape(number)}(?!\w)", heard) is not None
    )


def take_cart(state: ConversationState, cart: Any) -> ToolResult | None:
    """The order a model wants to keep for the next turn (`LLMTurn.cart`).
    Taken as a whole or not at all: the order in the state is always one the
    core accepted in full. None when it was taken; otherwise the failed result
    the model gets instead of having its answer carried out.

    A refused order is still an attempt to change the order: a draft that
    waits for its yes is dropped as after a failed correction (`state.py`,
    `_supersedes`), so a yes cannot confirm what the guest just changed."""
    try:
        lines = cart_lines(cart)
    except ValidationError as exc:
        # Field and problem, never the value: a note can hold what a guest said.
        refused = ToolResult(
            ok=False,
            error_code="invalid_input",
            hint=f"{HINT_CART_FORM} {validation_hint(exc)}",
        )
    else:
        if all(line["menu_item_id"] in state.known_item_ids for line in lines):
            replace_cart(state, lines)
            return None
        refused = _CART_ITEM
    order_corrected(state)
    return refused


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
_CART_ITEM = ToolResult(ok=False, error_code="invalid_input", hint=HINT_CART_ITEM)


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
