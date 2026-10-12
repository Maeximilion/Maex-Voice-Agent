"""Rule 3 in the loop: a yes books only what the guest heard as the last thing.

A draft alone waits for nothing. The readback of `create_reservation` or
`draft_order` has to end a turn before the yes of the next one can confirm the
draft: the core reads it out itself where a model does not, and reads it out
again where a `confirm` comes for a draft whose question is no longer the last
thing said. Both flows share the mechanism, so every test runs for a table and
for an order, on the database and the outbox, not on what is said alone.
"""

import json
import uuid
from dataclasses import dataclass
from typing import Any

import pytest
from sqlalchemy import create_engine, func, select, update
from sqlalchemy.orm import Session

from api.agent.guards import HINT_CONFIRM
from api.agent.llm import LLMTurn, ToolCall
from api.agent.loop import ConversationLoop
from api.agent.state import ConversationState
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
    # The tool calls that end in the draft, and the same for a guest who
    # changes one detail.
    calls: tuple[ToolCall, ...]
    changed: tuple[ToolCall, ...]
    # A tool of the other flow: the guest has moved on.
    elsewhere: ToolCall

    def draft_id(self, state: ConversationState) -> uuid.UUID | None:
        return getattr(state, f"{self.entity}_id")

    def confirm(self, entity_id: Any) -> ToolCall:
        return ToolCall("confirm", {"entity": self.entity, "entity_id": str(entity_id)})

    def confirm_pending(self, state_json: dict[str, Any]) -> ToolCall:
        """`confirm` with the id the state shows at that moment."""
        return self.confirm(state_json[f"{self.entity}_id"])


def _table(party_size: int) -> ToolCall:
    return ToolCall(
        "create_reservation",
        {
            "guest_name": "Müller",
            "phone": CALLER,
            "party_size": party_size,
            "reserved_for": "2026-09-15T19:00:00+02:00",
        },
    )


def _order(item_id: str, quantity: int) -> tuple[ToolCall, ...]:
    return (
        ToolCall("search_menu", {"query": "die 23"}),
        ToolCall(
            "draft_order",
            {
                "type": "pickup",
                "customer": {"name": "Müller", "phone": CALLER},
                "items": [{"menu_item_id": item_id, "quantity": quantity}],
            },
        ),
    )


@pytest.fixture(params=["reservation", "order"])
def flow(request, session, tenant_id) -> Flow:
    item_id = str(
        session.scalar(
            select(MenuItem.id).where(
                MenuItem.tenant_id == tenant_id, MenuItem.number == "23"
            )
        )
    )
    slot = ToolCall(
        "check_slot", {"party_size": 2, "reserved_for": "2026-09-15T20:00:00+02:00"}
    )
    if request.param == "reservation":
        return Flow(
            "reservation",
            Reservation,
            RESERVATION_CONFIRMED,
            calls=(_table(4),),
            changed=(_table(5),),
            elsewhere=_order(item_id, 2)[0],
        )
    return Flow(
        "order",
        Order,
        ORDER_CONFIRMED,
        calls=_order(item_id, 2),
        changed=_order(item_id, 3),
        elsewhere=slot,
    )


class Model:
    """Plays its tool calls, then speaks: `say`, or without one the readback of
    the tool result it just got, the way the prompt asks for it. A call may be
    a function of the state the model is shown."""

    def __init__(self, *calls, say: str | None = None):
        self._calls = list(calls)
        self._say = say
        self.inputs: list[str] = []

    def next_turn(self, system_prompt, state_json, input_text) -> LLMTurn:
        self.inputs.append(input_text)
        if self._calls:
            call = self._calls.pop(0)
            return LLMTurn(tool_call=call(state_json) if callable(call) else call)
        if self._say is not None:
            return LLMTurn(say=self._say)
        return LLMTurn(say="Gern. " + json.loads(input_text)["data"]["readback"])


def turn(session, state, text: str, model: Model) -> list[str]:
    loop = ConversationLoop(session, model, "system", now=NOW, clock=lambda: 0)
    return loop.run_turn(state, text).say


def read_out(session, state, flow: Flow) -> str:
    """One guest turn that ends with the draft read out. Returns the readback."""
    said = turn(session, state, GUEST, Model(*flow.calls))
    assert state.stage == "readback_pending"
    return said[0].removeprefix("Gern. ")


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


def test_a_draft_the_model_does_not_read_out_is_read_out_by_the_core(
    session, state, flow
):
    """The audit case of 12.10.2026: the model drafts and asks something else.
    The guest hears the readback all the same, so the yes that follows is a
    yes to it."""
    model = Model(*flow.calls, say="Möchten Sie am Fenster sitzen?")

    said = turn(session, state, GUEST, model)

    readback = json.loads(model.inputs[-1])["data"]["readback"]
    assert said == [readback]
    assert status(session, flow, flow.draft_id(state)) == "draft"


def test_a_readback_the_model_reads_out_itself_is_spoken_as_it_is(session, state, flow):
    model = Model(*flow.calls)

    said = turn(session, state, GUEST, model)

    readback = json.loads(model.inputs[-1])["data"]["readback"]
    assert said == [f"Gern. {readback}"]


def test_yes_to_the_readback_books_once(session, state, flow):
    read_out(session, state, flow)
    draft_id = flow.draft_id(state)

    turn(session, state, "Ja, passt so.", Model(flow.confirm(draft_id), say="Danke."))

    assert status(session, flow, draft_id) == "confirmed"
    assert booked(session, flow) == (1, 1)


def test_yes_after_another_answer_gets_the_readback_not_a_booking(session, state, flow):
    """The readback is no longer the last thing the guest heard: the yes may
    mean the answer in between. The model is told so and gets the readback as
    the sentence to say; what it says instead is not spoken."""
    readback = read_out(session, state, flow)
    draft_id = flow.draft_id(state)
    turn(session, state, "Darf der Hund mit?", Model(say=OTHER_ANSWER))
    model = Model(flow.confirm(draft_id), say="Ist gebucht.")

    said = turn(session, state, "Ja", model)

    assert said == [readback]
    told = json.loads(model.inputs[1])
    assert (told["ok"], told["hint"], told["say"]) == (False, HINT_CONFIRM, readback)
    assert status(session, flow, draft_id) == "draft"
    assert booked(session, flow) == (0, 0)
    logged = session.scalars(select(Call.tool_calls)).one()[-1]
    assert (logged["name"], logged["ok"]) == ("confirm", False)

    turn(session, state, "Ja", Model(flow.confirm(draft_id), say="Danke."))

    assert status(session, flow, draft_id) == "confirmed"
    assert booked(session, flow) == (1, 1)


def test_another_answer_that_ends_with_the_readback_keeps_the_question_open(
    session, state, flow
):
    readback = read_out(session, state, flow)
    draft_id = flow.draft_id(state)
    turn(session, state, "Darf der Hund mit?", Model(say=f"{OTHER_ANSWER} {readback}"))

    turn(session, state, "Ja", Model(flow.confirm(draft_id), say="Danke."))

    assert status(session, flow, draft_id) == "confirmed"


def test_no_to_the_readback_goes_back_to_the_model(session, state, flow):
    """The question was heard and answered with a no: nothing to read out
    again, the model is told why and asks what to change."""
    read_out(session, state, flow)
    draft_id = flow.draft_id(state)
    model = Model(flow.confirm(draft_id), say="Was soll ich ändern?")

    said = turn(session, state, "Nein, das stimmt nicht", model)

    assert said == ["Was soll ich ändern?"]
    assert json.loads(model.inputs[1])["hint"] == HINT_CONFIRM
    assert status(session, flow, draft_id) == "draft"


def test_yes_in_the_turn_of_the_draft_gets_the_readback(session, state, flow):
    """Draft and `confirm` in one turn: the yes in "Ja, guten Tag" came before
    anything was read out. The turn ends with the readback, and the yes to
    that one books."""
    model = Model(*flow.calls, flow.confirm_pending, say="Ist gebucht.")

    said = turn(session, state, f"Ja, guten Tag. {GUEST}", model)

    draft_id = flow.draft_id(state)
    readback = json.loads(model.inputs[len(flow.calls)])["data"]["readback"]
    assert said == [readback]
    assert status(session, flow, draft_id) == "draft"

    turn(session, state, "Ja", Model(flow.confirm(draft_id), say="Danke."))

    assert status(session, flow, draft_id) == "confirmed"
    assert booked(session, flow) == (1, 1)


def test_the_same_draft_made_again_is_read_out_again_and_stays_one(
    session, state, flow
):
    """A model that drafts the same booking once more after the question was
    lost: the tool answers with the draft it has, and the core reads it out."""
    readback = read_out(session, state, flow)
    draft_id = flow.draft_id(state)
    turn(session, state, "Darf der Hund mit?", Model(say=OTHER_ANSWER))

    said = turn(session, state, "Gut, dann so", Model(*flow.calls, say="Alles klar."))

    assert said == [readback]
    assert flow.draft_id(state) == draft_id
    assert session.scalar(select(func.count()).select_from(flow.model)) == 1

    turn(session, state, "Ja", Model(flow.confirm(draft_id), say="Danke."))

    assert status(session, flow, draft_id) == "confirmed"
    assert booked(session, flow) == (1, 1)


def test_yes_books_the_corrected_draft_never_the_one_before(session, state, flow):
    read_out(session, state, flow)
    old_id = flow.draft_id(state)
    turn(session, state, "Nein, einer mehr", Model(*flow.changed))
    new_id = flow.draft_id(state)
    assert new_id is not None and new_id != old_id

    # The model tries the draft the guest corrected first, then the new one.
    model = Model(flow.confirm(old_id), flow.confirm(new_id), say="Danke.")
    turn(session, state, "Ja", model)

    assert json.loads(model.inputs[1])["hint"] == HINT_CONFIRM
    assert status(session, flow, old_id) == "draft"
    assert status(session, flow, new_id) == "confirmed"
    assert booked(session, flow) == (1, 1)


def test_yes_after_the_guest_moved_on_books_nothing_and_reads_nothing(
    session, state, flow
):
    """A tool of the other flow drops the draft: there is no readback left to
    read out, and the old id is refused like any other."""
    read_out(session, state, flow)
    draft_id = flow.draft_id(state)
    turn(session, state, "Und noch etwas anderes", Model(flow.elsewhere, say="Gern."))
    assert flow.draft_id(state) is None

    said = turn(session, state, "Ja", Model(flow.confirm(draft_id), say="Was denn?"))

    assert said == ["Was denn?"]
    assert status(session, flow, draft_id) == "draft"
    assert booked(session, flow) == (0, 0)
