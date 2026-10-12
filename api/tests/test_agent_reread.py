"""Rule 3 in the loop: after a refused yes the core reads the draft out itself.

A yes for a draft that is not what the guest heard last is refused
(`guards.py`, #251). What comes next is not left to the model: the refusal
carries the draft's sentence, and the turn ends with it whatever the model
says. The yes to that one books. Every test runs for a table and for an
order, on the database, the audit log and the outbox.
"""

import json
import uuid
from dataclasses import dataclass
from typing import Any

import pytest
from sqlalchemy import create_engine, func, select, update
from sqlalchemy.orm import Session

from api.agent.dispatch import ToolResult
from api.agent.guards import (
    HINT_CONFIRM,
    HINT_REREAD,
    begin_turn,
    note_said,
    refusal,
    reread,
)
from api.agent.llm import LLMTurn, ToolCall
from api.agent.loop import MAX_TOOL_HOPS, ConversationLoop
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
    the way the prompt asks for it. A step may be a whole answer."""

    def __init__(self, *steps: ToolCall | LLMTurn, say: str | None = None):
        self._steps = list(steps)
        self._say = say
        self.inputs: list[str] = []

    def next_turn(self, system_prompt, state_json, input_text) -> LLMTurn:
        self.inputs.append(input_text)
        if self._steps:
            step = self._steps.pop(0)
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


def test_yes_after_another_answer_gets_the_draft_read_out_not_a_booking(
    session, state, flow
):
    """The read-back is no longer the last thing the guest heard: the yes may
    mean the answer in between. The model is told so and gets the sentence to
    say; what it says instead is not spoken. The yes after that books, once."""
    readback = read_to_the_guest(session, state, flow)
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


def test_a_model_that_says_the_sentence_it_was_given_is_spoken_as_it_is(
    session, state, flow
):
    readback = read_to_the_guest(session, state, flow)
    draft_id = flow.draft_id(state)
    turn(session, state, "Darf der Hund mit?", Model(say=OTHER_ANSWER))

    said = turn(session, state, "Ja", Model(flow.confirm(draft_id)))

    assert said == [readback]
    assert state.readback_heard


def test_no_after_another_answer_is_left_to_the_model(session, state, flow):
    """The draft is put again only on a yes. A guest who wants a change is
    asked what, not the old question once more."""
    read_to_the_guest(session, state, flow)
    draft_id = flow.draft_id(state)
    turn(session, state, "Darf der Hund mit?", Model(say=OTHER_ANSWER))
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
    assert "say" not in json.loads(model.inputs[1])
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
        # Heard and answered with something else than a yes: nothing to repeat.
        (True, "Nein, das stimmt nicht", None),
    ],
)
def test_only_a_refused_yes_for_an_unheard_draft_carries_its_sentence(
    heard, said, sentence
):
    state, confirm = drafted(heard)

    refused = refusal(state, begin_turn(state), said, confirm)

    assert refused is not None and refused.hint == HINT_CONFIRM
    assert refused.say == sentence


def test_a_refused_yes_without_any_draft_carries_no_sentence():
    state = ConversationState(call_id=uuid.uuid4(), tenant_id=uuid.uuid4())
    confirm = ToolCall("confirm", {"entity": "order", "entity_id": str(uuid.uuid4())})

    assert refusal(state, begin_turn(state), "Ja", confirm).say is None


@pytest.mark.parametrize(
    ("say", "spoken"),
    [
        ("Ist gebucht.", READBACK),
        ("Ein Tisch für vier um sieben. Passt das so?", READBACK),
        (READBACK, READBACK),
        (f"Gerne. {READBACK}", f"Gerne. {READBACK}"),
    ],
)
def test_reread_keeps_a_sentence_that_reads_the_draft_out_and_replaces_any_other(
    say, spoken
):
    state, _ = drafted(heard=False)

    assert reread(state, say) == spoken


def test_reread_leaves_the_sentence_alone_when_no_draft_waits_unheard():
    heard, _ = drafted(heard=True)
    nothing = ConversationState(call_id=uuid.uuid4(), tenant_id=uuid.uuid4())

    assert reread(heard, "Ja, Hunde sind willkommen.") == "Ja, Hunde sind willkommen."
    assert reread(nothing, "Guten Tag.") == "Guten Tag."


# --- nothing else is carried out before the draft is read out (Codex PR #273) ---

TRANSFER = ToolCall("transfer_to_team", {"reason": "not_understood"})


def refused_yes(session, state, flow: Flow) -> tuple[str, uuid.UUID]:
    """A draft that was read out, then another answer: the next yes is refused."""
    readback = read_to_the_guest(session, state, flow)
    turn(session, state, "Darf der Hund mit?", Model(say=OTHER_ANSWER))
    draft_id = flow.draft_id(state)
    assert draft_id is not None
    return readback, draft_id


def test_a_transfer_after_the_refused_yes_is_not_carried_out(session, state, flow):
    """The model answers the refusal with a transfer, as the stand-in did. The
    guest said yes and nothing else: the call stays, the draft is read out."""
    readback, draft_id = refused_yes(session, state, flow)
    model = Model(flow.confirm(draft_id), TRANSFER, say="Ich verbinde Sie.")

    said = turn(session, state, "Ja", model)

    assert said == [readback]
    told = json.loads(model.inputs[2])
    assert (told["tool"], told["ok"]) == ("transfer_to_team", False)
    assert (told["hint"], told["say"]) == (HINT_REREAD, readback)
    assert state.stage == "readback_pending" and not state.transferred
    assert status(session, flow, draft_id) == "draft"

    turn(session, state, "Ja", Model(flow.confirm(draft_id), say="Danke."))

    assert status(session, flow, draft_id) == "confirmed"
    assert booked(session, flow) == (1, 1)


def test_a_second_confirm_after_the_refused_yes_books_nothing(session, state, flow):
    readback, draft_id = refused_yes(session, state, flow)
    again = flow.confirm(draft_id)

    said = turn(session, state, "Ja", Model(again, again, say="Ist gebucht."))

    assert said == [readback]
    assert status(session, flow, draft_id) == "draft"
    assert booked(session, flow) == (0, 0)


def test_a_model_that_returns_only_tools_ends_with_the_team(session, state, flow):
    """The last resort stays what it is for any stuck turn (rule 5): the hop
    limit, then the team. Nothing is booked on the way."""
    _, draft_id = refused_yes(session, state, flow)
    model = Model(flow.confirm(draft_id), *[TRANSFER] * MAX_TOOL_HOPS)
    loop = ConversationLoop(session, model, "system", now=NOW, clock=lambda: 0)

    result = loop.run_turn(state, "Ja")

    assert result.ended is True and state.transferred
    assert status(session, flow, draft_id) == "draft"
    assert booked(session, flow) == (0, 0)


HELD_BACK = {
    "search": ToolCall("search_menu", {"query": "die 23"}),
    "slot": ToolCall(
        "check_slot", {"party_size": 2, "reserved_for": "2026-09-15T20:00:00+02:00"}
    ),
}


@pytest.mark.parametrize("tried", ["search", "slot", "draft"])
def test_a_tool_held_back_for_the_reread_does_not_drop_the_draft(
    session, state, flow, tried
):
    """A search, a slot check or a new draft is how a correction begins, and
    a failed one drops the draft that waits (`state.apply_tool_result`). Held
    back after a bare yes it is none: the draft stays, is read out, and the
    yes to it books (Codex PR #273)."""
    readback, draft_id = refused_yes(session, state, flow)
    call = flow.calls[-1] if tried == "draft" else HELD_BACK[tried]
    model = Model(flow.confirm(draft_id), call, say="Einen Moment.")

    said = turn(session, state, "Ja", model)

    assert said == [readback]
    assert json.loads(model.inputs[2])["hint"] == HINT_REREAD
    assert flow.draft_id(state) == draft_id and state.stage == "readback_pending"
    assert session.scalar(select(func.count()).select_from(flow.model)) == 1

    turn(session, state, "Ja", Model(flow.confirm(draft_id), say="Danke."))

    assert status(session, flow, draft_id) == "confirmed"
    assert booked(session, flow) == (1, 1)


@pytest.mark.parametrize(
    "beside",
    [
        {"cart": [{"menu_item_id": str(uuid.uuid4()), "quantity": 1}]},
        {"state_patch": {"phone": CALLER}},
    ],
    ids=["cart", "patch"],
)
def test_what_stands_next_to_the_answer_is_not_taken_before_the_reread(
    session, state, flow, beside
):
    """A changed or refused `cart` drops the draft that waits, and so does a
    phone number in a patch. After a bare yes the model's answer carries one:
    it is left aside, the draft stays and is read out (Codex PR #273)."""
    readback, draft_id = refused_yes(session, state, flow)
    answer = LLMTurn(say="Ich habe etwas geändert.", **beside)

    said = turn(session, state, "Ja", Model(flow.confirm(draft_id), answer))

    assert said == [readback]
    assert flow.draft_id(state) == draft_id and state.stage == "readback_pending"

    turn(session, state, "Ja", Model(flow.confirm(draft_id), say="Danke."))

    assert status(session, flow, draft_id) == "confirmed"
    assert booked(session, flow) == (1, 1)
