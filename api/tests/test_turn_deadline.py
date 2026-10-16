"""A guest does not wait in silence for the model beyond the restaurant's deadline (#252).

Maxi, 12.10.2026: 10 seconds until the call goes to the team (15 once the agent
can say "Bitte warten Sie einen Moment" in between, which needs the voice layer).
The value is `service_config.turn_deadline_seconds` (migration 007), read by the
phone line with the call's routing. Before this, a turn could take six model
calls of 15 seconds each before the guest heard anything.
"""

import uuid
from dataclasses import replace
from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import Session

from api.agent.llm import ChatCompletionsLLM, LLMError, LLMTurn, ToolCall
from api.agent.loop import ConversationLoop
from api.agent.state import ConversationState
from api.core.envelope import SAY_ON_FAILURE
from api.domain.calls import call_routing
from api.models import Call, Callback, ServiceConfig
from api.telephony.adapters.fake import FakeTelephony
from api.tests.test_telephony import TEAM_PHONE, handler_for
from scripts.seed import seed

BERLIN = ZoneInfo("Europe/Berlin")
DIENSTAG = date(2026, 9, 15)
CLOSED_NOW = datetime(2026, 9, 15, 8, 0, tzinfo=BERLIN)  # nobody to transfer to
OPEN_NOW = datetime(2026, 9, 15, 18, 0, tzinfo=BERLIN)  # the team answers
DEADLINE = 10


@pytest.fixture
def session(migrated_db_url):
    engine = create_engine(migrated_db_url)
    with Session(engine) as s:
        yield s
    engine.dispose()


@pytest.fixture
def tenant_id(session):
    return uuid.UUID(
        seed(session, tenant_name="Testbetrieb", timezone="Europe/Berlin").tenant_id
    )


@pytest.fixture
def state(session, tenant_id):
    call = Call(
        tenant_id=tenant_id,
        external_session_id="ext",
        started_at=OPEN_NOW,
        delete_after=DIENSTAG,
    )
    session.add(call)
    session.commit()
    return ConversationState(call_id=call.id, tenant_id=tenant_id)


class Clock:
    """Time that only moves when a test says so."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class SlowModel:
    """Plays its turns, each after the seconds named with it, and notes how long
    it was given to answer."""

    def __init__(self, clock: Clock, *turns: tuple[float, LLMTurn]) -> None:
        self._clock = clock
        self._turns = list(turns)
        self.given: list[float | None] = []

    def next_turn(self, system_prompt, state_json, input_text, *, timeout=None):
        self.given.append(timeout)
        seconds, turn = self._turns.pop(0)
        self._clock.now += seconds
        return turn


def _loop(session, model, clock, *, now=OPEN_NOW, deadline=DEADLINE):
    return ConversationLoop(
        session,
        model,
        "system",
        now=now,
        clock=clock,
        turn_deadline_seconds=deadline,
    )


def _logged_tools(session, state) -> list[str]:
    call = session.get(Call, state.call_id)
    session.refresh(call)
    return [entry["name"] for entry in call.tool_calls]


def test_an_answer_within_the_deadline_is_spoken(session, state):
    clock = Clock()
    model = SlowModel(clock, (9.0, LLMTurn(say="Gern, für wann?")))

    result = _loop(session, model, clock).run_turn(state, "Einen Tisch bitte")

    assert result.say == ["Gern, für wann?"] and result.ended is False


def test_an_answer_after_the_deadline_is_not_spoken_and_the_call_goes_to_the_team(
    session, state
):
    clock = Clock()
    model = SlowModel(clock, (11.0, LLMTurn(say="Gern, für wann?")))

    result = _loop(session, model, clock).run_turn(state, "Einen Tisch bitte")

    assert "Gern, für wann?" not in result.say
    assert result.ended is True and state.stage == "transferred"
    assert _logged_tools(session, state) == ["transfer_to_team"]


def test_a_tool_call_after_the_deadline_is_not_carried_out(session, state):
    clock = Clock()
    late = LLMTurn(tool_call=ToolCall(name="get_service_status"))
    model = SlowModel(clock, (11.0, late))

    result = _loop(session, model, clock).run_turn(state, "Habt ihr offen?")

    assert result.ended is True and state.stage == "transferred"
    assert _logged_tools(session, state) == ["transfer_to_team"]


def test_the_seconds_of_one_guest_turn_add_up_over_its_model_calls(session, state):
    """The finding: six calls of fifteen seconds each. Each call alone is within
    the deadline here, the turn is not."""
    clock = Clock()
    tool = LLMTurn(tool_call=ToolCall(name="get_service_status"))
    model = SlowModel(
        clock, (4.0, tool), (4.0, tool), (4.0, LLMTurn(say="Wir haben geöffnet."))
    )

    result = _loop(session, model, clock).run_turn(state, "Habt ihr offen?")

    assert "Wir haben geöffnet." not in result.say
    assert result.ended is True and state.stage == "transferred"
    # Asked three times, and each time given only what was left of the ten seconds.
    assert model.given == [10.0, 6.0, 2.0]


def test_the_model_is_not_asked_again_once_the_time_is_up(session, state, monkeypatch):
    clock = Clock()
    tool = LLMTurn(tool_call=ToolCall(name="get_service_status"))
    model = SlowModel(clock, (4.0, tool), (1.0, LLMTurn(say="Wir haben geöffnet.")))
    loop = _loop(session, model, clock)
    dispatch = loop._dispatch

    def slow_tool(state, name, args):
        if name == "get_service_status":
            clock.now += 7.0
        return dispatch(state, name, args)

    monkeypatch.setattr(loop, "_dispatch", slow_tool)

    result = loop.run_turn(state, "Habt ihr offen?")

    assert len(model.given) == 1
    assert result.ended is True and state.stage == "transferred"


def test_every_guest_turn_has_its_own_deadline(session, state):
    clock = Clock()
    model = SlowModel(
        clock,
        (8.0, LLMTurn(say="Gern, für wann?")),
        (8.0, LLMTurn(say="Und für wie viele Personen?")),
    )
    loop = _loop(session, model, clock)

    first = loop.run_turn(state, "Einen Tisch bitte")
    second = loop.run_turn(state, "Morgen um sieben")

    assert first.say == ["Gern, für wann?"]
    assert second.say == ["Und für wie viele Personen?"]


def test_what_the_guest_hears_is_the_outage_sentence_when_no_tool_has_one(
    session, state, monkeypatch
):
    """The slow model is an outage for the guest: the sentence of every other
    one (docs/02 failure behaviour), never a new excuse."""
    clock = Clock()
    model = SlowModel(clock, (11.0, LLMTurn(say="Gern, für wann?")))
    loop = _loop(session, model, clock)
    dispatch = loop._dispatch

    def silent_transfer(state, name, args):
        return replace(dispatch(state, name, args), say=None)

    monkeypatch.setattr(loop, "_dispatch", silent_transfer)

    assert loop.run_turn(state, "Einen Tisch bitte").say == [SAY_ON_FAILURE]


def test_a_number_heard_in_the_late_answer_still_reaches_the_callback(session, state):
    """Nobody to transfer to and a withheld caller ID: the number the guest said
    in this very sentence is all that saves the call (rule 5). It is taken
    before the deadline ends the turn, as before the call limit does."""
    clock = Clock()
    late = LLMTurn(say="Gern.", state_patch={"phone": "+4972215551234"})
    model = SlowModel(clock, (11.0, late))

    result = _loop(session, model, clock, now=CLOSED_NOW).run_turn(
        state, "Meine Nummer ist 07221 5551234"
    )

    assert result.ended is True and state.stage == "callback"
    assert session.scalars(select(Callback)).one().phone == "+4972215551234"


def test_without_a_deadline_nothing_changes(session, state):
    """The text phone and the evals have no guest on the line: no deadline, and a
    model client of the old shape is called as before."""

    class OldShape:
        def next_turn(self, system_prompt, state_json, input_text):
            return LLMTurn(say="Gern, für wann?")

    clock = Clock()
    clock.now = 1000.0
    loop = _loop(session, OldShape(), clock, deadline=None)

    assert loop.run_turn(state, "Einen Tisch bitte").say == ["Gern, für wann?"]


# -- the model client -------------------------------------------------------------


def _client(seen: list, *, timeout: float = 15.0) -> ChatCompletionsLLM:
    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request.extensions["timeout"])
        content = '{"say": "Gern."}'
        return httpx.Response(
            200, json={"choices": [{"message": {"content": content}}]}
        )

    return ChatCompletionsLLM(
        "http://model.test/v1",
        "test-model",
        timeout=timeout,
        transport=httpx.MockTransport(answer),
    )


def test_the_client_cuts_a_request_off_after_the_time_it_is_given():
    seen: list = []

    _client(seen).next_turn("system", {}, "Hallo", timeout=4.0)

    assert set(seen[0].values()) == {4.0}


def test_the_client_never_waits_longer_than_its_own_timeout():
    """LLM_TIMEOUT_SECONDS stays the upper limit: a deadline of ten seconds does
    not stretch a client that is set to five."""
    seen: list = []
    client = _client(seen, timeout=5.0)

    client.next_turn("system", {}, "Hallo", timeout=10.0)
    client.next_turn("system", {}, "Hallo")

    assert set(seen[0].values()) == {5.0} and set(seen[1].values()) == {5.0}


def test_a_request_that_runs_out_of_time_is_a_model_failure():
    def too_slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("no answer in time", request=request)

    client = ChatCompletionsLLM(
        "http://model.test/v1", "test-model", transport=httpx.MockTransport(too_slow)
    )

    with pytest.raises(LLMError):
        client.next_turn("system", {}, "Hallo", timeout=4.0)


# -- the phone line -------------------------------------------------------------


def test_the_routing_of_a_call_carries_the_tenants_deadline(session, tenant_id):
    assert call_routing(session, tenant_id).turn_deadline_seconds == 10

    session.execute(
        update(ServiceConfig)
        .where(ServiceConfig.tenant_id == tenant_id)
        .values(turn_deadline_seconds=15)
    )
    session.commit()

    assert call_routing(session, tenant_id).turn_deadline_seconds == 15


ONE_SENTENCE = {
    "caller_id": "0721 5551234",
    "transcript": [{"role": "customer", "text": "Einen Tisch bitte."}],
}


@pytest.fixture
def line(session, tenant_id):
    """The tenant as the phone line tests know it: the agent answers first, and
    the team has a number to transfer to."""
    session.execute(
        update(ServiceConfig)
        .where(ServiceConfig.tenant_id == tenant_id)
        .values(call_mode="primary", team_phone=TEAM_PHONE, turn_deadline_seconds=7)
    )
    session.commit()
    return SimpleNamespace(id=tenant_id, timezone="Europe/Berlin", name="Testbetrieb")


def test_the_phone_line_gives_the_model_the_tenants_deadline(session, line):
    given = []

    class Listening:
        def next_turn(self, system_prompt, state_json, input_text, *, timeout=None):
            given.append(timeout)
            return LLMTurn(say="Auf Wiederhören.")

    port = FakeTelephony()
    port.play(
        ONE_SENTENCE,
        handler_for(
            session, line, port, llm_factory=lambda s: Listening(), clock=Clock()
        ),
    )

    assert given == [7]


def test_a_model_that_keeps_the_guest_waiting_on_the_phone_loses_the_call_to_the_team(
    session, line
):
    clock = Clock()

    class Slow:
        def next_turn(self, system_prompt, state_json, input_text, *, timeout=None):
            clock.now += 8.0
            return LLMTurn(say="Gern, für wann?")

    port = FakeTelephony()
    sid = port.play(
        ONE_SENTENCE,
        handler_for(session, line, port, llm_factory=lambda s: Slow(), clock=clock),
    )

    assert port.transfers(sid) == [TEAM_PHONE]
    assert "Gern, für wann?" not in port.says(sid)


class Closable:
    """A model client that holds something to close, like the real one."""

    def __init__(self, turn: LLMTurn | Exception) -> None:
        self._turn = turn
        self.closed = 0

    def next_turn(self, system_prompt, state_json, input_text, *, timeout=None):
        if isinstance(self._turn, Exception):
            raise self._turn
        return self._turn

    def close(self) -> None:
        self.closed += 1


@pytest.mark.parametrize(
    "turn",
    [
        LLMTurn(say="Gern, für wann?"),  # the guest hangs up after the answer
        LLMTurn(say="Auf Wiederhören."),  # the agent ends the call
        LLMTurn(tool_call=ToolCall(name="transfer_to_team", args={"reason": "x"})),
        RuntimeError("model endpoint down"),  # the outage path
    ],
    ids=["guest hangs up", "agent hangs up", "transfer", "failure"],
)
def test_the_model_client_is_closed_with_the_call(session, line, turn):
    """One client per call (`llm_factory`), and the real one holds a connection
    pool: unclosed, they pile up over a day of calls (#252)."""
    made = []

    def factory(s):
        made.append(Closable(turn))
        return made[-1]

    port = FakeTelephony()
    port.play(ONE_SENTENCE, handler_for(session, line, port, llm_factory=factory))

    assert [client.closed for client in made] == [1]


def test_a_client_that_cannot_be_closed_does_not_fail_the_call(session, line):
    class Stubborn(Closable):
        def close(self) -> None:
            raise OSError("socket gone")

    port = FakeTelephony()
    sid = port.play(
        ONE_SENTENCE,
        handler_for(
            session,
            line,
            port,
            llm_factory=lambda s: Stubborn(LLMTurn(say="Gern, für wann?")),
        ),
    )

    # No outage sentence and no transfer: the call had ended in good order.
    assert port.says(sid)[-1] == "Gern, für wann?" and port.transfers(sid) == []
