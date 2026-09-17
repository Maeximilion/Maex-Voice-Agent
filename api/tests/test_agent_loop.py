"""agent/loop.py: Kundenzug -> Modell -> Tool-Aufrufe -> Antwort, Abbruch bei Zeit/Hops.

Nutzt eine echte Sitzung wie die Domain-Tests, weil `dispatch()` jeden Aufruf --
auch einen mit unbekanntem Toolnamen -- in `calls.tool_calls` protokolliert
(docs/04 §Gemeinsame Regeln) und dafür eine echte `calls`-Zeile braucht.
"""

import uuid
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from api.agent.llm import FakeLLM, LLMTurn, ToolCall
from api.agent.loop import MAX_TOOL_HOPS, SAY_STUCK, SAY_TIMEOUT, ConversationLoop
from api.agent.state import ConversationState
from api.models import Call, Callback
from scripts.seed import seed

BERLIN = ZoneInfo("Europe/Berlin")
DIENSTAG = date(2026, 9, 15)
NOW = datetime(2026, 9, 15, 8, 0, tzinfo=BERLIN)  # vor Öffnung: Team nicht erreichbar
OPEN_NOW = datetime(
    2026, 9, 15, 18, 0, tzinfo=BERLIN
)  # im Abendfenster: Team erreichbar


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
    loop = ConversationLoop(session, llm, "system", now=NOW, clock=clock_from([0, 0]))

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
    loop = ConversationLoop(
        session, llm, "system", now=NOW, clock=clock_from([0, 0, 0])
    )

    result = loop.run_turn(state, "Habt ihr offen?")

    assert result.say == ["Wir haben aktuell geöffnet."]
    # zweiter next_turn-Aufruf bekam das Tool-Ergebnis statt des ursprünglichen Kundentexts
    second_input = llm.calls[1][2]
    assert "get_service_status" in second_input
    assert '"ok": true' in second_input


def test_state_patch_wird_in_die_slots_uebernommen(session, state):
    """Ohne das würde eine über mehrere Züge gesammelte Personenzahl/Name/Datum
    beim nächsten Zug verloren gehen, weil das Modell nur den kompakten Zustand
    sieht, nie den Verlauf (Codex-Review PR #101, P1)."""
    llm = FakeLLM(
        [
            LLMTurn(
                tool_call=ToolCall(name="get_service_status"),
                state_patch={"guest_name": "Müller", "party_size": 4},
            ),
            LLMTurn(say="Für wann darf ich reservieren?"),
        ]
    )
    loop = ConversationLoop(
        session, llm, "system", now=NOW, clock=clock_from([0, 0, 0])
    )

    loop.run_turn(state, "Ich bin die Müller, wir sind zu viert")

    assert state.slots == {"guest_name": "Müller", "party_size": 4}
    # der zweite next_turn-Aufruf sieht die Slots bereits im kompakten Zustand
    assert llm.calls[1][1]["slots"] == {"guest_name": "Müller", "party_size": 4}


def test_tool_say_wird_dem_modell_zurueckgegeben(session, state):
    """`say` transportiert die vorgeschriebene Formulierung heikler Fälle aus dem
    Code (docs/05 §5); ohne sie müsste das Modell aus dem bloßen error_code selbst
    improvisieren (Codex-Review PR #101, P2)."""
    llm = FakeLLM(
        [
            LLMTurn(tool_call=ToolCall(name="get_service_status")),
            LLMTurn(say="ok"),
        ]
    )
    loop = ConversationLoop(
        session, llm, "system", now=NOW, clock=clock_from([0, 0, 0])
    )

    loop.run_turn(state, "Habt ihr offen?")

    second_input = llm.calls[1][2]
    # NOW liegt vor Öffnung: get_service_status liefert ein `say` mit der nächsten Öffnungszeit.
    assert '"say"' in second_input


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
    loop = ConversationLoop(
        session, llm, "system", now=OPEN_NOW, clock=clock_from([0, 0, 0])
    )

    # bewusst kein Auslösewort aus escalation.py: dieser Test prüft den Weg über
    # einen vom Modell selbst gewählten Tool-Aufruf, nicht die Vorab-Prüfung.
    result = loop.run_turn(state, "Da gibt es leider ein Problem mit meiner Bestellung")

    assert result.ended is True
    assert state.stage == "transferred"


def test_max_call_seconds_uebergibt_wirklich_statt_nur_anzukuendigen(session, state):
    """Vorher endete der Anruf nach dem Zeitlimit mit einer Übergabe-Ankündigung,
    ohne dass `transfer_to_team`/`create_callback` je aufgerufen wurde -- die
    Telefonanlage hätte einfach aufgelegt (Codex-Review PR #101, P1)."""
    llm = FakeLLM([])  # darf gar nicht erst aufgerufen werden
    loop = ConversationLoop(
        session,
        llm,
        "system",
        now=OPEN_NOW,
        max_call_seconds=10,
        clock=clock_from([0, 100]),
    )

    result = loop.run_turn(state, "Hallo")

    assert result.ended is True
    assert llm.calls == []
    assert state.stage == "transferred"
    assert state.transferred is True


def test_max_call_seconds_ohne_erreichbares_team_legt_rueckruf_an(session, state):
    state.slots["phone"] = "+4972215551234"
    llm = FakeLLM([])
    loop = ConversationLoop(
        session, llm, "system", now=NOW, max_call_seconds=10, clock=clock_from([0, 100])
    )

    result = loop.run_turn(state, "Hallo")

    assert result.ended is True
    assert state.stage == "callback"


def test_max_call_seconds_ohne_telefon_bleibt_beim_ehrlichen_fallback_satz(
    session, state
):
    """Ohne bekannte Rufnummer kann kein Rückruf angelegt werden -- raten statt
    dessen wäre CLAUDE.md §2 Regel 2 (nie raten)."""
    llm = FakeLLM([])
    loop = ConversationLoop(
        session, llm, "system", now=NOW, max_call_seconds=10, clock=clock_from([0, 100])
    )

    result = loop.run_turn(state, "Hallo")

    assert result.ended is True
    assert result.say == [SAY_TIMEOUT]
    assert state.stage == "ended"


def test_beschwerde_eskaliert_sofort_ohne_modellaufruf(session, state):
    """escalation.py prüft vor dem Modell (docs/11 §agent): ein eindeutiges
    Auslösewort ruft nie erst das Modell auf."""
    llm = FakeLLM([])  # darf gar nicht erst aufgerufen werden
    loop = ConversationLoop(session, llm, "system", now=OPEN_NOW, clock=clock_from([0]))

    result = loop.run_turn(state, "Ich möchte mich beschweren!")

    assert llm.calls == []
    assert result.ended is True
    assert state.stage == "transferred"


def test_storno_ohne_erreichbares_team_legt_rueckruf_mit_gueltigem_grund_an(
    session, state
):
    """`cancellation` ist ein gültiger Transfer-Grund, aber kein gültiger
    Rückruf-Grund (docs/03) -- der Fallback muss trotzdem einen Rückruf anlegen
    können statt an der Validierung zu scheitern."""
    state.slots["phone"] = "+4972215551234"
    llm = FakeLLM([])
    loop = ConversationLoop(session, llm, "system", now=NOW, clock=clock_from([0]))

    result = loop.run_turn(state, "Ich möchte stornieren")

    assert result.ended is True
    assert state.stage == "callback"


def test_ladder_erschoepft_eskaliert_ohne_dass_das_modell_selbst_zaehlt(session, state):
    """Nach drei Stufenwechseln ohne Erfolg an derselben Information -- über
    sechs Kundenzüge, nicht innerhalb eines einzigen -- übernimmt `loop.py`
    die Eskalation selbst (docs/05 §2); das Modell muss nur jeden Fehlversuch
    melden, nicht mitzählen."""
    turns = [
        LLMTurn(say="Wie bitte?", understanding_failure="reserved_for")
        for _ in range(6)
    ]
    llm = FakeLLM(turns)
    loop = ConversationLoop(session, llm, "system", now=NOW, clock=clock_from([0] * 7))

    results = [loop.run_turn(state, "Am Dingsbums um irgendwann") for _ in range(6)]

    assert [r.ended for r in results[:5]] == [False] * 5
    assert results[5].ended is True
    assert state.stage == "ended"


def test_ladder_gibt_dem_modell_einen_stufen_hinweis(session, state):
    llm = FakeLLM(
        [
            LLMTurn(say="Wie bitte?", understanding_failure="party_size"),
            LLMTurn(say="Und für wann?", understanding_failure="party_size"),
            LLMTurn(say="Okay."),
        ]
    )
    loop = ConversationLoop(
        session, llm, "system", now=NOW, clock=clock_from([0, 0, 0, 0])
    )

    loop.run_turn(state, "äh, keine Ahnung")
    loop.run_turn(state, "wie bitte")
    loop.run_turn(state, "irgendwas")

    # nach zwei Fehlversuchen an party_size sieht der dritte Zug den Stufen-Hinweis
    assert llm.calls[2][1]["ladder"] == {"party_size": 2}


def test_erfolg_loescht_den_ladder_stand_des_feldes(session, state):
    llm = FakeLLM(
        [
            LLMTurn(say="Wie bitte?", understanding_failure="party_size"),
            LLMTurn(say="Wie bitte?", understanding_failure="party_size"),
            LLMTurn(say="Verstanden.", state_patch={"party_size": 4}),
        ]
    )
    loop = ConversationLoop(
        session, llm, "system", now=NOW, clock=clock_from([0, 0, 0, 0])
    )

    loop.run_turn(state, "äh")
    loop.run_turn(state, "wie bitte")
    loop.run_turn(state, "vier Personen")

    assert loop._ladder.level_for("party_size") == 1


def test_understanding_failure_zaehlt_nur_einmal_pro_kundenzug(session, state):
    """Zwei Meldungen desselben Felds innerhalb eines Zugs (erst beim Tool-Hop,
    dann in der Antwort) sind ein Kundenversuch, keine zwei -- sonst könnte die
    Leiter schneller hochklettern, als der Kunde tatsächlich etwas gesagt hat
    (Codex-Review PR #102, P2)."""
    llm = FakeLLM(
        [
            LLMTurn(
                tool_call=ToolCall(name="get_service_status"),
                understanding_failure="party_size",
            ),
            LLMTurn(
                say="Wie viele Personen sind Sie?", understanding_failure="party_size"
            ),
        ]
    )
    loop = ConversationLoop(
        session, llm, "system", now=NOW, clock=clock_from([0, 0, 0])
    )

    loop.run_turn(state, "hm, weiß nicht genau")

    assert loop._ladder.level_for("party_size") == 1


def test_eskalations_rueckruf_enthaelt_den_kundentext(session, state):
    """Bei einer Vorab-Eskalation läuft nie das Modell, also gibt es sonst
    keine Aufzeichnung dessen, was der Kunde eigentlich wollte -- eine feste
    Floskel allein lässt das Team ohne die nötigen Angaben zurück (Codex-Review
    PR #102, P2)."""
    state.slots["phone"] = "+4972215551234"
    llm = FakeLLM([])
    loop = ConversationLoop(session, llm, "system", now=NOW, clock=clock_from([0]))

    result = loop.run_turn(state, "Reservierung morgen um 18 Uhr auf Müller stornieren")

    assert result.ended is True
    assert state.stage == "callback"
    callback = session.scalars(select(Callback)).one()
    assert "Reservierung morgen um 18 Uhr auf Müller stornieren" in callback.summary


def test_zu_viele_tool_hops_brechen_sauber_ab(session, state):
    turns = [
        LLMTurn(tool_call=ToolCall(name="unbekanntes_tool"))
        for _ in range(MAX_TOOL_HOPS)
    ]
    llm = FakeLLM(turns)
    loop = ConversationLoop(
        session, llm, "system", now=NOW, clock=clock_from([0] * (MAX_TOOL_HOPS + 1))
    )

    result = loop.run_turn(state, "Hallo")

    assert result.ended is True
    assert result.say == [SAY_STUCK]
    assert len(llm.calls) == MAX_TOOL_HOPS
    assert state.stage == "ended"
