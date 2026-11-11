"""A delivery by phone through the core (T-6.5, T-6.2): the tools, the state,
the guards, and what never reaches the model or the guest (legal check L12)."""

import json
import uuid

import pytest
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import Session

from api.agent import guards
from api.agent.dispatch import TOOLS, dispatch
from api.agent.intent import heard
from api.agent.llm import FakeLLM, LLMTurn, ToolCall
from api.agent.loop import ConversationLoop
from api.agent.state import (
    apply_state_patch,
    apply_tool_result,
    initial_state,
    note_guest_details,
    note_intent,
)
from api.domain.customers import SAY_SAME_ADDRESS
from api.domain.delivery.zones import SAY_OUT_OF_ZONE
from api.domain.ordering.validation import SAY_DELIVERY_CLOSED
from api.domain.status.config import set_delivery
from api.models import (
    Address,
    Customer,
    DeliveryZone,
    Order,
    RecognitionObjection,
    ServiceConfig,
)
from api.tests.test_domain_draft_order import NOW, _call, _tenant, item
from sim.scripted_llm import ScriptedLLM

CALLER = "+497215551234"
ADDRESS = {
    "street": "Lindenstraße",
    "house_number": "12",
    "postal_code": "12345",
    "city": "Musterdorf",
}
SENTENCE = (
    "Bitte zweimal die 23 liefern, Lindenstraße 12, 12345 Musterdorf, "
    "auf den Namen Braun."
)


@pytest.fixture
def engine(migrated_db_url):
    engine = create_engine(migrated_db_url)
    yield engine
    engine.dispose()


@pytest.fixture
def session(engine):
    with Session(engine) as s:
        yield s


@pytest.fixture
def tenant_id(session) -> uuid.UUID:
    tid = _tenant(session, "Testbetrieb")
    session.add(
        DeliveryZone(
            tenant_id=tid,
            name="Zone 1",
            match_type="postal_code",
            postal_codes=["12345"],
            fee_cents=250,
            min_order_cents=1000,
            eta_minutes=50,
        )
    )
    session.commit()
    return tid


@pytest.fixture
def call_id(session, tenant_id) -> uuid.UUID:
    return _call(session, tenant_id, caller_id=CALLER)


def known(session, tenant_id, name="Müller", phone=CALLER):
    row = Customer(tenant_id=tenant_id, phone=phone, name=name, order_count=1)
    session.add(row)
    session.flush()
    home = Address(
        tenant_id=tenant_id,
        customer_id=row.id,
        street="Rheinstraße",
        house_number="54",
        postal_code="12345",
        city="Musterdorf",
        is_default=True,
    )
    session.add(home)
    session.commit()
    return row, home


def _state(call_id, tenant_id):
    return initial_state(call_id, tenant_id, CALLER)


def _draft_args(dish, name="Braun", **customer):
    body = {"phone": CALLER, "address": ADDRESS, **customer}
    if name:
        body["name"] = name
    return {
        "type": "delivery",
        "customer": body,
        "items": [{"menu_item_id": str(dish), "quantity": 2}],
    }


def _opening(dish, **draft):
    """The turns up to the read-back, as a model would answer."""
    return [
        LLMTurn(tool_call=ToolCall("search_menu", {"query": "die 23"})),
        LLMTurn(tool_call=ToolCall("check_delivery", dict(ADDRESS))),
        LLMTurn(tool_call=ToolCall("draft_order", _draft_args(dish, **draft))),
    ]


def _loop(session, turns):
    return ConversationLoop(session, FakeLLM(turns), "prompt", now=NOW)


class Reader(FakeLLM):
    """Reads the draft's sentence out, as a model does."""

    def next_turn(self, system_prompt, state_json, input_text, *, timeout=None):
        turn = super().next_turn(system_prompt, state_json, input_text)
        if turn.say == "READBACK":
            return LLMTurn(say=json.loads(input_text)["data"]["readback"])
        return turn


def _reading_loop(session, turns):
    return ConversationLoop(session, Reader(turns), "prompt", now=NOW)


def test_both_tools_are_in_the_core():
    assert {"check_delivery", "find_customer"} <= TOOLS.keys()


def test_a_delivery_from_the_sentence_to_the_yes(session, tenant_id, call_id):
    dish = item(session, tenant_id, "23")
    turns = [
        *_opening(dish),
        LLMTurn(say="READBACK"),
        LLMTurn(tool_call=ToolCall("confirm", {"entity": "order", "entity_id": "x"})),
    ]
    loop = _reading_loop(session, turns)
    state = _state(call_id, tenant_id)

    said = loop.run_turn(state, SENTENCE).say[0]
    order = session.get(Order, state.order_id)
    assert state.intent == "delivery"
    assert state.stage == "readback_pending"
    assert order.status == "draft"
    assert "inklusive 2 Euro 50 Liefergebühr" in said

    turns[-1] = LLMTurn(
        tool_call=ToolCall(
            "confirm", {"entity": "order", "entity_id": str(state.order_id)}
        )
    )
    loop._llm._turns = [turns[-1]]
    done = loop.run_turn(state, "Ja, passt.")

    session.refresh(order)
    assert order.status == "confirmed"
    assert state.stage == "confirmed"
    assert order.total_cents == 1380 + 250
    assert "Ihre Bestellung auf den Namen Braun" in done.say[0]


def test_no_yes_books_nothing(session, tenant_id, call_id):
    dish = item(session, tenant_id, "23")
    loop = _reading_loop(session, [*_opening(dish), LLMTurn(say="READBACK")])
    state = _state(call_id, tenant_id)
    loop.run_turn(state, SENTENCE)
    order_id = state.order_id
    loop._llm._turns = [
        LLMTurn(
            tool_call=ToolCall(
                "confirm", {"entity": "order", "entity_id": str(order_id)}
            )
        ),
        LLMTurn(say="Was soll ich ändern?"),
    ]

    loop.run_turn(state, "Nein, das stimmt nicht.")

    assert session.get(Order, order_id).status == "draft"


def test_a_new_address_after_the_read_back_takes_the_draft_back(
    session, tenant_id, call_id
):
    """The read-back named the old address: a yes must not send the driver."""
    dish = item(session, tenant_id, "23")
    loop = _reading_loop(session, [*_opening(dish), LLMTurn(say="READBACK")])
    state = _state(call_id, tenant_id)
    loop.run_turn(state, SENTENCE)
    assert state.stage == "readback_pending"

    apply_state_patch(state, {"house_number": "41"})

    assert state.order_id is None
    assert state.stage == "collecting"
    assert state.intent == "delivery"


def test_the_same_address_again_keeps_the_draft(session, tenant_id, call_id):
    dish = item(session, tenant_id, "23")
    loop = _reading_loop(session, [*_opening(dish), LLMTurn(say="READBACK")])
    state = _state(call_id, tenant_id)
    state.slots["house_number"] = "12"
    loop.run_turn(state, SENTENCE)

    apply_state_patch(state, {"house_number": "12"})

    assert state.stage == "readback_pending"


def test_the_address_stays_in_the_slots(call_id, tenant_id):
    state = _state(call_id, tenant_id)

    taken = apply_state_patch(state, {**ADDRESS, "floor_note": "2. OG", "fee": 1})

    assert taken == {**ADDRESS, "floor_note": "2. OG"}
    assert "fee" not in state.slots


def test_the_cart_goes_to_the_model_from_the_wish_on(call_id, tenant_id):
    """The order format names the address slots: an address said before the
    first dish would be lost without it."""
    state = _state(call_id, tenant_id)
    assert "cart" not in state.to_prompt_json()

    note_intent(state, "Ich möchte etwas liefern lassen.")

    assert state.to_prompt_json()["cart"] == []


def test_status_keeps_whether_delivery_is_taken(session, tenant_id, call_id):
    state = _state(call_id, tenant_id)
    result = dispatch(session, call_id, tenant_id, "get_service_status", {}, now=NOW)

    apply_tool_result(state, "get_service_status", result)

    assert state.service["delivery_enabled"] is True


def test_the_team_switch_reaches_a_call_in_progress(
    engine, session, tenant_id, call_id
):
    dish = item(session, tenant_id, "23")
    assert session.get(ServiceConfig, tenant_id).delivery_enabled is True
    with Session(engine) as tablet:
        set_delivery(tablet, tenant_id, False)

    result = dispatch(
        session, call_id, tenant_id, "draft_order", _draft_args(dish), now=NOW
    )

    assert result.error_code == "closed"
    assert result.say is not None
    assert result.say.startswith(SAY_DELIVERY_CLOSED)


def test_a_switch_at_the_yes_takes_the_draft_back(engine, session, tenant_id, call_id):
    """Refused for good: a later yes must not try again once it is back on."""
    dish = item(session, tenant_id, "23")
    loop = _reading_loop(session, [*_opening(dish), LLMTurn(say="READBACK")])
    state = _state(call_id, tenant_id)
    loop.run_turn(state, SENTENCE)
    order_id = state.order_id
    with Session(engine) as tablet:
        set_delivery(tablet, tenant_id, False)
    loop._llm._turns = [
        LLMTurn(
            tool_call=ToolCall(
                "confirm", {"entity": "order", "entity_id": str(order_id)}
            )
        ),
        LLMTurn(say="Lieferung ist gerade leider nicht möglich."),
    ]

    loop.run_turn(state, "Ja.")

    assert session.get(Order, order_id).status == "draft"
    assert state.order_id is None
    assert state.stage == "collecting"
    assert state.intent == "delivery"


def test_recognition_goes_by_the_number_of_the_call(session, tenant_id, call_id):
    row, home = known(session, tenant_id, phone=CALLER)
    known(session, tenant_id, name="Anders", phone="+497215559999")

    result = dispatch(
        session,
        call_id,
        tenant_id,
        "find_customer",
        {"caller_id": "+497215559999"},
        now=NOW,
    )

    assert result.data["customer_id"] == str(row.id)
    assert result.data["address_id"] == str(home.id)
    assert result.say == SAY_SAME_ADDRESS


def test_a_withheld_number_recognizes_nobody(session, tenant_id):
    known(session, tenant_id)
    call_id = _call(session, tenant_id)

    result = dispatch(session, call_id, tenant_id, "find_customer", {}, now=NOW)

    assert result.ok
    assert result.data["found"] is False


def test_a_caller_id_that_is_no_number_recognizes_nobody(session, tenant_id):
    call_id = _call(session, tenant_id, caller_id="anonymous")

    result = dispatch(session, call_id, tenant_id, "find_customer", {}, now=NOW)

    assert result.ok
    assert result.data["found"] is False


def test_an_objection_switches_recognition_off(session, tenant_id, call_id):
    known(session, tenant_id)
    session.add(RecognitionObjection(tenant_id=tenant_id, phone=CALLER))
    session.commit()

    result = dispatch(session, call_id, tenant_id, "find_customer", {}, now=NOW)

    assert result.data == {"found": False, "customer_id": None, "address_id": None}
    assert result.say is None


def test_a_name_the_guest_did_not_say_is_refused(session, tenant_id, call_id):
    """Trying names would read out who stands behind the number."""
    state = _state(call_id, tenant_id)
    note_guest_details(state, "Ich möchte etwas liefern lassen.")
    call = ToolCall("find_customer", {"guest_name": "Müller"})

    refused = guards.refusal(state, guards.begin_turn(state), "Hallo", call)

    assert refused is not None
    assert refused.hint == guards.HINT_GUEST_NAME


def test_a_name_the_guest_said_goes_through(session, tenant_id, call_id):
    state = _state(call_id, tenant_id)
    note_guest_details(state, "Auf den Namen Müller.")
    call = ToolCall("find_customer", {"guest_name": "Müller"})

    assert guards.refusal(state, guards.begin_turn(state), "Hallo", call) is None


def test_no_stored_word_reaches_the_model_or_the_guest(session, tenant_id, call_id):
    """L12 in the core: a recognized caller, a yes, a read-back, a booking. No
    prompt, no tool result, no sentence holds the name or the address."""
    row, home = known(session, tenant_id)
    dish = item(session, tenant_id, "23")
    draft = _draft_args(dish, name=None, address_id=str(home.id))
    del draft["customer"]["address"]
    llm = Reader(
        [
            LLMTurn(tool_call=ToolCall("search_menu", {"query": "die 23"})),
            LLMTurn(tool_call=ToolCall("find_customer", {})),
            LLMTurn(say=SAY_SAME_ADDRESS),
            LLMTurn(tool_call=ToolCall("draft_order", draft)),
            LLMTurn(say="READBACK"),
            LLMTurn(tool_call=ToolCall("confirm", {"entity": "order"})),
        ]
    )
    loop = ConversationLoop(session, llm, "prompt", now=NOW)
    state = _state(call_id, tenant_id)

    said = list(loop.run_turn(state, "Zweimal die 23 liefern bitte.").say)
    assert state.customer == {"found": True, "address_id": str(home.id)}
    said += loop.run_turn(state, "Ja, dorthin.").say
    assert state.stage == "readback_pending"
    llm._turns[0] = LLMTurn(
        tool_call=ToolCall(
            "confirm", {"entity": "order", "entity_id": str(state.order_id)}
        )
    )
    said += loop.run_turn(state, "Ja.").say

    order = session.get(Order, state.order_id)
    session.refresh(order)
    assert order.status == "confirmed"
    assert order.customer_name == "Müller"
    assert (order.customer_id, order.address_id) == (row.id, home.id)
    seen = json.dumps(llm.calls, ensure_ascii=False, default=str) + " ".join(said)
    for stored in ("Müller", "Mueller", "Rheinstraße", "Musterdorf"):
        assert stored not in seen, stored


def test_a_recognized_customer_with_another_name_is_asked(session, tenant_id, call_id):
    """L12: a name that does not match opens nobody's record."""
    _, home = known(session, tenant_id)
    dish = item(session, tenant_id, "23")
    draft = _draft_args(dish, name="Braun", address_id=str(home.id))
    del draft["customer"]["address"]

    result = dispatch(session, call_id, tenant_id, "draft_order", draft, now=NOW)

    assert result.error_code == "invalid_input"
    assert result.say is not None
    assert "Rheinstraße" not in result.say


def test_the_state_says_a_lookup_that_found_nobody(session, tenant_id, call_id):
    state = _state(call_id, tenant_id)
    result = dispatch(session, call_id, tenant_id, "find_customer", {}, now=NOW)

    apply_tool_result(state, "find_customer", result)

    assert state.to_prompt_json()["customer"] == {"found": False, "address_id": None}


def test_a_pickup_draft_stays_a_pickup_for_the_call_log(session, tenant_id, call_id):
    dish = item(session, tenant_id, "23")
    state = _state(call_id, tenant_id)
    state.intent = "delivery"
    args = {
        "type": "pickup",
        "customer": {"name": "Braun", "phone": CALLER},
        "items": [{"menu_item_id": str(dish), "quantity": 2}],
    }
    result = dispatch(session, call_id, tenant_id, "draft_order", args, now=NOW)

    apply_tool_result(state, "draft_order", result, args)

    assert state.intent == "pickup"


def test_the_core_books_under_the_same_key_with_the_same_address(
    session, tenant_id, call_id
):
    """A model retry is no second draft; a new address is one."""
    dish = item(session, tenant_id, "23")
    args = _draft_args(dish)

    first = dispatch(session, call_id, tenant_id, "draft_order", args, now=NOW)
    again = dispatch(session, call_id, tenant_id, "draft_order", args, now=NOW)
    moved = dispatch(
        session,
        call_id,
        tenant_id,
        "draft_order",
        _draft_args(dish, address={**ADDRESS, "house_number": "41"}),
        now=NOW,
    )

    assert first.data["order_id"] == again.data["order_id"]
    assert moved.data["order_id"] != first.data["order_id"]
    assert session.scalar(select(Order.total_cents).limit(1)) == 1630


def test_below_the_minimum_reaches_the_model_with_its_sentence(
    session, tenant_id, call_id
):
    dish = item(session, tenant_id, "23")
    session.execute(update(DeliveryZone).values(min_order_cents=2000))
    session.commit()

    result = dispatch(
        session, call_id, tenant_id, "draft_order", _draft_args(dish), now=NOW
    )

    assert result.error_code == "below_minimum"
    assert result.say is not None
    assert "6 Euro 20" in result.say
    assert session.scalar(select(Order.id)) is None


def test_out_of_zone_reaches_the_model_with_its_sentence(session, tenant_id, call_id):
    result = dispatch(
        session,
        call_id,
        tenant_id,
        "check_delivery",
        {"postal_code": "54321", "street": "Hauptstraße"},
        now=NOW,
    )

    assert result.error_code == "out_of_zone"
    assert result.say is not None
    assert "abholen" in result.say


@pytest.mark.parametrize(
    ("sentence", "wish"),
    [
        ("Ich möchte etwas liefern lassen.", "delivery"),
        ("Liefern Sie nach Musterdorf?", "delivery"),
        ("Eine Lieferung bitte.", "delivery"),
        ("Ich möchte nach Hause bestellen.", "delivery"),
        ("Liefern oder abholen, was geht schneller?", None),
        ("Ein Tisch und etwas geliefert.", None),
        ("Bitte nicht liefern.", None),
        ("Ich möchte bestellen.", "pickup"),
    ],
)
def test_the_wish_for_a_delivery_is_heard(sentence, wish):
    assert heard(sentence) == wish


def test_a_later_word_changes_pickup_and_delivery_until_a_draft(call_id, tenant_id):
    state = _state(call_id, tenant_id)
    note_intent(state, "Ich möchte bestellen.")
    assert state.intent == "pickup"

    note_intent(state, "Bitte doch liefern.")
    assert state.intent == "delivery"

    note_intent(state, "Dann möchte ich es doch abholen.")
    assert state.intent == "pickup"


def test_a_delivery_wish_counts_next_to_a_cart(call_id, tenant_id):
    state = _state(call_id, tenant_id)
    state.cart = [{"menu_item_id": str(uuid.uuid4()), "quantity": 1}]

    note_intent(state, "Und bitte liefern.")

    assert state.intent == "delivery"


def test_a_table_stays_a_table(call_id, tenant_id):
    state = _state(call_id, tenant_id)
    note_intent(state, "Ich möchte einen Tisch reservieren.")

    note_intent(state, "Liefern Sie auch?")

    assert state.intent == "reservation"


def test_a_confirmed_call_keeps_its_intent(call_id, tenant_id):
    state = _state(call_id, tenant_id)
    state.intent = "pickup"
    state.stage = "confirmed"

    note_intent(state, "Liefern Sie eigentlich auch?")

    assert state.intent == "pickup"


def _stand_in(session):
    llm = ScriptedLLM(now=NOW, timezone="Europe/Berlin")
    return ConversationLoop(session, llm, "prompt", now=NOW)


def test_the_stand_in_takes_a_recognized_caller_on_a_yes(session, tenant_id, call_id):
    """The flow of docs/05 §Lieferung through the core, with no question for an
    address or a name, and no stored word in anything the guest hears."""
    row, home = known(session, tenant_id)
    loop = _stand_in(session)
    state = _state(call_id, tenant_id)

    loop.run_turn(state, "Ich möchte etwas liefern lassen.")
    loop.run_turn(state, "Zweimal die 23.")
    asked = loop.run_turn(state, "Nein, das wars.").say
    read = loop.run_turn(state, "Ja, genau.").say
    done = loop.run_turn(state, "Ja, passt.").say

    assert asked == [SAY_SAME_ADDRESS]
    assert "an dieselbe Adresse wie beim letzten Mal" in read[0]
    order = session.get(Order, state.order_id)
    session.refresh(order)
    assert order.status == "confirmed"
    assert (order.customer_id, order.address_id) == (row.id, home.id)
    heard_all = " ".join([*asked, *read, *done])
    assert "Müller" not in heard_all
    assert "Rheinstraße" not in heard_all


def test_the_stand_in_asks_for_the_address_on_a_no(session, tenant_id, call_id):
    known(session, tenant_id)
    loop = _stand_in(session)
    state = _state(call_id, tenant_id)
    loop.run_turn(state, "Ich möchte etwas liefern lassen.")
    loop.run_turn(state, "Zweimal die 23.")
    loop.run_turn(state, "Nein, das wars.")

    asked = loop.run_turn(state, "Nein, woanders hin.").say

    assert "Adresse" in asked[0]
    assert "Rheinstraße" not in asked[0]


def test_a_new_order_after_out_of_zone_is_a_pickup(session, tenant_id, call_id):
    loop = _stand_in(session)
    state = _state(call_id, tenant_id)

    said = loop.run_turn(
        state, "Die 23 bitte liefern, nach 54321 Weitweg, Hauptstraße 1."
    )

    assert said.say[-1] == SAY_OUT_OF_ZONE
    assert state.order_id is None


def test_a_bare_order_sentence_keeps_the_delivery(call_id, tenant_id):
    """Codex PR #472: the model has only the state, so a delivery lost here is
    lost. A bare order word says that the guest orders, not how it comes."""
    state = _state(call_id, tenant_id)
    note_intent(state, "Ich möchte etwas liefern lassen.")

    note_intent(state, "Ich möchte zweimal die 23 bestellen.")

    assert state.intent == "delivery"


@pytest.mark.parametrize(
    "sentence",
    [
        "Dann möchte ich es doch abholen.",
        "Ich möchte zweimal die 23 zum Abholen bestellen.",
        "Ich bestelle das zum Mitnehmen.",
    ],
)
def test_a_word_for_the_pickup_still_changes_a_delivery(call_id, tenant_id, sentence):
    state = _state(call_id, tenant_id)
    note_intent(state, "Ich möchte etwas liefern lassen.")

    note_intent(state, sentence)

    assert state.intent == "pickup"


def test_an_order_for_home_changes_a_bare_order(call_id, tenant_id):
    state = _state(call_id, tenant_id)
    note_intent(state, "Ich möchte bestellen.")

    note_intent(state, "Ich möchte das nach Hause bestellen.")

    assert state.intent == "delivery"
