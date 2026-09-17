"""agent/loop.py: Kundenzug -> Modell -> Tool-Aufrufe -> Antwort, Abbruch bei Zeit/Hops.

Nutzt eine echte Sitzung wie die Domain-Tests, weil `dispatch()` jeden Aufruf --
auch einen mit unbekanntem Toolnamen -- in `calls.tool_calls` protokolliert
(docs/04 §Gemeinsame Regeln) und dafür eine echte `calls`-Zeile braucht.
"""

import uuid
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from api.agent.llm import FakeLLM, LLMTurn, ToolCall
from api.agent.loop import MAX_TOOL_HOPS, SAY_STUCK, SAY_TIMEOUT, ConversationLoop
from api.agent.state import ConversationState
from api.models import Call
from scripts.seed import seed

BERLIN = ZoneInfo("Europe/Berlin")
DIENSTAG = date(2026, 9, 15)
NOW = datetime(2026, 9, 15, 8, 0, tzinfo=BERLIN)


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
def call_id(session, tenant_id):
    call = Call(
        tenant_id=tenant_id,
        external_session_id="ext",
        started_at=NOW,
        delete_after=DIENSTAG,
    )
    session.add(call)
    session.commit()
    return call.id


@pytest.fixture
def state(tenant_id, call_id):
    return ConversationState(call_id=call_id, tenant_id=tenant_id)


def clock_from(values):
    it = iter(values)
    return lambda: next(it)


def test_direkte_modell_antwort_ohne_tool_aufruf(session, state):
    llm = FakeLLM([LLMTurn(say="Guten Tag, hier spricht der Assistent.")])
    loop = ConversationLoop(session, llm, "system", clock=clock_from([0, 0]))

    result = loop.run_turn(state, "Hallo")

    assert result.say == ["Guten Tag, hier spricht der Assistent."]
    assert result.ended is False


def test_tool_aufruf_wird_ausgefuehrt_und_ergebnis_zurueckgefuettert(session, state):
    llm = FakeLLM(
        [
            LLMTurn(tool_call=ToolCall(name="get_service_status")),
            LLMTurn(say="Wir haben aktuell geöffnet."),
        ]
    )
    loop = ConversationLoop(session, llm, "system", clock=clock_from([0, 0, 0]))

    result = loop.run_turn(state, "Habt ihr offen?")

    assert result.say == ["Wir haben aktuell geöffnet."]
    # zweiter next_turn-Aufruf bekam das Tool-Ergebnis statt des ursprünglichen Kundentexts
    second_input = llm.calls[1][2]
    assert "get_service_status" in second_input
    assert '"ok": true' in second_input


def test_transfer_erfolgreich_beendet_das_gespraech(session, state):
    llm = FakeLLM(
        [
            LLMTurn(
                tool_call=ToolCall(
                    name="transfer_to_team", args={"reason": "complaint"}
                )
            ),
            LLMTurn(say="Ich verbinde Sie."),
        ]
    )
    loop = ConversationLoop(session, llm, "system", clock=clock_from([0, 0, 0]))

    result = loop.run_turn(state, "Ich will jemanden sprechen")

    assert result.ended is True
    assert state.stage == "transferred"


def test_max_call_seconds_bricht_sofort_ab_ohne_modellaufruf(session, state):
    llm = FakeLLM([])  # darf gar nicht erst aufgerufen werden
    loop = ConversationLoop(
        session, llm, "system", max_call_seconds=10, clock=clock_from([0, 100])
    )

    result = loop.run_turn(state, "Hallo")

    assert result.ended is True
    assert result.say == [SAY_TIMEOUT]
    assert llm.calls == []
    assert state.stage == "ended"


def test_zu_viele_tool_hops_brechen_sauber_ab(session, state):
    turns = [
        LLMTurn(tool_call=ToolCall(name="unbekanntes_tool"))
        for _ in range(MAX_TOOL_HOPS)
    ]
    llm = FakeLLM(turns)
    loop = ConversationLoop(
        session, llm, "system", clock=clock_from([0] * (MAX_TOOL_HOPS + 1))
    )

    result = loop.run_turn(state, "Hallo")

    assert result.ended is True
    assert result.say == [SAY_STUCK]
    assert len(llm.calls) == MAX_TOOL_HOPS
