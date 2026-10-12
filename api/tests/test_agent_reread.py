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
from api.agent.loop import ConversationLoop
from api.agent.state import ConversationState, apply_tool_result
from api.events.types import ORDER_CONFIRMED, RESERVATION_CONFIRMED
from api.models import (
    AuditLog,
    Call,
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

    def next_turn(self, system_prompt, state_json, input_text) -> LLMTurn:
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
