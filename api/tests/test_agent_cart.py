"""The order in the conversation state (T-2.4): what a model may keep across turns.

A real model gets the compact state and one input per turn, never a transcript.
A dish found in one turn is gone in the next unless the state carries it. The
model writes the order (`LLMTurn.cart`), the core decides whether it takes it
(`guards.take_cart`, CLAUDE.md §2 rules 1 to 3).

First part without a database, on state and guards. Second part through the
loop with a menu, driven by a model that reads nothing but the state.
"""

import json
import uuid

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from api.agent.dispatch import ToolResult
from api.agent.guards import (
    HINT_CART_FORM,
    HINT_CART_ITEM,
    HINT_CART_SOLD_OUT,
    begin_turn,
    note_said,
    refusal,
    take_cart,
)
from api.agent.llm import (
    ORDER_FORMAT,
    OUTPUT_FORMAT,
    ChatCompletionsLLM,
    FakeLLM,
    LLMTurn,
    ToolCall,
    parse_turn,
)
from api.agent.loop import ConversationLoop
from api.agent.state import (
    GUEST_SLOTS,
    ConversationState,
    apply_tool_result,
    initial_state,
)
from api.models import Call, MenuItem, Order, OrderItem
from api.schemas.orders import MAX_ITEMS
from api.tests.test_domain_draft_order import NOW, _call, _tenant

ENTE = str(uuid.uuid4())
PHO = str(uuid.uuid4())
SUPPE = str(uuid.uuid4())

CARD = {
    ENTE: ("47", "Ente knusprig"),
    PHO: ("13", "Pho Bo"),
    SUPPE: ("12", "Wan-Tan-Suppe"),
}
FLEISCH = {
    "group": "Fleisch",
    "required": True,
    "options": [
        {"name": "Ente", "price_delta_cents": 0, "default": True},
        {"name": "Huhn", "price_delta_cents": -100, "default": False},
    ],
}
SAUCE = {
    "group": "Sauce",
    "required": False,
    "options": [{"name": "Erdnuss", "price_delta_cents": 50, "default": False}],
}


def new_state(**fields):
    return ConversationState(call_id=uuid.uuid4(), tenant_id=uuid.uuid4(), **fields)


def hit(item_id, **fields):
    number, name = CARD[item_id]
    return {
        "menu_item_id": item_id,
        "number": number,
        "name": name,
        "price_cents": 1550,
        "sold_out": False,
        **fields,
    }


def searched(state, *item_ids, match_type="exact_number", **fields):
    data = {"match_type": match_type, "results": [hit(i, **fields) for i in item_ids]}
    apply_tool_result(state, "search_menu", ToolResult(ok=True, data=data))


def line(item_id, quantity=1, **fields):
    return {"menu_item_id": item_id, "quantity": quantity, **fields}


def order_read_back(state, *lines):
    """A state in which an order with these lines was read to the guest."""
    order_id = uuid.uuid4()
    apply_tool_result(
        state,
        "draft_order",
        ToolResult(ok=True, data={"order_id": str(order_id)}),
        {"type": "pickup", "items": list(lines)},
    )
    assert state.stage == "readback_pending"
    return str(order_id)


# --- Rule 2: only dishes a search delivered get into the order -----------------


def test_cart_with_a_dish_from_a_clear_match_is_taken():
    state = new_state()
    searched(state, ENTE)

    assert take_cart(state, [line(ENTE, 2)]) is None

    assert state.cart == [line(ENTE, 2)]
    assert state.to_prompt_json()["cart"] == [
        {**line(ENTE, 2), "number": "47", "name": "Ente knusprig"}
    ]


def test_cart_with_an_id_no_search_delivered_is_refused():
    state = new_state()
    searched(state, ENTE)
    take_cart(state, [line(ENTE)])

    refused = take_cart(state, [line(ENTE), line(SUPPE)])

    assert refused is not None
    assert refused.ok is False
    assert refused.hint == HINT_CART_ITEM
    # Whole or not at all: the line that was fine is not taken either.
    assert state.cart == [line(ENTE)]


def test_cart_with_a_candidate_the_guest_never_heard_is_refused():
    """The model picks from an unclear result itself. The dish is usable once
    the guest was asked by name, as for draft_order."""
    state = new_state()
    searched(state, ENTE, PHO, match_type="ambiguous")

    assert take_cart(state, [line(ENTE)]) is not None
    assert state.cart == []

    note_said(state, "Meinen Sie Nummer 47 Ente knusprig oder Nummer 13 Pho Bo?")
    assert take_cart(state, [line(ENTE)]) is None


@pytest.mark.parametrize(
    "cart",
    [
        "zweimal die 47",
        {"menu_item_id": ENTE, "quantity": 1},
        [{"quantity": 2}],
        [line(ENTE, 0)],
        [line(ENTE, "viele")],
        [line("die-47")],
        [line(ENTE)] * (MAX_ITEMS + 1),
    ],
)
def test_cart_in_another_form_than_order_items_is_refused(cart):
    state = new_state()
    searched(state, ENTE)

    refused = take_cart(state, cart)

    assert refused is not None
    assert refused.error_code == "invalid_input"
    assert refused.hint.startswith(HINT_CART_FORM)
    assert state.cart == []


def test_refusal_names_the_field_but_never_what_the_guest_said():
    state = new_state()
    searched(state, ENTE)
    note = "Allergie gegen Erdnuss, Frau Müller " * 10

    refused = take_cart(state, [line(ENTE, note=note)])

    assert "note" in refused.hint
    assert "Müller" not in refused.hint


# --- Rule 1: what the model reads about a dish comes from the search -----------


def test_labels_and_prices_a_model_writes_are_not_kept():
    state = new_state()
    searched(state, ENTE)

    take_cart(
        state,
        [
            {
                **line(ENTE),
                "name": "Ente gratis",
                "number": "1",
                "price_cents": 0,
                "open": [],
            }
        ],
    )

    assert state.cart == [line(ENTE)]
    shown = state.to_prompt_json()["cart"][0]
    assert shown["name"] == "Ente knusprig"
    assert shown["number"] == "47"
    assert "price_cents" not in shown


def test_same_order_in_another_spelling_is_the_same_cart():
    state = new_state()
    searched(state, ENTE)
    take_cart(state, [line(ENTE, 2)])

    take_cart(state, [line(ENTE.upper(), "2", options=[], note="  ")])

    assert state.cart == [line(ENTE, 2)]


def test_open_names_the_mandatory_groups_without_a_choice():
    state = new_state()
    searched(state, ENTE, option_groups=[FLEISCH, SAUCE])

    take_cart(state, [line(ENTE)])
    # Mandatory only, names only: no price goes back into the prompt.
    assert state.to_prompt_json()["cart"][0]["open"] == [
        {"group": "Fleisch", "options": ["Ente", "Huhn"]}
    ]

    take_cart(state, [line(ENTE, options=[{"group": "fleisch", "name": "Huhn"}])])
    assert "open" not in state.to_prompt_json()["cart"][0]


# --- Only the order goes back to the model -------------------------------------


def test_dish_that_is_not_in_the_order_never_goes_back_to_the_model():
    """Seen on a real model with a first version that also showed what a
    search had found: the candidate the guest had not chosen stayed in sight,
    and two turns later the model put it into the order. A dish reaches the
    next turn only as a line of the order: neither the other candidate of an
    answered question nor a clear hit that was not taken up."""
    state = new_state()
    searched(state, SUPPE)
    searched(state, ENTE, PHO, match_type="ambiguous")
    note_said(state, "Meinen Sie Nummer 47 Ente knusprig oder Nummer 13 Pho Bo?")

    take_cart(state, [line(PHO)])

    shown = str(state.to_prompt_json())
    assert PHO in shown
    assert ENTE not in shown
    assert "Ente knusprig" not in shown
    assert SUPPE not in shown


# --- Rule 3: a changed order is no longer the one that was read back ----------


def test_draft_order_makes_its_items_the_cart():
    """Also when the model drafted without writing `cart` first: a correction
    after the readback starts from the lines that were read out."""
    state = new_state()
    searched(state, ENTE)

    order_read_back(state, line(ENTE, 2))

    assert state.cart == [line(ENTE, 2)]


def test_changed_cart_after_the_readback_drops_the_draft():
    state = new_state()
    searched(state, ENTE)
    order_id = order_read_back(state, line(ENTE, 2))
    start = begin_turn(state)

    take_cart(state, [line(ENTE, 3)])

    assert state.stage == "collecting"
    assert state.order_id is None
    confirm = ToolCall("confirm", {"entity": "order", "entity_id": order_id})
    assert refusal(state, start, "Ja", confirm) is not None


def test_same_cart_after_the_readback_keeps_the_draft():
    """A model that repeats the order with its answer changes nothing."""
    state = new_state()
    searched(state, ENTE)
    order_id = order_read_back(state, line(ENTE, 2))
    start = begin_turn(state)

    take_cart(state, [{**line(ENTE, 2), "number": "47", "name": "Ente knusprig"}])

    assert state.stage == "readback_pending"
    confirm = ToolCall("confirm", {"entity": "order", "entity_id": order_id})
    assert refusal(state, start, "Ja", confirm) is None


def test_same_cart_in_another_order_keeps_the_draft():
    """The sequence of lines and of options is no part of an order (Codex PR
    #237, P2): a model that repeats the order the other way round with its
    `confirm` has changed nothing, and the yes must not cost a second
    readback. The state keeps the sequence that was read out."""
    state = new_state()
    searched(state, ENTE)
    searched(state, PHO)
    sauces = [
        {"group": "Sauce", "name": "Erdnuss"},
        {"group": "Fleisch", "name": "Huhn"},
    ]
    read_out = [line(ENTE, options=sauces), line(PHO, 2)]
    order_id = order_read_back(state, *read_out)
    start = begin_turn(state)

    take_cart(state, [line(PHO, 2), line(ENTE, options=sauces[::-1])])

    assert state.stage == "readback_pending"
    assert state.cart == read_out
    confirm = ToolCall("confirm", {"entity": "order", "entity_id": order_id})
    assert refusal(state, start, "Ja", confirm) is None


def test_same_dishes_in_other_quantities_are_a_changed_cart():
    state = new_state()
    searched(state, ENTE)
    searched(state, PHO)
    order_read_back(state, line(ENTE, 2), line(PHO))

    take_cart(state, [line(PHO, 2), line(ENTE)])

    assert state.stage == "collecting"


def test_sold_out_dish_does_not_get_into_the_cart():
    """A clear hit that is sold out is still a hit, and the format tells the
    model to take up what was found (Codex PR #237, P2). In the state it would
    stand like any other line, and the guest would hear of it only when
    draft_order refuses at the very end."""
    state = new_state()
    searched(state, PHO)
    searched(state, ENTE, sold_out=True)

    refused = take_cart(state, [line(PHO), line(ENTE)])

    assert refused is not None
    assert refused.hint == HINT_CART_SOLD_OUT
    assert state.cart == []

    # Back on the menu by the next search: usable again.
    searched(state, ENTE)
    assert take_cart(state, [line(PHO), line(ENTE)]) is None


@pytest.mark.parametrize("cart", [False, 0, 7, "zwei"])
def test_cart_that_is_no_list_is_refused_not_ignored(cart):
    """`false` and `0` are no field left blank (Codex PR #237, P1). Read as
    "nothing said" they let the draft that was read back wait on, while the
    model may have meant "the order is empty now". Refused, the draft is
    dropped like after any failed correction."""
    turn = parse_turn(json.dumps({"say": "Sonst noch etwas?", "cart": cart}))
    state = new_state()
    searched(state, ENTE)
    order_read_back(state, line(ENTE))

    refused = take_cart(state, turn.cart)

    assert turn.cart is not None
    assert refused is not None
    assert refused.hint.startswith(HINT_CART_FORM)
    assert state.stage == "collecting"
    assert state.order_id is None


def test_refused_cart_after_the_readback_drops_the_draft():
    """The model tried to change the order and failed: as after a failed
    draft_order, a yes must not confirm what the guest just changed."""
    state = new_state()
    searched(state, ENTE)
    order_read_back(state, line(ENTE, 2))

    assert take_cart(state, [line(ENTE, 2), line(SUPPE)]) is not None

    assert state.stage == "collecting"
    assert state.order_id is None
    assert state.cart == [line(ENTE, 2)]


@pytest.mark.parametrize("cart", [[line(ENTE, 2)], [line(ENTE), line(SUPPE)]])
def test_cart_change_drops_a_reservation_that_was_read_back_too(cart):
    """The guest put an order together, switched to a table, heard the
    reservation read back and then goes on with the order (Codex PR #237, P1).
    The conversation has moved on, as with a menu search after a readback: a
    yes to a question about the order must not book the table. A refused cart
    counts the same, it is the same attempt."""
    state = new_state()
    searched(state, ENTE)
    take_cart(state, [line(ENTE)])
    reservation_id = uuid.uuid4()
    apply_tool_result(
        state,
        "create_reservation",
        ToolResult(ok=True, data={"reservation_id": str(reservation_id)}),
    )
    start = begin_turn(state)

    take_cart(state, cart)

    assert state.stage == "collecting"
    assert state.reservation_id is None
    confirm = ToolCall(
        "confirm", {"entity": "reservation", "entity_id": str(reservation_id)}
    )
    assert refusal(state, start, "Ja", confirm) is not None


def test_same_cart_keeps_a_reservation_that_was_read_back():
    state = new_state()
    searched(state, ENTE)
    take_cart(state, [line(ENTE)])
    apply_tool_result(
        state,
        "create_reservation",
        ToolResult(ok=True, data={"reservation_id": str(uuid.uuid4())}),
    )

    take_cart(state, [line(ENTE)])

    assert state.stage == "readback_pending"
    assert state.reservation_id is not None


def test_emptied_cart_after_the_readback_drops_the_draft():
    """The guest removes the only dish after the readback (Codex PR #237, P1):
    the order is empty, and the draft that still holds the dish must not be
    confirmable by the next yes."""
    state = new_state()
    searched(state, ENTE)
    order_id = order_read_back(state, line(ENTE))
    start = begin_turn(state)

    assert take_cart(state, []) is None

    assert state.cart == []
    assert state.stage == "collecting"
    assert state.order_id is None
    confirm = ToolCall("confirm", {"entity": "order", "entity_id": order_id})
    assert refusal(state, start, "Ja", confirm) is not None


def test_confirmed_order_empties_the_cart():
    state = new_state()
    searched(state, ENTE)
    order_read_back(state, line(ENTE))

    apply_tool_result(state, "confirm", ToolResult(ok=True, data={}))

    assert state.cart == []
    assert state.to_prompt_json()["cart"] == []


# --- The answer format ---------------------------------------------------------


def test_answer_carries_the_cart():
    turn = parse_turn(json.dumps({"say": "Gern.", "cart": [line(ENTE, 2)]}))

    assert turn.cart == [line(ENTE, 2)]


@pytest.mark.parametrize("absent", [None, {}, ""])
def test_absent_cart_in_an_answer_leaves_the_order_alone(absent):
    """A model fills unused fields with null, "" or {}; that says nothing
    about the order."""
    turn = parse_turn(json.dumps({"say": "Gern.", "cart": absent}))

    assert turn.cart is None


def test_empty_list_in_an_answer_is_an_empty_order():
    """`[]` is a statement, not a blank: the whole order as it stands is
    nothing (Codex PR #237, P1). Read as "unchanged" it left the draft with
    the removed dish waiting for a yes."""
    turn = parse_turn(json.dumps({"say": "Gern.", "cart": []}))

    assert turn.cart == []


def test_order_format_tells_the_model_about_the_cart():
    assert '"cart": null' in ORDER_FORMAT
    assert "- `cart`:" in ORDER_FORMAT
    assert "`open`" in ORDER_FORMAT
    # The rest of the contract is the same text, in the same order.
    assert ORDER_FORMAT.startswith(OUTPUT_FORMAT.split("\n")[0])
    assert ORDER_FORMAT.endswith(OUTPUT_FORMAT.split("\n")[-1])


def test_order_format_names_every_slot_the_core_takes():
    """Seen on a real model: it wrote the name as `customer_name`, the core
    dropped it as no guest slot, and the order had no name a turn later."""
    for name in GUEST_SLOTS:
        assert f"`{name}`" in ORDER_FORMAT


def test_call_about_a_table_gets_the_short_format_unchanged():
    """Measured on a real model (07.10.2026): the reservation cases passed 6
    of 6 with the short format, 4 with the slot names added, 3 with the cart
    text and 1 with both. A call without a menu search sends what it sent
    before the cart existed."""
    assert "cart" not in OUTPUT_FORMAT
    assert "guest_name" not in OUTPUT_FORMAT
    state = new_state(slots={"party_size": 4})

    assert "cart" not in state.to_prompt_json()


def test_state_carries_the_cart_from_the_first_search_on():
    """Also while it is empty: the answer to the search result is where the
    model writes the dish, so the rules for `cart` must go out with it."""
    state = new_state()
    searched(state, ENTE)

    assert state.to_prompt_json()["cart"] == []


@pytest.mark.parametrize(
    ("state", "sent", "not_sent"),
    [
        ({"stage": "start"}, OUTPUT_FORMAT, ORDER_FORMAT),
        ({"stage": "start", "cart": []}, ORDER_FORMAT, OUTPUT_FORMAT),
    ],
)
def test_client_sends_the_format_that_fits_the_state(state, sent, not_sent):
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        answer = {"choices": [{"message": {"content": '{"say": "Gern."}'}}]}
        return httpx.Response(200, json=answer)

    client = ChatCompletionsLLM(
        "http://model.test/v1", "m", transport=httpx.MockTransport(handler)
    )
    client.next_turn("system", state, "Hallo")

    system = bodies[0]["messages"][0]["content"]
    assert system.endswith(sent)
    assert not system.endswith(not_sent)


# --- Through the loop, with a menu ---------------------------------------------


@pytest.fixture
def session(migrated_db_url):
    engine = create_engine(migrated_db_url)
    with Session(engine) as s:
        yield s
    engine.dispose()


@pytest.fixture
def tenant_id(session):
    return _tenant(session, "Testbetrieb")


@pytest.fixture
def state(session, tenant_id):
    return initial_state(_call(session, tenant_id), tenant_id, "+4972215551234")


def item_id(session, tenant_id, number):
    return str(
        session.scalar(
            select(MenuItem.id).where(
                MenuItem.tenant_id == tenant_id, MenuItem.number == number
            )
        )
    )


class StatelessModel:
    """Like a real model behind `ChatCompletionsLLM`: it knows only the state
    and the input of this very request. Whatever it needs from an earlier
    turn has to stand in the state."""

    def __init__(self, answer):
        self._answer = answer
        self.states: list[dict] = []
        self.inputs: list[str] = []

    def next_turn(self, system_prompt, state_json, input_text):
        # A copy as the model would get it over the wire.
        seen = json.loads(json.dumps(state_json, default=str))
        self.states.append(seen)
        self.inputs.append(input_text)
        return self._answer(seen, input_text)


def tool_result(input_text):
    return json.loads(input_text) if input_text.startswith("{") else None


def pickup_order(state, said):
    """A pickup order over four turns, decided from state and input alone."""
    result = tool_result(said)
    if result and result["tool"] == "search_menu":
        cart = [
            line(part["results"][0]["menu_item_id"])
            for part in result["data"]["positions"]
        ]
        return LLMTurn(say=result.get("say") or "Gern.", cart=cart)
    if result and result["tool"] == "draft_order":
        return LLMTurn(say=result["data"]["readback"])
    if result and result["tool"] == "confirm":
        return LLMTurn(say="Vielen Dank, bis gleich.")
    if "47" in said:
        return LLMTurn(tool_call=ToolCall("search_menu", {"query": said}))
    if said == "Mit Huhn":
        # The group comes from `open` of the line: no search result is in
        # sight any more. The lines go back as they were read, labels included.
        cart = [dict(entry) for entry in state["cart"]]
        for entry in cart:
            for group in entry.pop("open", []):
                entry["options"] = [{"group": group["group"], "name": "Huhn"}]
        return LLMTurn(say="Auf welchen Namen?", cart=cart)
    if said == "Müller":
        return LLMTurn(
            tool_call=ToolCall(
                "draft_order",
                {
                    "type": "pickup",
                    "customer": {"name": said, "phone": state["slots"]["phone"]},
                    "items": state["cart"],
                },
            ),
            state_patch={"guest_name": said},
        )
    return LLMTurn(
        tool_call=ToolCall(
            "confirm", {"entity": "order", "entity_id": state["order_id"]}
        )
    )


def test_order_over_several_turns_survives_on_the_state_alone(
    session, tenant_id, state
):
    """The case this was built for: search in the first turn, an option in the
    second, the name in the third, the yes in the fourth."""
    model = StatelessModel(pickup_order)
    loop = ConversationLoop(session, model, "system", now=NOW)
    ente = item_id(session, tenant_id, "47")
    pho = item_id(session, tenant_id, "13")

    loop.run_turn(state, "die 47 und einmal Pho Bo")
    assert state.cart == [line(ente), line(pho)]

    loop.run_turn(state, "Mit Huhn")
    # What the model read at the start of the second turn.
    assert model.states[2]["cart"] == [
        {
            **line(ente),
            "number": "47",
            "name": "Ente knusprig",
            "open": [{"group": "Fleisch", "options": ["Ente", "Huhn"]}],
        },
        {**line(pho), "number": "13", "name": "Pho Bo"},
    ]

    loop.run_turn(state, "Müller")
    assert state.stage == "readback_pending"
    loop.run_turn(state, "Ja, passt so.")

    order = session.scalars(select(Order)).one()
    assert order.status == "confirmed"
    booked = {
        str(entry.menu_item_id): entry
        for entry in session.scalars(
            select(OrderItem).where(OrderItem.order_id == order.id)
        )
    }
    assert {key: entry.quantity for key, entry in booked.items()} == {ente: 1, pho: 1}
    assert [option["option"] for option in booked[ente].options] == ["Huhn"]
    assert state.cart == []


def test_refused_cart_stops_the_answer_and_tells_the_model(session, state):
    """An invented dish in the cart: the sentence that goes with it is not
    spoken, the model learns why, and the call log shows the attempt."""
    llm = FakeLLM(
        [
            LLMTurn(say="Die Ente habe ich notiert.", cart=[line(str(uuid.uuid4()))]),
            LLMTurn(say="Welches Gericht darf es sein?"),
        ]
    )
    loop = ConversationLoop(session, llm, "system", now=NOW)

    result = loop.run_turn(state, "Einmal die Ente bitte")

    assert result.say == ["Welches Gericht darf es sein?"]
    assert state.cart == []
    refused = json.loads(llm.calls[1][2])
    assert refused == {
        "tool": "cart",
        "ok": False,
        "error_code": "invalid_input",
        "hint": HINT_CART_ITEM,
    }
    logged = session.scalars(select(Call.tool_calls)).one()[-1]
    assert logged["name"] == "cart"
    assert logged["ok"] is False


def test_tool_call_next_to_a_refused_cart_is_not_dispatched(session, tenant_id, state):
    llm = FakeLLM(
        [
            LLMTurn(
                tool_call=ToolCall("search_menu", {"query": "die 47"}),
                cart=[line(str(uuid.uuid4()))],
            ),
            LLMTurn(say="Einen Moment."),
        ]
    )
    loop = ConversationLoop(session, llm, "system", now=NOW)

    loop.run_turn(state, "die 47")

    names = [entry["name"] for entry in session.scalars(select(Call.tool_calls)).one()]
    assert names == ["cart"]
    assert state.known_item_ids == set()


@pytest.mark.parametrize("emptied", [False, True])
def test_yes_after_a_changed_cart_does_not_confirm_the_old_draft(
    session, tenant_id, state, emptied
):
    """The guest changes the quantity after the readback, or removes the only
    dish (Codex PR #237, P1). The model notes it without drafting again and
    asks on. The yes to that question is no yes to the order that was read
    out."""
    ente = item_id(session, tenant_id, "47")
    huhn = [{"group": "Fleisch", "name": "Huhn"}]
    changed = [] if emptied else [line(ente, 3, options=huhn)]
    llm = FakeLLM(
        [
            LLMTurn(tool_call=ToolCall("search_menu", {"query": "die 47"})),
            LLMTurn(
                tool_call=ToolCall(
                    "draft_order",
                    {
                        "type": "pickup",
                        "customer": {"name": "Müller", "phone": "+4972215551234"},
                        "items": [line(ente, 2, options=huhn)],
                    },
                )
            ),
            LLMTurn(say="Zweimal Ente knusprig mit Huhn. Passt das so?"),
        ]
    )
    ConversationLoop(session, llm, "system", now=NOW).run_turn(state, "zweimal die 47")
    order = session.scalars(select(Order)).one()
    assert state.stage == "readback_pending"

    llm = FakeLLM([LLMTurn(say="Gern. Sonst noch etwas?", cart=changed)])
    ConversationLoop(session, llm, "system", now=NOW).run_turn(state, "Mach drei draus")
    llm = FakeLLM(
        [
            LLMTurn(
                tool_call=ToolCall(
                    "confirm", {"entity": "order", "entity_id": str(order.id)}
                )
            ),
            LLMTurn(say="Ich lese die Bestellung noch einmal vor."),
        ]
    )
    ConversationLoop(session, llm, "system", now=NOW).run_turn(state, "Ja, passt.")

    session.refresh(order)
    assert order.status == "draft"
    assert state.cart == changed
