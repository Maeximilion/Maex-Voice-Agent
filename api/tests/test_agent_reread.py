"""Rule 3 in the loop: after a refused yes the core reads the draft out itself.

A `confirm` on a yes for a draft that is not what the guest heard last is
refused (`guards.py`, #251). What comes next is not left to the model: the
core ends the turn with the draft's sentence and asks the model nothing more.
The yes to that one books. Every loop test runs for a table and for an order,
on the database, the audit log and the outbox.
"""

import json
import uuid
from dataclasses import dataclass
from typing import Any

import pytest
from sqlalchemy import create_engine, func, select, update
from sqlalchemy.orm import Session

from api.agent.dispatch import ToolResult
from api.agent.guards import HINT_CONFIRM, begin_turn, note_said, reread_on_yes
from api.agent.ladder import UnderstandingLadder
from api.agent.llm import LLMTurn, ToolCall
from api.agent.loop import (
    SAY_ORDER_DROPPED,
    SAY_TIMEOUT,
    SUMMARY_NOTE_TO_CLEAR,
    SUMMARY_OPEN_ORDER,
    SUMMARY_READBACK_UNANSWERED,
    ConversationLoop,
)
from api.agent.state import ConversationState, apply_tool_result
from api.events.types import ORDER_CONFIRMED, RESERVATION_CONFIRMED
from api.models import (
    AuditLog,
    Call,
    Callback,
    MenuItem,
    Order,
    OutboxEvent,
    Reservation,
    ServiceConfig,
)
from api.tests.test_domain_draft_order import NOW, _call, _tenant

CALLER = "+497215551234"
GUEST = "Einen Tisch für vier heute um sieben und zweimal die 23, auf Müller"
OTHER_ANSWER = "Ja, Hunde sind willkommen."


@pytest.fixture
def session(migrated_db_url):
    engine = create_engine(migrated_db_url)
    with Session(engine) as s:
        yield s
    engine.dispose()


@pytest.fixture
def tenant_id(session) -> uuid.UUID:
    tenant_id = _tenant(session, "Testbetrieb")
    # Only here an order goes to the kitchen with its yes, and into the outbox.
    session.execute(
        update(ServiceConfig)
        .where(ServiceConfig.tenant_id == tenant_id)
        .values(call_mode="primary")
    )
    session.commit()
    return tenant_id


@pytest.fixture
def state(session, tenant_id) -> ConversationState:
    return ConversationState(
        call_id=_call(session, tenant_id), tenant_id=tenant_id, caller_phone=CALLER
    )


@dataclass(frozen=True)
class Flow:
    entity: str
    model: type
    event: str
    # The tool calls that end in the draft.
    calls: tuple[ToolCall, ...]

    def draft_id(self, state: ConversationState) -> uuid.UUID | None:
        return getattr(state, f"{self.entity}_id")

    def confirm(self, entity_id: Any) -> ToolCall:
        return ToolCall("confirm", {"entity": self.entity, "entity_id": str(entity_id)})


@pytest.fixture(params=["reservation", "order"])
def flow(request, session, tenant_id) -> Flow:
    if request.param == "reservation":
        table = ToolCall(
            "create_reservation",
            {
                "guest_name": "Müller",
                "phone": CALLER,
                "party_size": 4,
                "reserved_for": "2026-09-15T19:00:00+02:00",
            },
        )
        return Flow("reservation", Reservation, RESERVATION_CONFIRMED, (table,))
    return _order_flow(session, tenant_id)


def _order_flow(session, tenant_id) -> Flow:
    item_id = str(
        session.scalar(
            select(MenuItem.id).where(
                MenuItem.tenant_id == tenant_id, MenuItem.number == "23"
            )
        )
    )
    order = (
        ToolCall("search_menu", {"query": "die 23"}),
        ToolCall(
            "draft_order",
            {
                "type": "pickup",
                "customer": {"name": "Müller", "phone": CALLER},
                "items": [{"menu_item_id": item_id, "quantity": 2}],
            },
        ),
    )
    return Flow("order", Order, ORDER_CONFIRMED, order)


class Model:
    """Plays its tool calls, then speaks: `say`, or without one the sentence of
    the tool result it just got (`say` of a refusal, `readback` of a draft),
    the way the prompt asks for it. A step may be a whole answer, or a
    function of the state the model is shown."""

    def __init__(self, *steps: Any, say: str | None = None):
        self._steps = list(steps)
        self._say = say
        self.inputs: list[str] = []

    def next_turn(
        self, system_prompt, state_json, input_text, *, timeout=None
    ) -> LLMTurn:
        self.inputs.append(input_text)
        if self._steps:
            step = self._steps.pop(0)
            if callable(step):
                step = step(state_json)
            return step if isinstance(step, LLMTurn) else LLMTurn(tool_call=step)
        if self._say is not None:
            return LLMTurn(say=self._say)
        result = json.loads(input_text)
        return LLMTurn(say=result.get("say") or result["data"]["readback"])


def turn(session, state, text: str, model: Model) -> list[str]:
    loop = ConversationLoop(session, model, "system", now=NOW, clock=lambda: 0)
    return loop.run_turn(state, text).say


def read_to_the_guest(session, state, flow: Flow) -> str:
    """One guest turn that ends with the draft read out. Returns the sentence."""
    said = turn(session, state, GUEST, Model(*flow.calls))
    assert state.stage == "readback_pending" and state.readback_heard
    return said[0]


def status(session, flow: Flow, entity_id) -> str:
    session.expire_all()
    return session.get(flow.model, entity_id).status


def booked(session, flow: Flow) -> tuple[int, int]:
    """How often the booking was written: audit log, outbox."""
    audit = session.scalar(
        select(func.count())
        .select_from(AuditLog)
        .where(AuditLog.action == f"{flow.entity}.confirmed")
    )
    outbox = session.scalar(
        select(func.count())
        .select_from(OutboxEvent)
        .where(OutboxEvent.event_type == flow.event)
    )
    return audit, outbox


def refused_yes(session, state, flow: Flow) -> tuple[str, uuid.UUID]:
    """A draft that was read out, then another answer: the next yes cannot
    mean the draft any more. Returns the draft's sentence and its id."""
    readback = read_to_the_guest(session, state, flow)
    turn(session, state, "Darf der Hund mit?", Model(say=OTHER_ANSWER))
    draft_id = flow.draft_id(state)
    assert draft_id is not None
    return readback, draft_id


def other_cart(flow: Flow) -> list[dict[str, Any]]:
    """An order that differs from what was drafted: taken as a change in the
    order flow, refused for its unknown dish in a call about a table. Either
    way it would drop the draft that waits."""
    if flow.entity == "order":
        dish = flow.calls[-1].args["items"][0]["menu_item_id"]
        return [{"menu_item_id": dish, "quantity": 5}]
    return [{"menu_item_id": str(uuid.uuid4()), "quantity": 1}]


def test_yes_after_another_answer_gets_the_draft_read_out_not_a_booking(
    session, state, flow
):
    """The read-back is no longer the last thing the guest heard: the yes may
    mean the answer in between. The core reads the draft out and does not ask
    the model again, so there is no sentence like "Ist gebucht." to replace.
    The yes after that books, once."""
    readback, draft_id = refused_yes(session, state, flow)
    model = Model(flow.confirm(draft_id), say="Ist gebucht.")

    said = turn(session, state, "Ja", model)

    assert said == [readback]
    assert len(model.inputs) == 1
    assert state.readback_heard
    assert status(session, flow, draft_id) == "draft"
    assert booked(session, flow) == (0, 0)
    logged = session.scalars(select(Call.tool_calls)).one()[-1]
    assert (logged["name"], logged["ok"]) == ("confirm", False)

    turn(session, state, "Ja", Model(flow.confirm(draft_id), say="Danke."))

    assert status(session, flow, draft_id) == "confirmed"
    assert booked(session, flow) == (1, 1)


def test_yes_after_a_draft_nobody_read_out_gets_it_read_out(session, state, flow):
    """The audit case of 12.10.2026 to its end: the model drafts and asks
    something else, the guest says yes to that. Nothing is booked, the guest
    hears the draft, and the yes to it books."""
    drafts = Model(*flow.calls, say="Möchten Sie am Fenster sitzen?")
    assert turn(session, state, GUEST, drafts) == ["Möchten Sie am Fenster sitzen?"]
    draft_id = flow.draft_id(state)
    readback = json.loads(drafts.inputs[-1])["data"]["readback"]

    said = turn(session, state, "Ja", Model(flow.confirm(draft_id), say="Sehr gern."))

    assert said == [readback]
    assert status(session, flow, draft_id) == "draft"
    assert booked(session, flow) == (0, 0)

    turn(session, state, "Ja", Model(flow.confirm(draft_id), say="Danke."))

    assert status(session, flow, draft_id) == "confirmed"
    assert booked(session, flow) == (1, 1)


def test_yes_in_the_turn_of_the_draft_gets_the_draft_read_out(session, state, flow):
    """Draft and `confirm` in one turn on a bare yes: the draft did not exist
    when the guest spoke. The turn ends with its sentence."""
    turn(session, state, GUEST, Model(say="Soll ich das so aufnehmen?"))

    def confirm_what_waits(state_json: dict[str, Any]) -> ToolCall:
        return flow.confirm(state_json[f"{flow.entity}_id"])

    model = Model(*flow.calls, confirm_what_waits, say="Ist gebucht.")

    said = turn(session, state, "Ja", model)

    draft_id = flow.draft_id(state)
    readback = json.loads(model.inputs[len(flow.calls)])["data"]["readback"]
    assert said == [readback]
    assert status(session, flow, draft_id) == "draft"

    turn(session, state, "Ja", Model(flow.confirm(draft_id), say="Danke."))

    assert status(session, flow, draft_id) == "confirmed"
    assert booked(session, flow) == (1, 1)


def test_what_stands_next_to_the_refused_confirm_is_not_taken(session, state, flow):
    """The answer that carries the `confirm` also carries a changed order, a
    patch and a report to the understanding ladder. A changed or refused
    `cart` drops the draft that waits, a phone number in a patch does too,
    and the ladder is one failure away from ending the call. None of it is
    taken: the guest said yes and nothing else (Codex PR #273)."""
    readback, draft_id = refused_yes(session, state, flow)
    answer = LLMTurn(
        tool_call=flow.confirm(draft_id),
        cart=other_cart(flow),
        state_patch={"phone": CALLER},
        understanding_failure="phone",
    )
    loop = ConversationLoop(session, Model(answer), "system", now=NOW, clock=lambda: 0)
    probe, needed = UnderstandingLadder(), 0
    while not probe.should_end_call("phone"):
        probe.record_failure("phone")
        needed += 1
    for _ in range(needed - 1):
        loop._ladder.record_failure("phone")

    result = loop.run_turn(state, "Ja")

    assert result.say == [readback]
    assert result.ended is False and not state.transferred
    assert flow.draft_id(state) == draft_id and state.stage == "readback_pending"

    turn(session, state, "Ja", Model(flow.confirm(draft_id), say="Danke."))

    assert status(session, flow, draft_id) == "confirmed"
    assert booked(session, flow) == (1, 1)


def test_the_time_limit_comes_before_the_reread(session, state, flow):
    """An answer that arrives after the call's time limit is not answered
    with the draft's sentence either: the call goes to the team."""
    _, draft_id = refused_yes(session, state, flow)
    ticks = iter([0, 0, 100])
    loop = ConversationLoop(
        session,
        Model(flow.confirm(draft_id)),
        "system",
        now=NOW,
        max_call_seconds=10,
        clock=lambda: next(ticks, 100),
    )

    result = loop.run_turn(state, "Ja")

    assert result.ended is True and state.transferred
    assert status(session, flow, draft_id) == "draft"
    assert booked(session, flow) == (0, 0)


def test_no_after_another_answer_is_left_to_the_model(session, state, flow):
    """The draft is put again only on a yes. A guest who wants a change is
    asked what, not the old question once more."""
    _, draft_id = refused_yes(session, state, flow)
    model = Model(flow.confirm(draft_id), say="Was soll ich ändern?")

    said = turn(session, state, "Nein, ich möchte etwas ändern", model)

    assert said == ["Was soll ich ändern?"]
    told = json.loads(model.inputs[1])
    assert told["hint"] == HINT_CONFIRM and "say" not in told
    assert status(session, flow, draft_id) == "draft"
    assert booked(session, flow) == (0, 0)


def test_no_to_the_read_back_goes_back_to_the_model(session, state, flow):
    """The question was heard and answered with a no: nothing to read out
    again."""
    read_to_the_guest(session, state, flow)
    draft_id = flow.draft_id(state)
    model = Model(flow.confirm(draft_id), say="Was soll ich ändern?")

    said = turn(session, state, "Nein, das stimmt nicht", model)

    assert said == ["Was soll ich ändern?"]
    assert status(session, flow, draft_id) == "draft"


# --- the guard, without a database -------------------------------------------

READBACK = "Ein Tisch für vier Personen heute um sieben Uhr. Passt das so?"


def drafted(heard: bool) -> tuple[ConversationState, ToolCall]:
    """A state with a draft, read to the guest or not, and the `confirm` for it."""
    state = ConversationState(call_id=uuid.uuid4(), tenant_id=uuid.uuid4())
    draft_id = str(uuid.uuid4())
    apply_tool_result(
        state,
        "create_reservation",
        ToolResult(ok=True, data={"reservation_id": draft_id, "readback": READBACK}),
    )
    note_said(state, READBACK if heard else "Möchten Sie am Fenster sitzen?")
    confirm = ToolCall("confirm", {"entity": "reservation", "entity_id": draft_id})
    return state, confirm


@pytest.mark.parametrize(
    ("heard", "said", "sentence"),
    [
        (False, "Ja", READBACK),
        (False, "Ja, passt so.", READBACK),
        # No yes: the model answers itself.
        (False, "Nein, lieber später", None),
        (False, "Ja, und noch eine Cola", None),
        (False, "Passt das so?", None),
        # Heard and a yes: the `confirm` goes through, nothing to read out.
        (True, "Ja", None),
        (True, "Nein, das stimmt nicht", None),
    ],
)
def test_only_a_yes_for_an_unheard_draft_gets_the_draft_read_out(heard, said, sentence):
    state, confirm = drafted(heard)

    assert reread_on_yes(state, begin_turn(state), said, confirm) == sentence


def test_a_yes_for_another_id_than_the_heard_draft_reads_nothing_out():
    """The draft was heard; the model names another id. That `confirm` is
    refused, and there is nothing the guest has not heard."""
    state, _ = drafted(heard=True)
    other = ToolCall("confirm", {"entity": "reservation", "entity_id": "x"})

    assert reread_on_yes(state, begin_turn(state), "Ja", other) is None


def test_only_a_confirm_gets_the_draft_read_out():
    state, _ = drafted(heard=False)
    start = begin_turn(state)

    assert reread_on_yes(state, start, "Ja", None) is None
    assert reread_on_yes(state, start, "Ja", ToolCall("transfer_to_team", {})) is None


def test_a_yes_without_any_draft_reads_nothing_out():
    state = ConversationState(call_id=uuid.uuid4(), tenant_id=uuid.uuid4())
    confirm = ToolCall("confirm", {"entity": "order", "entity_id": str(uuid.uuid4())})

    assert reread_on_yes(state, begin_turn(state), "Ja", confirm) is None


# --- #678: the expected time closes the read-back of an order ------------------


@pytest.fixture
def order(session, tenant_id) -> Flow:
    return _order_flow(session, tenant_id)


@pytest.mark.parametrize(
    "answer", ["Okay", "Alles klar", "Gut", "Okay, danke", "In Ordnung", "Ja"]
)
def test_an_answer_to_the_stated_time_books_the_order(session, state, order, answer):
    """Maxi, 23.11.2026: callers answer the time with "okay", "alles klar" or
    "gut", and that is their yes to what was read out."""
    said = read_to_the_guest(session, state, order)
    assert said.endswith("Es dauert so ungefähr 30 Minuten.")
    draft_id = order.draft_id(state)

    turn(session, state, answer, Model(order.confirm(draft_id), say="Danke."))

    assert status(session, order, draft_id) == "confirmed"


@pytest.mark.parametrize(
    "answer", ["Gut?", "Alles klar, und wie lange genau?", "Gut, und noch eine 23"]
)
def test_an_acknowledgement_that_asks_or_adds_books_nothing(
    session, state, order, answer
):
    read_to_the_guest(session, state, order)
    draft_id = order.draft_id(state)

    turn(session, state, answer, Model(order.confirm(draft_id), say="Gerne."))

    assert status(session, order, draft_id) == "draft"


def test_an_acknowledgement_after_other_talk_books_nothing(session, state, order):
    """ "Alles klar" after the answer about the dog acknowledges that answer."""
    _, draft_id = refused_yes(session, state, order)

    turn(session, state, "Alles klar", Model(order.confirm(draft_id)))

    assert status(session, order, draft_id) == "draft"


def test_alles_klar_is_no_yes_to_a_table(session, state, tenant_id, flow):
    """A table's read-back asks "Passt das so?"; only an order states a time."""
    if flow.entity != "reservation":
        pytest.skip("the order is the case above")
    read_to_the_guest(session, state, flow)
    draft_id = flow.draft_id(state)

    turn(session, state, "Alles klar", Model(flow.confirm(draft_id), say="Gerne."))

    assert status(session, flow, draft_id) == "draft"


@pytest.mark.parametrize(
    "answer",
    ["Oh, so lange? Dann stornieren Sie das bitte.", "Nein, dann storniere ich."],
)
def test_a_cancel_after_the_stated_time_drops_the_draft_without_the_team(
    session, state, order, answer
):
    """Nothing is booked yet: the draft is dropped, no callback card, no
    transfer, and the next yes cannot confirm it."""
    read_to_the_guest(session, state, order)
    draft_id = order.draft_id(state)

    said = turn(session, state, answer, Model(say="Nicht gesprochen."))

    assert said == [SAY_ORDER_DROPPED]
    assert (state.order_id, state.readback, state.stage) == (None, None, "collecting")
    assert (state.cart, state.order_note) == ([], None)
    assert status(session, order, draft_id) == "draft"
    assert session.scalar(select(func.count()).select_from(Callback)) == 0
    turn(session, state, "Ja", Model(order.confirm(draft_id), say="Gerne."))
    assert status(session, order, draft_id) == "draft"


def test_the_same_order_after_a_cancel_is_drafted_anew(session, state, order):
    """#952: the dropped draft stays the call's latest row under the key of
    the same dishes. Replayed, it kept its old pickup time and skipped the
    checks of hours and sold-out dishes; the order is drafted anew instead,
    and that draft's own retry replays it."""
    read_to_the_guest(session, state, order)
    dropped = order.draft_id(state)
    turn(session, state, "Dann stornieren Sie das bitte.", Model(say="x"))

    read_to_the_guest(session, state, order)
    again = order.draft_id(state)
    turn(session, state, "Wie bitte?", Model(order.calls[-1]))

    assert again is not None and again != dropped
    assert order.draft_id(state) == again
    assert (status(session, order, dropped), status(session, order, again)) == (
        "draft",
        "draft",
    )
    turn(session, state, "Okay", Model(order.confirm(again), say="Danke."))
    assert status(session, order, again) == "confirmed"
    assert status(session, order, dropped) == "draft"


@pytest.mark.parametrize(
    "answer",
    [
        "Bitte nicht stornieren, das passt so.",
        "Kann ich das später noch stornieren?",
        "Die Cola bitte abbestellen",
        "Den Tisch für morgen bitte absagen",
        "Die Bestellung von vorhin stornieren Sie bitte",
        # A question about the state is no cancel (Codex, PR #954).
        "Ist die Bestellung storniert?",
        "Ist die Bestellung storniert",
        "Die Bestellung ist storniert?",
        "Stornieren?",
    ],
)
def test_a_cancel_of_something_else_keeps_the_order_and_goes_to_the_team(
    session, state, order, answer
):
    """Review of #678: only a plain cancel drops the order that waits. A
    "nicht", a question, one line, a table or an earlier order is the team's,
    with the sentence and the open order on its card, as before."""
    read_to_the_guest(session, state, order)
    draft_id = order.draft_id(state)

    said = turn(session, state, answer, Model(say="x"))

    assert said != [SAY_ORDER_DROPPED]
    assert state.cart, "the open order stays for the team's card"
    assert status(session, order, draft_id) == "draft"


def test_a_cancel_past_the_call_limit_ends_with_the_team(session, state, order):
    """The dropped order's sentence asks for more; past its limit the call
    goes to the team instead, as on any other turn."""
    read_to_the_guest(session, state, order)
    times = iter([0.0, 10_000.0])
    loop = ConversationLoop(
        session,
        Model(say="x"),
        "system",
        now=NOW,
        clock=lambda: next(times, 10_000.0),
        max_call_seconds=60,
    )

    said = loop.run_turn(state, "Dann stornieren Sie das bitte.").say

    assert SAY_TIMEOUT in said and SAY_ORDER_DROPPED not in said
    assert state.order_id is not None


def test_a_cancel_without_a_draft_still_goes_to_the_team(session, state):
    said = turn(session, state, "Ich möchte meine Bestellung stornieren", Model())

    assert said != [SAY_ORDER_DROPPED]
    assert state.stage != "collecting"


def test_a_cancel_of_a_table_keeps_the_order_that_waits(session, state, order):
    """ "Meine Reservierung stornieren" goes to the team as before; the order
    read back is not what the guest cancels."""
    read_to_the_guest(session, state, order)
    draft_id = order.draft_id(state)

    said = turn(
        session, state, "Ich möchte meine Reservierung stornieren", Model(say="x")
    )

    assert said != [SAY_ORDER_DROPPED]
    assert state.order_id == draft_id


# --- #953: a call that ends while a read-back waits for its yes -----------------


def cards(session, state) -> list[Callback]:
    session.expire_all()
    return list(
        session.scalars(select(Callback).where(Callback.call_id == state.call_id))
    )


def test_a_call_that_ends_on_the_read_back_leaves_a_card(session, state, order):
    """#953 (b): "Danke" alone is no yes (Maxi, 23.11.2026), and the caller
    hangs up. Nothing is booked, and the guest expects the food: the team
    calls back, with what was read out on the card. Once, however often the
    end comes."""
    read_to_the_guest(session, state, order)
    draft_id = order.draft_id(state)
    turn(session, state, "Danke", Model(order.confirm(draft_id), say="Gerne."))
    assert status(session, order, draft_id) == "draft"
    loop = ConversationLoop(session, Model(), "system", now=NOW, clock=lambda: 0)

    loop.offer_unanswered(state)
    loop.offer_unanswered(state)

    card = cards(session, state)
    assert len(card) == 1
    # Its own reason (#977, Maxi 24.11.2026): "Bestellung offen" on the
    # tablet, not "Nicht verstanden".
    assert card[0].reason == "order_open"
    assert card[0].summary.startswith(SUMMARY_READBACK_UNANSWERED)
    assert SUMMARY_OPEN_ORDER in card[0].summary
    assert card[0].phone == CALLER
    assert status(session, order, draft_id) == "draft"
    assert state.stage == "callback"


def test_a_table_or_a_call_with_the_team_leaves_no_card(session, state, flow):
    """A table's read-back asks "Passt das so?": unanswered, the guest knows
    nothing is booked (`reservierung_0032`). An outage or a failed line takes
    the call to the team, whose own card waits if nobody answers."""
    read_to_the_guest(session, state, flow)
    loop = ConversationLoop(session, Model(), "system", now=NOW, clock=lambda: 0)

    loop.offer_unanswered(state, with_the_team=True)
    if flow.entity == "reservation":
        loop.offer_unanswered(state)

    assert cards(session, state) == []
    assert state.stage == "readback_pending"


def test_a_booked_order_leaves_no_card(session, state, order):
    loop = ConversationLoop(session, Model(), "system", now=NOW, clock=lambda: 0)
    read_to_the_guest(session, state, order)

    turn(session, state, "Okay", Model(order.confirm(order.draft_id(state)), say="x"))
    loop.offer_unanswered(state)
    assert cards(session, state) == []


def test_a_read_back_without_a_number_leaves_no_card(session, tenant_id, order):
    """A withheld number nobody named: there is nobody to call back, and the
    call ends as before."""
    state = ConversationState(call_id=_call(session, tenant_id), tenant_id=tenant_id)
    state.slots["phone"] = CALLER
    read_to_the_guest(session, state, order)
    state.slots.pop("phone")
    loop = ConversationLoop(session, Model(), "system", now=NOW, clock=lambda: 0)

    loop.offer_unanswered(state)

    assert cards(session, state) == []
    assert state.stage == "readback_pending"


def test_the_card_of_an_unanswered_order_leads_with_the_kitchen_marker(
    session, state, order
):
    """The guest spoke of an allergy in the call (#940): the card leads with
    the marker and never names the allergy (L13)."""
    read_to_the_guest(session, state, order)
    state.allergy_said = True
    loop = ConversationLoop(session, Model(), "system", now=NOW, clock=lambda: 0)

    loop.offer_unanswered(state)

    (card,) = cards(session, state)
    assert card.summary.startswith(SUMMARY_NOTE_TO_CLEAR)
    assert SUMMARY_READBACK_UNANSWERED in card.summary
