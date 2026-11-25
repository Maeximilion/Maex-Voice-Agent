"""agent/loop.py: Kundenzug -> Modell -> Tool-Aufrufe -> Antwort, Abbruch bei Zeit/Hops.

Nutzt eine echte Sitzung wie die Domain-Tests, weil `dispatch()` jeden Aufruf --
auch einen mit unbekanntem Toolnamen -- in `calls.tool_calls` protokolliert
(docs/04 §Gemeinsame Regeln) und dafür eine echte `calls`-Zeile braucht.
"""

import json
import uuid
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import Session

from api.agent.guards import (
    HINT_CALLBACK_PHONE,
    HINT_CONFIRM,
    HINT_GUEST_NAME,
    HINT_GUEST_PHONE,
    HINT_ITEM,
    HINT_NOT_BOOKED,
    SAY_ALLERGY_NOTED,
    note_said,
    note_unheard,
)
from api.agent.llm import FakeLLM, LLMError, LLMTurn, ToolCall
from api.agent.loop import (
    MAX_TOOL_HOPS,
    SAY_BOOKED_GOODBYE,
    SAY_CALLBACK_NUMBER,
    SAY_ESCALATION,
    SAY_NOBODY_REACHABLE,
    SAY_NOT_A_PERSON,
    SAY_ORDER_NOT_PLACED,
    SAY_ORDER_PLACED,
    SAY_TABLE_BOOKED,
    SAY_TABLE_NOT_BOOKED,
    SAY_TEAM_OFFER,
    SUMMARY_DELIVERY,
    SUMMARY_DELIVERY_NO_ADDRESS,
    SUMMARY_DELIVERY_STORED,
    SUMMARY_FLOOR_NOTE_TO_CLEAR,
    SUMMARY_NOTE_TO_CLEAR,
    SUMMARY_OPEN_ORDER,
    SUMMARY_OPEN_TABLE,
    SUMMARY_OUTAGE,
    SUMMARY_REFUSED_TABLE,
    SUMMARY_TABLE_UNREAD,
    ConversationLoop,
    TurnResult,
    _booked_say,
    _callback_reason_refused,
    _card_without_allergy,
    _handoff_summary,
    _judged_again,
    _local_times,
    _note_allergy_said,
    _order_lines,
    _still_open,
    _with_open_order,
)
from api.agent.outcome import call_outcome
from api.agent.state import ALLERGY_SAID_MAX, ConversationState, initial_state
from api.domain.confirm import SAY_CANCELLED
from api.models import Call, Callback, Reservation, ServiceConfig
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


CALLER = "+4972215551234"


@pytest.fixture
def state(tenant_id, call_id):
    """The core knows the number the guest calls from; `slots` stays empty, so
    each test decides what the model was given."""
    return ConversationState(call_id=call_id, tenant_id=tenant_id, caller_phone=CALLER)


def clock_from(values):
    """Plays the values, then stays on the last one: the loop reads the clock
    before and after every model call, a test only names the moments it means."""
    it = iter(values)
    return lambda: next(it, values[-1])


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
            # Never asked for: a real model called transfer_to_team again on
            # its own result until the hop limit (13.10.2026).
            LLMTurn(tool_call=ToolCall(name="transfer_to_team", args={})),
        ]
    )
    loop = ConversationLoop(
        session, llm, "system", now=OPEN_NOW, clock=clock_from([0, 0, 0])
    )

    # bewusst kein Auslösewort aus escalation.py: dieser Test prüft den Weg über
    # einen vom Modell selbst gewählten Tool-Aufruf, nicht die Vorab-Prüfung.
    result = loop.run_turn(state, "Da gibt es leider ein Problem mit meiner Bestellung")

    assert result.ended is True
    assert result.say == [SAY_ESCALATION]
    assert state.stage == "transferred"
    assert len(llm.calls) == 1
    # The card for a team leg nobody answers, as on the core's own handover.
    assert state.handoff_summary == (
        "Kunde hat sich beschwert. Kunde sagte: "
        "Da gibt es leider ein Problem mit meiner Bestellung"
    )


def _model_asks_for_the_team() -> FakeLLM:
    """Hands the call over itself. Asked again, it would only announce it."""
    return FakeLLM(
        [
            LLMTurn(
                tool_call=ToolCall(
                    name="transfer_to_team", args={"reason": "human_requested"}
                )
            ),
            LLMTurn(say="Ich verbinde Sie mit dem Team."),
        ]
    )


def test_a_transfer_of_the_model_without_the_team_becomes_a_callback(session, state):
    """Before opening nobody takes the call. The result went back to the
    model, and one that answered with a handover sentence left the guest with
    neither a transfer nor a card (#655): the core writes the card itself."""
    state.slots["phone"] = CALLER
    llm = _model_asks_for_the_team()
    loop = ConversationLoop(session, llm, "system", now=NOW, clock=clock_from([0]))

    result = loop.run_turn(state, "Ich hätte gern jemanden vom Team")

    assert result.ended is True and state.stage == "callback"
    assert len(llm.calls) == 1
    card = session.scalars(select(Callback)).one()
    assert card.reason == "human_requested"
    assert card.summary == (
        "Kunde wollte mit einem Menschen sprechen. Kunde sagte: "
        "Ich hätte gern jemanden vom Team"
    )


def test_a_transfer_of_the_model_without_team_or_number_asks_for_one(session, state):
    llm = _model_asks_for_the_team()
    loop = ConversationLoop(session, llm, "system", now=NOW, clock=clock_from([0]))

    result = loop.run_turn(state, "Ich hätte gern jemanden vom Team")

    assert result.ended is False and result.say == [SAY_CALLBACK_NUMBER]
    result = loop.run_turn(state, "0721 5551234")
    assert result.ended is True and len(llm.calls) == 1
    card = session.scalars(select(Callback)).one()
    assert (card.reason, card.phone) == ("human_requested", "+497215551234")
    # What the guest wanted waits with the card for the number (Codex PR #658).
    assert card.summary == (
        "Kunde wollte mit einem Menschen sprechen. Kunde sagte: "
        "Ich hätte gern jemanden vom Team"
    )


def test_a_transfer_with_a_reason_the_model_made_up_is_still_carried_out(
    session, state
):
    call = ToolCall(name="transfer_to_team", args={"reason": ["unklar"]})
    llm = FakeLLM([LLMTurn(tool_call=call)])
    loop = ConversationLoop(session, llm, "system", now=OPEN_NOW, clock=clock_from([0]))

    result = loop.run_turn(state, "Ich hätte gern jemanden vom Team")

    assert result.ended is True and state.stage == "transferred"


def test_a_model_cannot_claim_an_outage_as_its_reason(session, state):
    """An outage is the core's to log: a model that names it hands over as
    not understood, so the figures count only real faults (#769)."""
    call = ToolCall(name="transfer_to_team", args={"reason": "outage"})
    llm = FakeLLM([LLMTurn(tool_call=call)])
    loop = ConversationLoop(session, llm, "system", now=OPEN_NOW, clock=clock_from([0]))

    result = loop.run_turn(state, "Ich hätte gern jemanden vom Team")

    assert result.ended is True and state.stage == "transferred"
    assert session.get(Call, state.call_id).transfer_reason == "not_understood"


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


def test_the_call_limit_without_team_or_number_asks_for_a_number(session, state):
    """Past its limit, outside the opening hours, the caller id withheld: the
    call ended on the last resort, with nobody rung and nothing on the tablet.
    The question for the number runs without the model, so the core asks it
    before it gives up (#652, rule 5). No number is guessed (rule 2)."""
    llm = FakeLLM([])
    loop = ConversationLoop(
        session, llm, "system", now=NOW, max_call_seconds=10, clock=clock_from([0, 100])
    )

    result = loop.run_turn(state, "Ich möchte einen Tisch reservieren")

    assert result.ended is False and result.say == [SAY_CALLBACK_NUMBER]
    result = loop.run_turn(state, "0721 5551234")
    assert result.ended is True and state.stage == "callback"
    assert llm.calls == []
    card = session.scalars(select(Callback)).one()
    assert card.phone == "+497215551234"
    assert "Ich möchte einen Tisch reservieren" in card.summary


def test_a_guest_who_gives_no_number_past_the_call_limit_hears_the_last_resort(
    session, state
):
    """The question does not keep a call open: a no ends it, and the sentence
    does not promise the team (Codex PR #208)."""
    loop = ConversationLoop(
        session,
        FakeLLM([]),
        "system",
        now=NOW,
        max_call_seconds=10,
        clock=clock_from([0, 100]),
    )

    loop.run_turn(state, "Hallo")
    result = loop.run_turn(state, "Nein")

    assert result.ended is True and state.stage == "ended"
    assert result.say == [SAY_NOBODY_REACHABLE]
    assert session.scalars(select(Callback)).all() == []


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


@pytest.mark.parametrize(
    ("now", "stage", "phone"),
    [(OPEN_NOW, "transferred", None), (NOW, "callback", CALLER)],
)
def test_a_caller_the_ladder_cannot_understand_ends_with_the_team(
    session, state, now, stage, phone
):
    """#651 point 3: a caller who stays hard to understand is not hung up on.
    The team takes the call, or, with nobody there, a callback card under the
    number we know (docs/05 §2: the ladder ends at step 6 or 7)."""
    if phone:
        state.slots["phone"] = phone
    turns = [
        LLMTurn(say="Wie bitte?", understanding_failure="party_size") for _ in range(6)
    ]
    loop = ConversationLoop(
        session, FakeLLM(turns), "system", now=now, clock=clock_from([0] * 7)
    )

    results = [loop.run_turn(state, "Am Dingsbums um irgendwann") for _ in range(6)]

    assert [r.ended for r in results] == [False] * 5 + [True]
    assert state.stage == stage
    if phone:
        card = session.scalars(select(Callback)).one()
        assert (card.phone, card.reason) == (CALLER, "not_understood")
    else:
        assert "nicht geklärt" in (state.handoff_summary or "")


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


@pytest.mark.parametrize("known", [None, "+491712345678"])
def test_a_number_the_core_took_restarts_the_ladder_for_phone(session, state, known):
    """Codex PR #244, P2: the core writes the number itself and a model has no
    reason to report it, so the old level-ups stayed and a later correction
    went to the team after two failures. The same when the guest says the
    number that stands already: the answer was understood."""
    if known:
        state.slots["phone"] = known
    llm = FakeLLM(
        [
            *[LLMTurn(say="Wie bitte?", understanding_failure="phone")] * 4,
            LLMTurn(say="Danke."),
        ]
    )
    loop = ConversationLoop(session, llm, "system", now=NOW, clock=clock_from([0]))
    for _ in range(4):
        loop.run_turn(state, "äh")
    assert loop._ladder.level_for("phone") == 3

    note_said(state, "Unter welcher Telefonnummer sind Sie erreichbar?")
    loop.run_turn(state, "0171 2345678")

    assert state.slots["phone"] == "+491712345678"
    assert loop._ladder.level_for("phone") == 1


def test_digits_the_core_did_not_take_keep_the_ladder_for_phone(session, state):
    llm = FakeLLM(
        [
            *[LLMTurn(say="Wie bitte?", understanding_failure="phone")] * 4,
            LLMTurn(say="Mit Vorwahl bitte."),
        ]
    )
    loop = ConversationLoop(session, llm, "system", now=NOW, clock=clock_from([0]))
    for _ in range(4):
        loop.run_turn(state, "äh")

    note_said(state, "Unter welcher Telefonnummer sind Sie erreichbar?")
    loop.run_turn(state, "555 1234")

    assert loop._ladder.level_for("phone") == 3


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

    # A model that goes in circles is a fault of ours like one that is down:
    # with nobody to reach and no number, the core asks for one (#652).
    assert result.ended is False
    assert result.say == [SAY_CALLBACK_NUMBER]
    assert len(llm.calls) == MAX_TOOL_HOPS
    assert state.callback_waits is not None


def test_last_resort_never_promises_a_transfer(session, state):
    """Team not reachable and no number for a callback: the core asks for one
    (#690), and a guest who declines ends the call hearing so, not "Ich
    verbinde Sie" or "Ich gebe an das Team weiter" (Codex PR #208, P1)."""
    loop = ConversationLoop(
        session, BrokenLLM(), "system", now=NOW, clock=clock_from([0, 0])
    )

    asked = loop.run_turn(state, "Ich möchte mich beschweren!")
    result = loop.run_turn(state, "Nein, danke.")

    assert asked.ended is False and asked.say == [SAY_CALLBACK_NUMBER]
    assert result.ended is True
    assert state.stage == "ended"
    (say,) = result.say
    assert say == SAY_NOBODY_REACHABLE
    assert "verbinde" not in say
    assert "Team weiter" not in say


class BrokenLLM:
    """A model that is unreachable or answers outside the contract (T-2.4)."""

    def next_turn(
        self,
        system_prompt: str,
        state_json: dict[str, Any],
        input_text: str,
        *,
        timeout: float | None = None,
    ) -> LLMTurn:
        raise LLMError("model call failed: ReadTimeout")


def test_model_failure_hands_the_call_to_the_team(session, state):
    """A model outage is an outage like any other: the phone rings at the
    team, the caller is not left with an error (CLAUDE.md §2 rule 5)."""
    loop = ConversationLoop(
        session, BrokenLLM(), "system", now=OPEN_NOW, clock=clock_from([0, 0])
    )

    result = loop.run_turn(state, "Einen Tisch für vier bitte")

    assert result.ended is True
    assert state.stage == "transferred"
    assert state.transferred is True
    # The log tells the outage from a guest it did not understand (#769).
    assert session.get(Call, state.call_id).transfer_reason == "outage"


def test_model_failure_without_a_reachable_team_creates_a_callback(session, state):
    state.slots["phone"] = "+4972215551234"
    loop = ConversationLoop(
        session, BrokenLLM(), "system", now=NOW, clock=clock_from([0, 0])
    )

    result = loop.run_turn(state, "Einen Tisch für vier bitte")

    assert result.ended is True
    assert state.stage == "callback"
    callback = session.scalars(select(Callback)).one()
    assert "Einen Tisch für vier bitte" in callback.summary
    # The card keeps its reason of before #769; its text names the fault.
    assert callback.reason == "not_understood"
    assert callback.summary.startswith("Technische Störung")


def test_model_failure_without_team_or_number_asks_for_a_number(session, state):
    """The model is down, no service is open and the caller id is withheld:
    the guest is asked for a number and the card is written, without the
    model (#652). Before, the call ended with nobody rung and no card."""
    loop = ConversationLoop(
        session, BrokenLLM(), "system", now=NOW, clock=clock_from([0, 0])
    )

    result = loop.run_turn(state, "Einen Tisch für vier bitte")

    assert result.ended is False and result.say == [SAY_CALLBACK_NUMBER]
    # The question is what the guest heard last: a "Ja" confirms nothing.
    assert state.last_say == SAY_CALLBACK_NUMBER
    result = loop.run_turn(state, "0721 5551234")
    assert result.ended is True and state.stage == "callback"
    card = session.scalars(select(Callback)).one()
    assert card.phone == "+497215551234"
    assert "Einen Tisch für vier bitte" in card.summary
    # A fault of ours, not a guest the core did not understand.
    assert card.summary.startswith(SUMMARY_OUTAGE)


def test_a_complaint_in_the_answer_to_the_number_question_names_the_callback(
    session, state
):
    """The model is down and the core asks for a number: a complaint in the
    answer is what the team calls back for, not the outage (#652)."""
    loop = ConversationLoop(
        session, BrokenLLM(), "system", now=NOW, clock=clock_from([0, 0])
    )
    loop.run_turn(state, "Einen Tisch für vier bitte")

    result = loop.run_turn(state, "Ich will mich beschweren, 0721 5551234")

    assert result.ended is True
    assert session.scalars(select(Callback)).one().reason == "complaint"


def test_a_person_question_in_the_number_answer_keeps_the_callback_reason(
    session, state
):
    """ "Spreche ich mit einem Menschen?" next to the number is no wish to talk
    to one: the callback keeps its reason (Codex PR #667)."""
    loop = ConversationLoop(
        session, BrokenLLM(), "system", now=NOW, clock=clock_from([0, 0])
    )
    loop.run_turn(state, "Einen Tisch für vier bitte")

    loop.run_turn(
        state, "Spreche ich mit einem Menschen? Meine Nummer ist 0721 5551234"
    )

    assert session.scalars(select(Callback)).one().reason == "not_understood"


def test_an_outage_after_an_allergy_took_the_read_back_names_the_table(session, state):
    """The allergy takes the read-back back before the model is asked; the
    model then fails. The card still names the table, and the guest hears it
    is not booked (#652)."""
    read_back_reservation(session, state)
    state.slots["phone"] = CALLER
    loop = ConversationLoop(
        session, BrokenLLM(), "system", now=NOW, clock=clock_from([0, 0])
    )

    result = loop.run_turn(state, "Ich habe übrigens eine Erdnussallergie")

    assert result.ended is True and result.say[0] == SAY_TABLE_NOT_BOOKED
    card = session.scalars(select(Callback)).one().summary
    assert f"{SUMMARY_OPEN_TABLE} 4 Personen, 16.09.2026 19:00 Uhr" in card


def test_model_failure_after_a_tool_call_still_hands_over(session, state):
    """The model answers once, then drops out in the middle of the turn."""

    class DropsOut(FakeLLM):
        def next_turn(
            self,
            system_prompt: str,
            state_json: dict[str, Any],
            input_text: str,
            *,
            timeout: float | None = None,
        ) -> LLMTurn:
            if not self._turns:
                raise LLMError("model call failed: ConnectError")
            return super().next_turn(system_prompt, state_json, input_text)

    llm = DropsOut([LLMTurn(tool_call=ToolCall(name="get_service_status"))])
    loop = ConversationLoop(
        session, llm, "system", now=OPEN_NOW, clock=clock_from([0, 0, 0])
    )

    result = loop.run_turn(state, "Hallo")

    assert result.ended is True
    assert state.stage == "transferred"


# --- The core holds the hard rules against the model (agent/guards.py) ----------

READBACK_TURN = "Einen Tisch für vier morgen um sieben auf Müller"


class DraftsATable:
    """Drafts the table, then speaks. Without `then_say` it reads the sentence
    of the tool result out word for word, as the prompt asks."""

    def __init__(self, then_say: str | None = None):
        self._then_say = then_say
        self.calls: list[tuple[str, dict, str]] = []

    def next_turn(self, system_prompt, state_json, input_text, *, timeout=None):
        self.calls.append((system_prompt, state_json, input_text))
        if len(self.calls) == 1:
            return LLMTurn(
                tool_call=ToolCall(
                    "create_reservation",
                    {
                        "guest_name": "Müller",
                        "phone": "+4972215551234",
                        "party_size": 4,
                        "reserved_for": "2026-09-16T19:00:00+02:00",
                    },
                )
            )
        return LLMTurn(say=self._then_say or json.loads(input_text)["data"]["readback"])


def read_back_reservation(session, state, then_say: str | None = None):
    """One guest turn that ends with a draft read back to the guest, or, with
    `then_say`, with a draft and another sentence."""
    llm = DraftsATable(then_say)
    ConversationLoop(session, llm, "system", now=NOW, clock=clock_from([0])).run_turn(
        state, READBACK_TURN
    )
    assert state.stage == "readback_pending"
    return session.get(Reservation, state.reservation_id)


def confirming(state, then_say="Was soll ich ändern?"):
    """A model that returns `confirm` with the id from the state, whatever the
    guest said, and speaks afterwards."""
    return FakeLLM(
        [
            LLMTurn(
                tool_call=ToolCall(
                    "confirm",
                    {"entity": "reservation", "entity_id": str(state.reservation_id)},
                )
            ),
            LLMTurn(say=then_say),
        ]
    )


def test_confirm_after_a_no_stays_a_draft(session, state):
    """The case from the review of PR #208: the guest rejects the readback, the
    model returns `confirm` anyway. Rule 3 holds in the core, not in the prompt."""
    reservation = read_back_reservation(session, state)
    llm = confirming(state)
    loop = ConversationLoop(session, llm, "system", now=NOW, clock=clock_from([0]))

    result = loop.run_turn(state, "Nein, das stimmt nicht")

    session.refresh(reservation)
    assert reservation.status == "draft"
    assert state.stage == "readback_pending"
    assert result.say == ["Was soll ich ändern?"]
    # The model learns why, and the call log shows the attempt.
    refused = json.loads(llm.calls[1][2])
    assert refused["tool"] == "confirm"
    assert refused["ok"] is False
    assert refused["error_code"] == "conflict"
    assert refused["hint"] == HINT_CONFIRM
    logged = session.scalars(select(Call.tool_calls)).one()[-1]
    assert logged["name"] == "confirm"
    assert logged["ok"] is False
    assert logged["error_code"] == "conflict"


def test_confirm_after_a_yes_books(session, state):
    reservation = read_back_reservation(session, state)
    llm = confirming(state, then_say="Vielen Dank, bis morgen.")
    loop = ConversationLoop(session, llm, "system", now=NOW, clock=clock_from([0]))

    loop.run_turn(state, "Ja, passt so.")

    session.refresh(reservation)
    assert reservation.status == "confirmed"
    assert state.stage == "confirmed"


@pytest.mark.parametrize(
    "instead",
    [
        # Another question: the yes answers that one.
        "Darf es sonst noch etwas sein?",
        # A read-back in the model's own words, with the name left out.
        "Ein Tisch für vier morgen um sieben. Passt das so?",
    ],
)
def test_confirm_of_a_draft_the_guest_never_heard_stays_a_draft(
    session, state, instead
):
    """#251: the model drafts and then says something else than the sentence of
    the draft. A "Ja" after that is no yes to the table, whatever the model
    does with it."""
    reservation = read_back_reservation(session, state, then_say=instead)
    llm = confirming(state, then_say="Ich lese es Ihnen noch einmal vor.")
    loop = ConversationLoop(session, llm, "system", now=NOW, clock=clock_from([0]))

    result = loop.run_turn(state, "Ja")

    session.refresh(reservation)
    assert reservation.status == "draft"
    # The model had the sentence in its state and did not say it. On the yes
    # the core reads the draft out itself and asks the model nothing more
    # (`guards.reread_on_yes`): its announcement is never spoken.
    assert llm.calls[0][1]["readback"].startswith("Ein Tisch für vier Personen")
    assert len(llm.calls) == 1
    assert result.say == [llm.calls[0][1]["readback"]]
    logged = session.scalars(select(Call.tool_calls)).one()[-1]
    assert (logged["name"], logged["ok"]) == ("confirm", False)
    assert logged["error_code"] == "conflict"


def test_confirm_in_the_turn_of_the_draft_stays_a_draft(session, state):
    """Draft and `confirm` in one turn: the yes in "Ja, guten Tag" came before
    anything was read back."""

    class DraftsThenConfirms:
        """Draft, then `confirm` with the id the state now shows, then a sentence."""

        calls = 0

        def next_turn(
            self,
            system_prompt: str,
            state_json: dict[str, Any],
            input_text: str,
            *,
            timeout: float | None = None,
        ) -> LLMTurn:
            self.calls += 1
            if self.calls == 1:
                return LLMTurn(
                    tool_call=ToolCall(
                        "create_reservation",
                        {
                            "guest_name": "Müller",
                            "phone": "+4972215551234",
                            "party_size": 4,
                            "reserved_for": "2026-09-16T19:00:00+02:00",
                        },
                    )
                )
            if self.calls == 2:
                return LLMTurn(
                    tool_call=ToolCall(
                        "confirm",
                        {
                            "entity": "reservation",
                            "entity_id": state_json["reservation_id"],
                        },
                    )
                )
            return LLMTurn(say="Passt das so?")

    loop = ConversationLoop(
        session, DraftsThenConfirms(), "system", now=NOW, clock=clock_from([0])
    )

    loop.run_turn(state, "Ja, guten Tag, " + READBACK_TURN)

    assert session.get(Reservation, state.reservation_id).status == "draft"
    assert state.stage == "readback_pending"


def test_model_that_insists_on_confirm_ends_with_the_team(session, state):
    """A refused call costs a tool hop: a model that keeps trying runs into the
    hop limit and the handoff (rule 5), never into a booking."""
    reservation = read_back_reservation(session, state)
    state.slots["phone"] = CALLER  # nobody to reach before opening: a card
    one = confirming(state)._turns[0]
    llm = FakeLLM([one] * MAX_TOOL_HOPS)
    loop = ConversationLoop(session, llm, "system", now=NOW, clock=clock_from([0]))

    result = loop.run_turn(state, "Nein")

    session.refresh(reservation)
    assert reservation.status == "draft"
    assert result.ended is True


def test_order_with_an_id_no_search_delivered_is_not_drafted(session, state):
    """Rule 2 in the loop: the model invents a `menu_item_id`, the tool is
    never reached and the model is told why."""
    llm = FakeLLM(
        [
            LLMTurn(
                tool_call=ToolCall(
                    "draft_order",
                    {
                        "type": "pickup",
                        "customer": {"name": "Müller", "phone": "+4972215551234"},
                        "items": [{"menu_item_id": str(uuid.uuid4()), "quantity": 1}],
                    },
                )
            ),
            LLMTurn(say="Welches Gericht darf es sein?"),
        ]
    )
    loop = ConversationLoop(session, llm, "system", now=OPEN_NOW, clock=clock_from([0]))

    loop.run_turn(state, "Einmal die Ente bitte, auf Müller")

    assert state.order_id is None
    refused = json.loads(llm.calls[1][2])
    assert refused["error_code"] == "invalid_input"
    assert refused["hint"] == HINT_ITEM


def test_slot_that_no_guest_can_name_is_not_taken(session, state):
    """Rule 1: a tool result copied into the patch never reaches the next
    prompt, and it is no success on the understanding ladder."""
    llm = FakeLLM(
        [
            LLMTurn(
                tool_call=ToolCall(name="get_service_status"),
                state_patch={"open": True, "closes_at": "22:00", "party_size": 4},
            ),
            LLMTurn(say="Für wann darf ich reservieren?"),
        ]
    )
    loop = ConversationLoop(session, llm, "system", now=NOW, clock=clock_from([0]))

    loop.run_turn(state, "Wir sind zu viert")

    assert state.slots == {"party_size": 4}
    assert llm.calls[1][1]["slots"] == {"party_size": 4}


def test_answer_that_arrives_after_the_limit_is_not_spoken(session, state):
    """The limit is read before the model is asked and again when it has
    answered: 5 s before, 500 s after, with 10 s allowed."""
    state.slots["phone"] = CALLER  # nobody to reach before opening: a card
    llm = FakeLLM([LLMTurn(say="Guten Tag, was darf es sein?")])
    loop = ConversationLoop(
        session,
        llm,
        "system",
        now=NOW,
        max_call_seconds=10,
        clock=clock_from([0, 5, 500]),
    )

    result = loop.run_turn(state, "Hallo")

    assert result.ended is True
    assert "Guten Tag" not in " ".join(result.say)
    assert state.stage == "callback"


def test_late_answer_still_gives_its_phone_number_to_the_callback(session, state):
    """From the review of PR #222: the caller ID is suppressed, the guest says
    the number in this very sentence, and the model is slow. What it heard is
    taken before the handoff, or the callback that saves the call (rule 5)
    could not be created."""
    llm = FakeLLM(
        [
            LLMTurn(
                say="Guten Tag, was darf es sein?",
                state_patch={"phone": "+4972215551234"},
            )
        ]
    )
    loop = ConversationLoop(
        session,
        llm,
        "system",
        now=NOW,
        max_call_seconds=10,
        clock=clock_from([0, 5, 500]),
    )

    result = loop.run_turn(state, "Meine Nummer ist 07221 5551234")

    assert result.ended is True
    assert state.stage == "callback"
    assert session.scalars(select(Callback)).one().phone == "+4972215551234"


def test_offer_spoken_to_the_guest_makes_its_dishes_usable(session, state):
    """The loop tells the guards what the guest heard: the sentence that ends
    the turn."""
    state.offered_items = {"a": ("47", "Ente knusprig"), "b": ("13", "Pho Bo")}
    llm = FakeLLM([LLMTurn(say="Meinen Sie Nummer 47 Ente knusprig?")])
    loop = ConversationLoop(session, llm, "system", now=NOW, clock=clock_from([0]))

    loop.run_turn(state, "die Ente")

    assert state.known_item_ids == {"a"}
    # Not named yet: stays offered, the model may still put it to the guest.
    assert set(state.offered_items) == {"b"}


def test_tool_call_that_arrives_after_the_limit_is_not_dispatched(session, state):
    reservation = read_back_reservation(session, state)
    state.slots["phone"] = CALLER  # nobody to reach before opening: a card
    llm = confirming(state)
    loop = ConversationLoop(
        session,
        llm,
        "system",
        now=NOW,
        max_call_seconds=10,
        clock=clock_from([0, 5, 500]),
    )

    result = loop.run_turn(state, "Ja, passt so.")

    session.refresh(reservation)
    assert reservation.status == "draft"
    assert result.ended is True


def test_model_sees_the_local_date_and_weekday(session, state):
    """A model has no clock: "morgen um sieben" needs today's date, and a
    weekday named by the guest needs today's weekday. Local time of the
    restaurant, the same clock the tools use."""
    llm = FakeLLM([LLMTurn(say="Guten Tag.")])
    loop = ConversationLoop(session, llm, "system", now=NOW, clock=clock_from([0]))

    loop.run_turn(state, "Einen Tisch morgen um sieben")

    assert llm.calls[0][1]["now"] == "Dienstag, 2026-09-15T08:00+02:00"


def test_guest_sentence_rides_along_on_a_tool_hop(session, state):
    """Seen in the first run on a real model: after `get_service_status` the
    model got only the tool result, no longer knew what the guest wanted and
    called the same tool until the hop limit. Within one turn the sentence
    stays visible; the next turn starts from the compact state again."""
    llm = FakeLLM(
        [
            LLMTurn(tool_call=ToolCall(name="get_service_status")),
            LLMTurn(say="Für wie viele Personen?"),
            LLMTurn(say="Gern."),
        ]
    )
    loop = ConversationLoop(session, llm, "system", now=NOW, clock=clock_from([0]))

    loop.run_turn(state, "Einen Tisch morgen um sieben")
    loop.run_turn(state, "Für vier")

    first, hop, next_turn = (call[1] for call in llm.calls)
    assert "guest_said" not in first
    assert hop["guest_said"] == "Einen Tisch morgen um sieben"
    assert "guest_said" not in next_turn


def test_last_sentence_reaches_the_next_turn_and_a_tool_hop_as_answered(session, state):
    """The guest's words answer the sentence that ended the last turn, so the
    model reads them next to it. On a tool hop it is no `last_say`: next to a
    tool result a real model said it again word for word instead of the
    read-back the tool had delivered (first full eval run, 13.10.2026). It
    stands there as `answered_say`, the sentence `guest_said` answers: a
    "Für vier" the model did not write into `slots` before the tool call keeps
    its question (Codex PR #291). The first sentence of a call also counts as
    the greeting."""
    llm = FakeLLM(
        [
            LLMTurn(say="Für wie viele Personen?"),
            LLMTurn(tool_call=ToolCall(name="get_service_status")),
            LLMTurn(say="Und um wie viel Uhr?"),
            LLMTurn(say="Gern."),
        ]
    )
    loop = ConversationLoop(session, llm, "system", now=NOW, clock=clock_from([0]))

    loop.run_turn(state, "Einen Tisch bitte")
    loop.run_turn(state, "Für vier")
    loop.run_turn(state, "Um sieben")

    first, second, hop, third = (call[1] for call in llm.calls)
    assert "last_say" not in first
    assert "greeted" not in first
    assert second["last_say"] == "Für wie viele Personen?"
    assert second["greeted"] is True
    assert "last_say" not in hop
    assert hop["guest_said"] == "Für vier"
    assert hop["answered_say"] == "Für wie viele Personen?"
    # Gone with the turn, like `guest_said`.
    assert "answered_say" not in third

    # After a barge-in (#1040) the sentence answered is the part heard; what
    # was not heard is for the turn's first request only.
    state.last_said = "Gern. Und um wie viel Uhr?"
    note_unheard(state, "Und um wie viel Uhr?")
    first_request = loop._prompt_state(state)
    assert first_request["last_say"] == "Gern."
    assert first_request["unheard_say"] == "Und um wie viel Uhr?"
    on_hop = loop._prompt_state(state, guest_said="Um sieben")
    assert "last_say" not in on_hop
    assert on_hop["answered_say"] == "Gern."
    assert "unheard_say" not in on_hop
    # Heard nothing of it: the guest's words answer no sentence.
    note_unheard(state, "Gern. Und um wie viel Uhr?")
    on_hop = loop._prompt_state(state, guest_said="Um sieben")
    assert "answered_say" not in on_hop
    assert "unheard_say" not in on_hop
    # NOW is eight in the morning: closed, and known for the rest of the call.
    assert hop["service"] == {
        "is_open": False,
        "pickup_enabled": False,
        "delivery_enabled": False,
        "reservations_enabled": True,
    }
    assert third["last_say"] == "Und um wie viel Uhr?"
    assert third["service"] == hop["service"]


def test_model_sees_what_the_guest_called_for_on_the_next_turn(session, state):
    """The wish of the first sentence stands in the state of every request
    after it, the one of the same turn included."""
    llm = FakeLLM([LLMTurn(say="Gern, was darf es sein?"), LLMTurn(say="Gern.")])
    loop = ConversationLoop(session, llm, "system", now=NOW, clock=clock_from([0]))

    loop.run_turn(state, "Ich möchte etwas zum Abholen bestellen")
    loop.run_turn(state, "Es zwölf bitte")

    assert [call[1]["intent"] for call in llm.calls] == ["pickup", "pickup"]


def test_wish_in_a_sentence_that_escalates_is_still_noted(session, state):
    """Codex PR #241: the escalation returned before the wish was noted, and
    the call log of a call that went to the team said nothing about it."""
    llm = FakeLLM([])
    loop = ConversationLoop(session, llm, "system", now=OPEN_NOW, clock=lambda: 0)

    result = loop.run_turn(
        state, "Ich möchte etwas zum Abholen bestellen und mit einem Menschen sprechen"
    )

    assert result.ended
    assert llm.calls == []
    assert state.intent == "pickup"


SAY_ANSWER = f"{SAY_NOT_A_PERSON} {SAY_TEAM_OFFER}"


def test_the_question_for_a_human_gets_the_truth_and_the_team_without_the_model(
    session, state
):
    """docs/09 L1: a truthful answer every time, so the core gives it and not
    the model; with the offer of the team (T-11.5)."""
    llm = FakeLLM([])  # must not be asked
    loop = ConversationLoop(session, llm, "system", now=OPEN_NOW, clock=lambda: 0)

    result = loop.run_turn(state, "Sind Sie ein Mensch?")

    assert result.say == [SAY_ANSWER]
    assert "KI-Assistent" in SAY_NOT_A_PERSON
    assert result.ended is False
    assert llm.calls == []
    assert state.last_say == SAY_ANSWER
    assert state.stage != "transferred"


def test_a_yes_to_the_offer_hands_the_call_to_the_team(session, state):
    llm = FakeLLM([])
    loop = ConversationLoop(session, llm, "system", now=OPEN_NOW, clock=lambda: 0)
    loop.run_turn(state, "Sind Sie ein Mensch?")

    result = loop.run_turn(state, "Ja, gerne.")

    assert result.ended is True
    assert result.say == [SAY_ESCALATION]
    assert state.stage == "transferred"
    assert llm.calls == []
    call = session.get(Call, state.call_id)
    assert call.transfer_reason == "human_requested"


def test_a_no_to_the_offer_goes_on_with_the_model(session, state):
    llm = FakeLLM([LLMTurn(say="Gern, für wie viele Personen?")])
    loop = ConversationLoop(session, llm, "system", now=OPEN_NOW, clock=lambda: 0)
    loop.run_turn(state, "Sind Sie ein Mensch?")

    result = loop.run_turn(state, "Nein, schon gut. Ich möchte einen Tisch.")

    assert result.say == ["Gern, für wie viele Personen?"]
    assert result.ended is False
    # The model knows what the guest answered.
    [(_, state_json, text)] = llm.calls
    assert state_json["last_say"] == SAY_ANSWER
    assert text == "Nein, schon gut. Ich möchte einen Tisch."


def test_a_later_yes_is_no_answer_to_the_offer(session, state):
    """The offer counts for the very next sentence only: a "Ja" two turns on
    answers the model's question."""
    llm = FakeLLM([LLMTurn(say="Für wie viele Personen?"), LLMTurn(say="Gern.")])
    loop = ConversationLoop(session, llm, "system", now=OPEN_NOW, clock=lambda: 0)
    loop.run_turn(state, "Sind Sie ein Mensch?")
    loop.run_turn(state, "Nein. Einen Tisch bitte.")

    result = loop.run_turn(state, "Ja")

    assert result.say == ["Gern."]
    assert state.stage != "transferred"


def test_asking_with_the_words_of_the_wish_is_still_a_question(session, state):
    """ "Spreche ich mit einem Menschen?" holds a word of the wish to talk to
    one (`escalation.check`). It asks: the answer and the offer, no transfer
    the guest did not ask for."""
    llm = FakeLLM([])
    loop = ConversationLoop(session, llm, "system", now=OPEN_NOW, clock=lambda: 0)

    result = loop.run_turn(state, "Spreche ich mit einem Menschen?")

    assert result.say == [SAY_ANSWER]
    assert result.ended is False


def test_a_question_next_to_a_complaint_hears_the_truth_before_the_transfer(
    session, state
):
    llm = FakeLLM([])
    loop = ConversationLoop(session, llm, "system", now=OPEN_NOW, clock=lambda: 0)

    result = loop.run_turn(state, "Sind Sie ein Roboter? Ich will mich beschweren.")

    assert result.ended is True
    assert result.say == [SAY_NOT_A_PERSON, SAY_ESCALATION]
    assert session.get(Call, state.call_id).transfer_reason == "complaint"


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        (
            "Spreche ich mit einem Menschen? Ich möchte meine Reservierung stornieren.",
            "cancellation",
        ),
        (
            "Sind Sie ein Mensch? Ich will mit einem Menschen sprechen.",
            "human_requested",
        ),
    ],
)
def test_a_wish_next_to_the_question_still_goes_to_the_team(
    session, state, text, reason
):
    """Codex PR #372, P1: the question's own words were read as the wish to
    talk to a human and then dropped, and with them a cancellation or the
    wish itself in the same sentence."""
    llm = FakeLLM([])
    loop = ConversationLoop(session, llm, "system", now=OPEN_NOW, clock=lambda: 0)

    result = loop.run_turn(state, text)

    assert result.ended is True
    assert result.say == [SAY_NOT_A_PERSON, SAY_ESCALATION]
    assert session.get(Call, state.call_id).transfer_reason == reason


def test_a_yes_to_a_later_sentence_is_no_yes_to_the_offer(session, state):
    """Codex PR #372, P1: the phone line asks "Sind Sie noch da?" after a
    silence (`telephony/handler.py`, through `note_said`). The guest's "Ja"
    answers that question, not the offer before it."""
    llm = FakeLLM([LLMTurn(say="Wie kann ich helfen?")])
    loop = ConversationLoop(session, llm, "system", now=OPEN_NOW, clock=lambda: 0)
    loop.run_turn(state, "Sind Sie ein Mensch?")
    note_said(state, "Sind Sie noch da?")

    result = loop.run_turn(state, "Ja")

    assert result.say == ["Wie kann ich helfen?"]
    assert state.stage != "transferred"


def test_a_request_next_to_the_question_goes_to_the_model_after_the_truth(
    session, state
):
    """Ultrareview of PR #372: the answer alone dropped the rest of the
    sentence, and the guest had to say the table again. The truthful sentence
    comes first, then the model answers the request; no offer of the team
    then, the guest has said what they want."""
    llm = FakeLLM([LLMTurn(say="Gern, auf welchen Namen?")])
    loop = ConversationLoop(session, llm, "system", now=OPEN_NOW, clock=lambda: 0)
    text = (
        "Hallo, sind Sie ein Roboter? Ich möchte einen Tisch für vier morgen um 19 Uhr."
    )

    result = loop.run_turn(state, text)

    assert result.say == [SAY_NOT_A_PERSON, "Gern, auf welchen Namen?"]
    assert result.ended is False
    [(_, _, heard)] = llm.calls
    assert heard == text
    assert state.last_say == "Gern, auf welchen Namen?"


def test_the_question_keeps_no_call_open_past_its_limit(session, state):
    """Codex PR #372, P1: the answer came before the call limit was checked,
    so a guest asking again and again kept the call open for good."""
    llm = FakeLLM([])
    loop = ConversationLoop(
        session,
        llm,
        "system",
        now=OPEN_NOW,
        max_call_seconds=10,
        clock=clock_from([0, 100]),
    )

    result = loop.run_turn(state, "Sind Sie ein Mensch?")

    assert result.ended is True
    assert result.say[0] == SAY_NOT_A_PERSON
    assert SAY_TEAM_OFFER not in " ".join(result.say)
    assert state.stage == "transferred"


@pytest.mark.parametrize(
    "text", ["Wie heißen Sie denn?", "Hallo, mit wem spreche ich?"]
)
def test_a_guest_who_asks_the_agents_name_hears_the_truth_and_the_offer(
    session, state, text
):
    """Art. 50 guidelines, point 40 (#638): a guest who takes the agent for a
    person hears again that it is none, from the core as on the question."""
    llm = FakeLLM([])  # must not be asked
    loop = ConversationLoop(session, llm, "system", now=OPEN_NOW, clock=lambda: 0)

    result = loop.run_turn(state, text)

    assert result.say == [SAY_ANSWER]
    assert result.ended is False
    assert llm.calls == []


def test_addressed_as_a_person_next_to_a_request_hears_the_truth_first(session, state):
    llm = FakeLLM([LLMTurn(say="Gern, auf welchen Namen?")])
    loop = ConversationLoop(session, llm, "system", now=OPEN_NOW, clock=lambda: 0)
    text = "Guten Tag, junge Frau, ich möchte einen Tisch für vier morgen um 19 Uhr."

    result = loop.run_turn(state, text)

    assert result.say == [SAY_NOT_A_PERSON, "Gern, auf welchen Namen?"]
    [(_, _, heard)] = llm.calls
    assert heard == text


def test_a_yes_to_a_person_is_still_an_answer_and_gets_no_offer(session, state):
    """An address is no question: "Ja, junge Frau." answers what the agent
    asked and goes to the model. Read as the question, it got the offer of the
    team, and the next "Ja" handed the call over."""
    llm = FakeLLM([LLMTurn(say="Gern.")])
    loop = ConversationLoop(session, llm, "system", now=OPEN_NOW, clock=lambda: 0)

    result = loop.run_turn(state, "Ja, junge Frau.")

    assert result.say == [SAY_NOT_A_PERSON, "Gern."]
    assert state.last_say == "Gern."


def test_complaint_about_an_order_is_not_logged_as_a_pickup(session, state):
    """Codex PR #241: the order word in a complaint made the call a pickup in
    the call log. The reason of the transfer says what such a call was."""
    llm = FakeLLM([])
    loop = ConversationLoop(session, llm, "system", now=OPEN_NOW, clock=lambda: 0)

    result = loop.run_turn(state, "Ich möchte mich über meine Bestellung beschweren")

    assert result.ended
    assert state.intent is None


def test_model_sees_which_tools_it_called_in_this_turn(session, state):
    """Seen on a real model with pickup orders: it got the result of
    `get_service_status`, did not see that it had asked for it and asked
    again, six times, until the call went to the team. The names stay
    visible for the turn, a refused call included; the next turn starts
    without them."""
    llm = FakeLLM(
        [
            LLMTurn(tool_call=ToolCall(name="get_service_status")),
            LLMTurn(
                tool_call=ToolCall("confirm", {"entity": "order", "entity_id": "x"})
            ),
            LLMTurn(say="Was darf es sein?"),
            LLMTurn(say="Gern."),
        ]
    )
    loop = ConversationLoop(session, llm, "system", now=NOW, clock=clock_from([0]))

    loop.run_turn(state, "Ich möchte etwas zum Abholen bestellen")
    loop.run_turn(state, "Die 47")

    first, hop, second_hop, next_turn = (call[1] for call in llm.calls)
    assert "called" not in first
    assert hop["called"] == ["get_service_status"]
    assert second_hop["called"] == ["get_service_status", "confirm"]
    assert "called" not in next_turn


def test_model_sees_the_date_in_the_timezone_of_the_tenant(session):
    """The tools compute with the timezone of the tenant row. The date in the
    state must be the same day, or "morgen" lands on the wrong one around
    midnight (own review of PR #235). 00:30 in Berlin is still Monday in New
    York."""
    abroad = uuid.UUID(
        seed(session, tenant_name="Abroad", timezone="America/New_York").tenant_id
    )
    state = ConversationState(call_id=uuid.uuid4(), tenant_id=abroad)
    night = datetime(2026, 9, 15, 0, 30, tzinfo=BERLIN)
    llm = FakeLLM([LLMTurn(say="Guten Tag.")])
    loop = ConversationLoop(session, llm, "system", now=night, clock=clock_from([0]))

    loop.run_turn(state, "Einen Tisch morgen um sieben")

    assert llm.calls[0][1]["now"] == "Montag, 2026-09-14T18:30-04:00"


# --- A name or number nobody said books nothing (agent/guest_details.py) --------


@pytest.fixture
def called_state(tenant_id, call_id):
    """A call with a caller ID, as the phone line and the text phone start it."""
    return initial_state(call_id, tenant_id, caller_id=CALLER)


def reserving(guest_name, phone):
    return ToolCall(
        "create_reservation",
        {
            "guest_name": guest_name,
            "phone": phone,
            "party_size": 4,
            "reserved_for": "2026-09-16T19:00:00+02:00",
        },
    )


def test_reservation_under_an_invented_name_is_not_drafted(session, called_state):
    """Seen on a real model (07.10.2026): a placeholder name to get the draft
    through. The tool is never reached and the model is told why."""
    llm = FakeLLM(
        [
            LLMTurn(
                tool_call=reserving("Max Mustermann", CALLER),
                state_patch={"guest_name": "Max Mustermann"},
            ),
            LLMTurn(say="Auf welchen Namen darf ich reservieren?"),
        ]
    )
    loop = ConversationLoop(session, llm, "system", now=NOW, clock=clock_from([0]))

    result = loop.run_turn(called_state, "Einen Tisch für vier morgen um sieben")

    assert session.scalars(select(Reservation)).all() == []
    assert called_state.reservation_id is None
    assert "guest_name" not in called_state.slots
    assert result.say == ["Auf welchen Namen darf ich reservieren?"]
    refused = json.loads(llm.calls[1][2])
    assert refused["tool"] == "create_reservation"
    assert refused["ok"] is False
    assert refused["hint"] == HINT_GUEST_NAME
    logged = session.scalars(select(Call.tool_calls)).one()[-1]
    assert logged == {
        "name": "create_reservation",
        "duration_ms": 0.0,
        "ok": False,
        "error_code": "invalid_input",
    }


def test_mangled_caller_id_books_nothing_and_the_number_in_the_state_does(
    session, called_state
):
    """Seen on a real model: the caller ID written again with digits missing.
    The number is not read back, so the booking would reach a stranger."""

    class DropsADigit:
        """First the number with a digit missing, then the one from the state."""

        calls = 0

        def next_turn(
            self,
            system_prompt: str,
            state_json: dict[str, Any],
            input_text: str,
            *,
            timeout: float | None = None,
        ) -> LLMTurn:
            self.calls += 1
            if self.calls == 1:
                return LLMTurn(
                    tool_call=reserving("Müller", "+497221555123"),
                    state_patch={"guest_name": "Müller", "phone": "+497221555123"},
                )
            if self.calls == 2:
                self.hint = json.loads(input_text)["hint"]
                return LLMTurn(
                    tool_call=reserving("Müller", state_json["slots"]["phone"])
                )
            return LLMTurn(say="Ein Tisch für vier morgen um sieben. Passt das so?")

    llm = DropsADigit()
    loop = ConversationLoop(session, llm, "system", now=NOW, clock=clock_from([0]))

    loop.run_turn(called_state, READBACK_TURN)

    assert llm.hint == HINT_GUEST_PHONE
    assert called_state.slots["phone"] == CALLER
    assert session.scalars(select(Reservation)).one().phone == CALLER


def test_number_the_guest_says_in_this_very_turn_books(session, state):
    """Another number than the caller ID: the guest's sentence is heard before
    the model is asked, so the number in it is known when the model writes it."""
    llm = FakeLLM(
        [
            LLMTurn(
                tool_call=reserving("Müller", "+491712345678"),
                state_patch={"guest_name": "Müller", "phone": "0171 2345678"},
            ),
            LLMTurn(say="Ein Tisch für vier morgen um sieben. Passt das so?"),
        ]
    )
    loop = ConversationLoop(session, llm, "system", now=NOW, clock=clock_from([0]))

    loop.run_turn(state, READBACK_TURN + ", meine Nummer ist 0171 2345678")

    assert state.slots == {"guest_name": "Müller", "phone": "+491712345678"}
    assert session.scalars(select(Reservation)).one().phone == "+491712345678"


def test_reservation_goes_under_the_number_the_guest_chose(session, called_state):
    """Codex PR #244, P2 twice: the guest names another number than the one
    they call from, the model leaves it out of `slots` and books under the
    caller ID. The number is not read back, so nobody would notice. The core
    wrote the number the guest gave into the state itself."""

    class BooksUnderTheCallerId:
        """First under the caller ID, then under the number of the state."""

        calls = 0

        def next_turn(
            self,
            system_prompt: str,
            state_json: dict[str, Any],
            input_text: str,
            *,
            timeout: float | None = None,
        ) -> LLMTurn:
            self.calls += 1
            if self.calls == 1:
                return LLMTurn(
                    tool_call=reserving("Müller", CALLER),
                    state_patch={"guest_name": "Müller"},
                )
            if self.calls == 2:
                self.hint = json.loads(input_text)["hint"]
                return LLMTurn(
                    tool_call=reserving("Müller", state_json["slots"]["phone"])
                )
            return LLMTurn(say="Ein Tisch für vier morgen um sieben. Passt das so?")

    llm = BooksUnderTheCallerId()
    loop = ConversationLoop(session, llm, "system", now=NOW, clock=clock_from([0]))

    loop.run_turn(called_state, READBACK_TURN + ", bitte unter 0171 2345678")

    assert llm.hint == HINT_GUEST_PHONE
    assert session.scalars(select(Reservation)).one().phone == "+491712345678"


def test_callback_to_a_number_nobody_said_is_not_created(session, state):
    """The team would call a stranger back."""
    llm = FakeLLM(
        [
            LLMTurn(
                tool_call=ToolCall(
                    "create_callback",
                    {
                        "phone": "+4915112345678",
                        "reason": "out_of_scope",
                        "summary": "Frage zur Lieferung.",
                    },
                )
            ),
            LLMTurn(say="Unter welcher Nummer erreichen wir Sie?"),
        ]
    )
    loop = ConversationLoop(session, llm, "system", now=NOW, clock=clock_from([0]))

    loop.run_turn(state, "Liefern Sie auch nach Hause?")

    assert session.scalars(select(Callback)).all() == []
    assert json.loads(llm.calls[1][2])["hint"] == HINT_GUEST_PHONE


def test_invented_name_is_no_success_on_the_ladder(session, state):
    """A name the core did not take was not understood: the ladder keeps its
    count for the field."""
    llm = FakeLLM(
        [
            LLMTurn(say="Wie bitte?", understanding_failure="guest_name"),
            LLMTurn(say="Wie bitte?", understanding_failure="guest_name"),
            LLMTurn(say="Danke.", state_patch={"guest_name": "Gast"}),
        ]
    )
    loop = ConversationLoop(session, llm, "system", now=NOW, clock=clock_from([0]))

    for sentence in ("äh", "wie bitte", "irgendwas"):
        loop.run_turn(state, sentence)

    assert loop._ladder.level_for("guest_name") == 2


def test_model_that_insists_on_an_invented_name_ends_with_the_team(
    session, called_state
):
    """A refused call costs a tool hop: the hop limit and the handoff (rule
    5), never a booking."""
    llm = FakeLLM([LLMTurn(tool_call=reserving("Gast", CALLER))] * MAX_TOOL_HOPS)
    loop = ConversationLoop(session, llm, "system", now=NOW, clock=clock_from([0]))

    result = loop.run_turn(called_state, "Einen Tisch für vier morgen um sieben")

    assert result.ended is True
    assert session.scalars(select(Reservation)).all() == []


# -- A table the team decides on (`team_callback`, #252) ---------------------------
# Seed: the agent books up to 8 guests itself. Above that the core hands the
# wish to the team in the turn that learns of it, and the model is not asked
# again: a model that speaks the tool's sentence first ends its turn with it,
# and a guest who hangs up then would be lost (Codex PR #280).
TEAM_SAY = "Für neun Personen kann ich den Tisch nicht selbst zusagen"
NINE_ON_SATURDAY = {"reserved_for": "2026-09-19T19:00:00+02:00", "party_size": 9}
WISH = "Reservierungswunsch, vom Team zu bestätigen: 9 Personen, 19.09.2026 19:00 Uhr"


def _ask_for_nine(session, state, now=NOW, tool="check_slot", **more):
    """One turn with a model that has one answer: the call for the table. A
    second question to it would fail the test (`FakeLLM`)."""
    call = ToolCall(name=tool, args={**NINE_ON_SATURDAY, **more})
    llm = FakeLLM([LLMTurn(tool_call=call)])
    loop = ConversationLoop(session, llm, "system", now=now, clock=clock_from([0]))
    return loop.run_turn(state, "Neun Personen am Samstag um 19 Uhr, auf Müller.")


def test_a_table_for_the_team_becomes_a_callback_without_the_model(session, state):
    state.slots.update(phone=CALLER, guest_name="Müller")

    result = _ask_for_nine(session, state)

    assert result.ended is True and state.stage == "callback"
    assert result.say[0].startswith(TEAM_SAY)
    assert result.say[1].startswith("Ich habe Ihre Nummer notiert.")
    callback = session.scalars(select(Callback)).one()
    assert (callback.reason, callback.phone) == ("out_of_scope", CALLER)
    assert callback.summary == f"{WISH}, Name Müller."
    assert session.scalars(select(Reservation)).all() == []


def test_a_team_table_without_a_name_is_handed_over_all_the_same(session, state):
    """The number is what the team needs. Waiting for the name would bring the
    call back that ends without the team."""
    state.slots["phone"] = CALLER

    result = _ask_for_nine(session, state)

    assert result.ended is True
    assert session.scalars(select(Callback)).one().summary == f"{WISH}."


def test_a_name_nobody_said_does_not_reach_the_card(session, state):
    """`check_slot` takes no name and ignores one a model writes next to its
    arguments. The card names the guest only as the guest did: "Müller" from
    the sentence, never the model's "Mustermann" (Codex PR #280)."""
    state.slots.update(phone=CALLER, guest_name="Müller")

    _ask_for_nine(session, state, guest_name="Mustermann")

    assert session.scalars(select(Callback)).one().summary == f"{WISH}, Name Müller."


def test_a_team_table_with_a_time_without_an_offset_reaches_the_team(session, state):
    """The tool reads such a time as the restaurant's local time; the card
    must read it the same way, or the handoff raised on the naive value and
    the wish never reached the team (Codex PR #347)."""
    state.slots["phone"] = CALLER

    result = _ask_for_nine(session, state, reserved_for="2026-09-19T19:00:00")

    assert result.ended is True and state.stage == "callback"
    assert session.scalars(select(Callback)).one().summary == f"{WISH}."


def test_a_time_in_seconds_reads_as_the_restaurants_clock_on_the_card(session, state):
    """The tool takes a time as seconds since 1970 too. The card shows what
    the tool read, in the restaurant's zone, not the raw number (Codex PR
    #280)."""
    state.slots["phone"] = CALLER
    seconds = int(datetime(2026, 9, 19, 19, 0, tzinfo=BERLIN).timestamp())

    _ask_for_nine(session, state, reserved_for=seconds)

    assert session.scalars(select(Callback)).one().summary == f"{WISH}."


def test_a_draft_for_a_team_table_ends_with_the_callback_too(session, state):
    """A model that skips `check_slot`: `create_reservation` drafts nothing and
    the core hands the wish over all the same. The time in the summary is the
    restaurant's, whatever zone the model wrote."""
    state.slots["phone"] = CALLER

    result = _ask_for_nine(
        session,
        state,
        tool="create_reservation",
        guest_name="Müller",
        phone=CALLER,
        reserved_for="2026-09-19T17:00:00Z",
    )

    assert result.ended is True and state.stage == "callback"
    assert result.say[0].startswith(TEAM_SAY)
    assert session.scalars(select(Callback)).one().summary == f"{WISH}, Name Müller."
    assert session.scalars(select(Reservation)).all() == []


def test_a_team_table_without_a_number_is_transferred(session, state):
    """Caller ID withheld: nobody to call back, so the call goes to the team
    itself, which is on the line in the evening."""
    state.caller_phone = None

    result = _ask_for_nine(session, state, now=OPEN_NOW)

    assert result.ended is True and state.stage == "transferred"
    assert result.say[0].startswith(TEAM_SAY) and len(result.say) == 2
    assert session.scalars(select(Callback)).all() == []
    assert session.get(Call, state.call_id).transfer_reason == "out_of_scope"


def test_a_team_table_without_a_number_and_without_the_team_asks_for_one(
    session, state
):
    """Before the restaurant opens and without a number the core asks for one,
    after the sentence about the table, and the wish is the card of the
    callback the answer makes (#690): the team learns of it."""
    state.caller_phone = None

    result = _ask_for_nine(session, state)

    assert result.ended is False
    assert result.say[0].startswith(TEAM_SAY)
    assert result.say[-1] == SAY_CALLBACK_NUMBER
    assert session.scalars(select(Callback)).all() == []

    loop = ConversationLoop(
        session, FakeLLM([]), "system", now=NOW, clock=clock_from([0])
    )
    answered = loop.run_turn(state, "07221 5551234")

    assert answered.ended is True and state.stage == "callback"
    callback = session.scalars(select(Callback)).one()
    assert callback.summary == f"{WISH}."
    assert callback.phone == CALLER


def test_a_callback_that_fails_still_ends_with_the_team(session, state):
    """The number in the state is none a callback takes: the call goes the way
    of every handover, here to the team that is on the line in the evening."""
    state.slots["phone"] = "12"

    result = _ask_for_nine(session, state, now=OPEN_NOW)

    assert result.ended is True and state.stage == "transferred"
    assert session.scalars(select(Callback)).all() == []
    # The wish as the card of the team leg, whole and not as a guest's words.
    assert state.handoff_summary == f"{WISH}."


def test_a_team_table_next_to_an_open_order_names_the_order(session, state):
    """A guest with dishes in the order who also asks for a large table: the
    call ends with the callback, and the card and the guest both say that the
    order is not placed, or the team would never learn of it (#207)."""
    state.slots["phone"] = CALLER
    pizza = str(uuid.uuid4())
    state.seen_items[pizza] = {"number": "12", "name": "Pizza Margherita"}
    state.cart = [{"menu_item_id": pizza, "quantity": 2}]

    result = _ask_for_nine(session, state)

    assert result.ended is True and state.stage == "callback"
    assert result.say[0].startswith(TEAM_SAY)
    assert SAY_ORDER_NOT_PLACED in result.say
    summary = session.scalars(select(Callback)).one().summary
    assert summary == f"{WISH}. {SUMMARY_OPEN_ORDER} 2x 12 Pizza Margherita."


def test_a_long_open_order_keeps_every_line_on_the_card(session, state):
    """Thirty lines with long names do not fit a callback: the card falls
    back to card numbers and still names every line (Codex PR #300)."""
    state.slots["phone"] = CALLER
    for n in range(30):
        dish = str(uuid.uuid4())
        state.seen_items[dish] = {"number": f"{100 + n}", "name": "Pizza " + "x" * 40}
        state.cart.append({"menu_item_id": dish, "quantity": 2})

    _ask_for_nine(session, state)

    summary = session.scalars(select(Callback)).one().summary
    assert len(summary) <= 1000 and "Pizza" not in summary
    assert all(f"2x {100 + n}" in summary for n in range(30))


def test_a_long_open_order_leaves_room_for_a_booking_of_the_call(session, state):
    """#764 review: `create_callback` adds the call's bookings to the card and
    cuts its own text for them; the open order is fitted around them, so the
    count of the lines that did not fit is not cut away."""
    state.slots["phone"] = CALLER
    session.add(
        Reservation(
            tenant_id=state.tenant_id,
            call_id=state.call_id,
            status="confirmed",
            guest_name="Müller",
            phone=CALLER,
            party_size=2,
            reserved_for=datetime(2026, 9, 16, 19, 0, tzinfo=BERLIN),
            idempotency_key=uuid.uuid4().hex,
        )
    )
    session.commit()
    for n in range(120):
        dish = str(uuid.uuid4())
        state.seen_items[dish] = {"number": f"{100 + n}", "name": "Pizza"}
        state.cart.append({"menu_item_id": dish, "quantity": 2})

    _ask_for_nine(session, state)

    summary = session.scalars(select(Callback)).one().summary
    assert len(summary) <= 1000
    assert summary.endswith(
        "weitere Positionen. Bereits gebucht: Reservierung, 2 Personen, "
        "16.09.2026 19:00 Uhr, Name Müller."
    )


def test_a_core_card_never_passes_what_a_callback_takes(session, state):
    """Codex review of #944, P1: `CreateCallbackRequest` refuses a summary over
    1000 characters before `create_callback` could cut it, and the callback
    would be lost (rule 5). A long table, a long note for the whole order and
    a long sentence of the guest still give a card within the limit."""
    state.order_note = "bitte an der Hintertür klingeln " * 7
    for n in range(3):
        dish = str(uuid.uuid4())
        state.seen_items[dish] = {"number": f"{100 + n}", "name": "Pizza"}
        state.cart.append({"menu_item_id": dish, "quantity": 1})
    loop = ConversationLoop(session, FakeLLM([]), "system")

    card, _ = loop._card(
        state,
        "human_requested",
        "Kann ich bitte mit jemandem sprechen, es ist dringend. " * 8,
        table="Tisch für 4 Personen, Name " + "Müller-Lüdenscheidt " * 40,
    )

    assert len(card) <= 1000


def test_what_does_not_fit_on_the_card_is_counted(state):
    for n in range(10):
        dish = str(uuid.uuid4())
        state.seen_items[dish] = {"number": f"{100 + n}", "name": "Salat"}
        state.cart.append({"menu_item_id": dish, "quantity": 1})

    assert _order_lines(state, 200).startswith("1x 100 Salat, 1x 101 Salat")
    assert _order_lines(state, 80).startswith("1x 100, 1x 101, 1x 102")
    assert _order_lines(state, 40) == "1x 100, 1x 101, 8 weitere Positionen"
    # The call's bookings can take all the room: the count still stands (#764).
    assert _order_lines(state, -3) == "10 weitere Positionen"


def test_options_and_the_note_of_a_line_reach_the_card(state):
    """Two pizzas of different sizes are two lines, and "ohne Zwiebeln" is
    what the kitchen has to know: the card shows both (Codex PR #300)."""
    pizza = str(uuid.uuid4())
    state.seen_items[pizza] = {"number": "12", "name": "Pizza Margherita"}
    state.cart = [
        {
            "menu_item_id": pizza,
            "quantity": 1,
            "options": [{"group": "Größe", "name": "groß"}],
            "note": "ohne Zwiebeln",
        },
        {
            "menu_item_id": pizza,
            "quantity": 2,
            "options": [{"group": "Größe", "name": "klein"}],
        },
    ]

    assert _order_lines(state, 200) == (
        "1x 12 Pizza Margherita (Größe: groß; ohne Zwiebeln), "
        "2x 12 Pizza Margherita (Größe: klein)"
    )
    assert _order_lines(state, 70) == (
        "1x 12 (Größe: groß; ohne Zwiebeln), 2x 12 (Größe: klein)"
    )


@pytest.mark.parametrize(
    "note",
    [
        "WICHTIG !!! Kunde ist allergisch auf Erdnüsse",
        "WICHTIG !!! Allergie (Frau): Sesam",
        "ohne Gluten wegen Zöliakie",
    ],
)
def test_the_card_never_carries_a_note_about_the_guests_health(state, note):
    """Codex PR #369, P1: the card is kept for the period of a callback, the
    allergy only until the night after its order (L13). A line note about
    the guest's health is replaced, the other lines keep theirs."""
    pho, ente = str(uuid.uuid4()), str(uuid.uuid4())
    state.seen_items[pho] = {"number": "13", "name": "Pho Bo"}
    state.seen_items[ente] = {"number": "41", "name": "Ente"}
    state.cart = [
        {"menu_item_id": pho, "quantity": 1, "note": note},
        {"menu_item_id": ente, "quantity": 1, "note": "ohne Zwiebeln"},
    ]

    lines = _order_lines(state, 200)

    assert lines == (
        f"1x 13 Pho Bo ({SUMMARY_NOTE_TO_CLEAR}), 1x 41 Ente (ohne Zwiebeln)"
    )
    assert "allerg" not in lines.lower()


def test_times_in_a_tool_result_reach_the_model_in_local_time(session, state):
    """closes_at is UTC in the tool and 22:00 in Berlin: the model read the
    UTC hour and said "bis 21 Uhr" (13.10.2026)."""
    llm = FakeLLM(
        [
            LLMTurn(tool_call=ToolCall(name="get_service_status")),
            LLMTurn(say="Wir haben geöffnet."),
        ]
    )
    loop = ConversationLoop(session, llm, "system", now=OPEN_NOW, clock=clock_from([0]))

    loop.run_turn(state, "Habt ihr offen?")

    data = json.loads(llm.calls[1][2])["data"]
    closes = datetime.fromisoformat(data["closes_at"])
    assert closes.utcoffset() == OPEN_NOW.utcoffset()
    assert data["closes_at"].endswith("+02:00")


def test_local_times_touch_only_points_in_time():
    zone = BERLIN
    data = {
        "closes_at": "2026-09-15T20:00:00Z",
        "alternatives": ["2026-09-15T17:30:00+00:00"],
        "day": "2026-09-15",
        "naive": "2026-09-15T20:00:00",
        "name": "Tom Yam Talay",
        "wait": 15,
        "nothing": None,
        # No zone can take it: left as it is (Codex PR #347).
        "note": "9999-12-31T23:00:00-12:00",
    }

    local = _local_times(data, zone)

    assert local["closes_at"] == "2026-09-15T22:00+02:00"
    assert local["alternatives"] == ["2026-09-15T19:30+02:00"]
    kept = ("day", "naive", "name", "wait", "nothing", "note")
    assert {k: local[k] for k in kept} == {k: data[k] for k in kept}


def test_a_booking_nobody_made_is_not_said(session, state):
    """On "Ja, passt" with no draft a real model said the pickup was
    confirmed and named a code it made up (13.10.2026)."""
    llm = FakeLLM(
        [
            LLMTurn(say="Ihre Abholung ist bestätigt. Der Abholcode lautet 8842."),
            LLMTurn(say="Was darf ich für Sie notieren?"),
        ]
    )
    loop = ConversationLoop(session, llm, "system", now=NOW, clock=clock_from([0]))

    result = loop.run_turn(state, "Ja, passt")

    assert result.say == ["Was darf ich für Sie notieren?"]
    refused = json.loads(llm.calls[1][2])
    assert refused["ok"] is False
    assert refused["hint"] == HINT_NOT_BOOKED
    assert llm.calls[1][1]["called"] == ["say"]


def test_a_model_that_insists_on_a_booking_ends_with_the_team(session, state):
    state.slots["phone"] = CALLER  # nobody to reach before opening: a card
    llm = FakeLLM(
        [LLMTurn(say="Ihr Tisch ist reserviert.") for _ in range(MAX_TOOL_HOPS)]
    )
    loop = ConversationLoop(session, llm, "system", now=NOW, clock=clock_from([0]))

    result = loop.run_turn(state, "Ja")

    assert result.ended is True
    assert "reserviert" not in " ".join(result.say)


def test_after_a_confirm_the_core_says_the_booking(session, state):
    """Booked: the core says so from the confirm result and ends the turn;
    the model is not asked for the sentence (Codex PR #347)."""
    read_back_reservation(session, state)
    llm = confirming(state, then_say="Ihre Reservierung ist ganz sicher gebucht.")
    loop = ConversationLoop(session, llm, "system", now=NOW, clock=clock_from([0]))

    result = loop.run_turn(state, "Ja, passt")

    assert result.say == [f"{SAY_TABLE_BOOKED} {SAY_BOOKED_GOODBYE}"]
    assert state.booking_say == SAY_TABLE_BOOKED
    assert len(llm.calls) == 1


def test_a_confirm_past_the_call_limit_goes_to_the_team_like_any_slow_tool(
    session, state
):
    """A confirm that held the guest past the limit: the next hop would have
    handed over, so the booking sentence must not come late in its place
    (Codex PR #347). The booking itself stands."""
    read_back_reservation(session, state)
    llm = confirming(state)
    loop = ConversationLoop(
        session,
        llm,
        "system",
        now=OPEN_NOW,
        max_call_seconds=10,
        clock=clock_from([0, 0, 0, 100]),
    )

    result = loop.run_turn(state, "Ja, passt")

    assert state.stage == "transferred"
    # The guest still hears what was booked, before the handover sentence.
    assert result.say[0] == SAY_TABLE_BOOKED and len(result.say) == 2
    assert session.get(Reservation, state.reservation_id).status == "confirmed"
    assert state.booking_say == SAY_TABLE_BOOKED


def test_a_late_confirm_tells_the_callback_card_what_is_booked(session, state):
    """With nobody to take the call, the card must say the booking stands, or
    the team books a second time (Codex PR #347)."""
    read_back_reservation(session, state)
    state.slots["phone"] = CALLER
    llm = confirming(state)
    loop = ConversationLoop(
        session,
        llm,
        "system",
        now=NOW,
        max_call_seconds=10,
        clock=clock_from([0, 0, 0, 100]),
    )

    loop.run_turn(state, "Ja, passt")

    card = session.scalars(select(Callback)).one()
    assert "Bereits gebucht: Reservierung, 4 Personen," in card.summary
    assert card.summary.endswith("Name Müller.")


def test_a_table_booked_before_an_order_began_stays_on_the_card(session, state):
    """#764: a table confirmed, then an order drafted, which clears the
    booking sentence of the conversation (`state.apply_tool_result`); the
    guest then asks for a person and nobody is reachable. The card names the
    table from the database, or the team books it a second time."""
    read_back_reservation(session, state)
    state.slots["phone"] = CALLER
    ConversationLoop(
        session, confirming(state, then_say="Noch etwas?"), "system", now=NOW
    ).run_turn(state, "Ja, passt")
    assert state.booking_say == SAY_TABLE_BOOKED
    state.booking_say = None  # what `draft_order` does to it
    loop = ConversationLoop(session, _model_asks_for_the_team(), "system", now=NOW)

    result = loop.run_turn(state, "Ich hätte gern jemanden vom Team")

    assert result.ended is True
    card = session.scalars(select(Callback)).one()
    assert "Bereits gebucht: Reservierung, 4 Personen," in card.summary


def test_a_late_confirm_without_team_or_number_asks_for_no_number(session, state):
    """The booking stands and the line hangs up after its sentence: a question
    for a callback number would be cut off by the farewell (#652)."""
    read_back_reservation(session, state)
    llm = confirming(state)
    loop = ConversationLoop(
        session,
        llm,
        "system",
        now=NOW,
        max_call_seconds=10,
        clock=clock_from([0, 0, 0, 100]),
    )

    result = loop.run_turn(state, "Ja, passt")

    assert result.ended is True and state.callback_waits is None
    assert result.say == [SAY_TABLE_BOOKED, SAY_NOBODY_REACHABLE]
    # The booking stands, and the call is logged as the booking it is.
    assert state.stage == "confirmed"
    assert call_outcome(state)[0] == "completed"


def test_a_confirmed_table_says_an_open_order_is_not_placed(session, state):
    """A cart built before the table stays unplaced: the closing sentence
    says so before the line hangs up (Codex PR #347)."""
    read_back_reservation(session, state)
    state.cart = [{"menu_item_id": str(uuid.uuid4()), "quantity": 1}]
    llm = confirming(state)
    loop = ConversationLoop(session, llm, "system", now=NOW, clock=clock_from([0]))

    result = loop.run_turn(state, "Ja, passt")

    assert result.say == [
        f"{SAY_TABLE_BOOKED} {SAY_ORDER_NOT_PLACED} {SAY_BOOKED_GOODBYE}"
    ]


def test_the_booked_order_sentence_names_the_code_the_database_gave():
    state_data = {"handover": "queued", "pickup_code": "A7"}
    waits = {"handover": "awaiting_approval", "pickup_code": "A8"}
    order = ConversationState(
        call_id=uuid.uuid4(), tenant_id=uuid.uuid4(), order_id=uuid.uuid4()
    )

    # The name the order runs under, never a pickup code (Maxi, 23.10.2026).
    assert (
        _booked_say(order, state_data, "Müller")
        == "Ihre Bestellung auf den Namen Müller ist aufgegeben."
    )
    assert _booked_say(order, waits, "Müller") == (
        "Ihre Bestellung auf den Namen Müller ist aufgenommen, "
        "das Team bestätigt sie gleich noch."
    )
    assert _booked_say(order, state_data, None) == SAY_ORDER_PLACED
    assert "A7" not in _booked_say(order, state_data, "Müller")


def numberless(tenant_id, call_id):
    """No caller ID and no number given: nobody to call back."""
    return ConversationState(call_id=call_id, tenant_id=tenant_id)


def wants_a_callback():
    return LLMTurn(
        tool_call=ToolCall(
            name="create_callback",
            args={"phone": None, "reason": "not_understood", "summary": "Rauschen"},
        )
    )


def test_a_callback_without_a_number_goes_to_the_team_when_it_is_there(
    session, tenant_id, call_id
):
    """`eskalation_0016`: a line with nothing but noise and no caller ID. The
    model asked for the number on every turn and the call reached nobody
    (Codex PR #347, P1)."""
    state = numberless(tenant_id, call_id)
    llm = FakeLLM([wants_a_callback(), LLMTurn(say="Unter welcher Nummer?")])
    loop = ConversationLoop(session, llm, "system", now=OPEN_NOW, clock=clock_from([0]))

    result = loop.run_turn(state, "... krrr ...")

    assert result.ended is True
    assert result.say == [SAY_ESCALATION]
    assert state.stage == "transferred"
    assert len(llm.calls) == 1
    assert state.handoff_summary == (
        "Anliegen konnte automatisch nicht geklärt werden. Kunde sagte: ... krrr ..."
    )


def test_a_callback_without_a_number_asks_for_it_when_nobody_is_there(
    session, tenant_id, call_id
):
    """Before opening the number is the only way left: the model asks."""
    state = numberless(tenant_id, call_id)
    llm = FakeLLM([wants_a_callback(), LLMTurn(say="Unter welcher Nummer?")])
    loop = ConversationLoop(session, llm, "system", now=NOW, clock=clock_from([0]))

    result = loop.run_turn(state, "Rufen Sie mich zurück")

    assert result.ended is False
    assert result.say == ["Unter welcher Nummer?"]
    assert json.loads(llm.calls[1][2])["hint"] == HINT_CALLBACK_PHONE


def test_a_number_that_never_comes_ends_the_call_on_the_ladder(
    session, tenant_id, call_id
):
    """Nobody there and the number never understood: the ladder ends the call
    honestly instead of asking forever."""
    state = numberless(tenant_id, call_id)
    asks = "Unter welcher Rufnummer erreichen wir Sie?"
    turns = [t for _ in range(10) for t in (wants_a_callback(), LLMTurn(say=asks))]
    loop = ConversationLoop(
        session, FakeLLM(turns), "system", now=NOW, clock=clock_from([0])
    )

    results: list[TurnResult] = []
    while not (results and results[-1].ended) and len(results) < 10:
        results.append(loop.run_turn(state, "... chhh ..."))

    assert results[-1].ended is True
    assert results[-1].say == [SAY_NOBODY_REACHABLE]
    assert len(results) < 10


def test_a_bare_question_behind_a_refused_callback_counts_on_the_ladder(
    session, tenant_id, call_id
):
    """Review of bb3601f: the model was told to ask for the number, so here
    the bare word cannot be a number on the card."""
    state = numberless(tenant_id, call_id)
    asks = "Welche Nummer soll ich notieren?"
    turns = [t for _ in range(3) for t in (wants_a_callback(), LLMTurn(say=asks))]
    loop = ConversationLoop(
        session, FakeLLM(turns), "system", now=NOW, clock=clock_from([0])
    )

    for _ in range(3):
        loop.run_turn(state, "... chhh ...")

    assert loop._ladder.level_for("phone") > 1


def test_a_second_refused_callback_in_a_turn_counts_nothing(
    session, tenant_id, call_id
):
    """Codex PR #452, P1: the model tried again before it asked, so the guest
    has not heard a question they could have failed to answer."""
    state = numberless(tenant_id, call_id)
    llm = FakeLLM(
        [
            wants_a_callback(),
            wants_a_callback(),
            LLMTurn(say="Unter welcher Nummer?"),
        ]
    )
    loop = ConversationLoop(session, llm, "system", now=NOW, clock=clock_from([0]))

    loop.run_turn(state, "Rufen Sie mich zurück")

    assert "phone" not in loop._ladder._fields


def test_digits_behind_a_bare_question_after_a_refused_callback_are_the_number(
    session, tenant_id, call_id
):
    state = numberless(tenant_id, call_id)
    llm = FakeLLM(
        [
            wants_a_callback(),
            LLMTurn(say="Welche Nummer soll ich notieren?"),
            LLMTurn(say="Danke."),
        ]
    )
    loop = ConversationLoop(session, llm, "system", now=NOW, clock=clock_from([0]))

    loop.run_turn(state, "Rufen Sie mich zurück")
    loop.run_turn(state, "0171 2345678")

    assert state.slots["phone"] == "+491712345678"


def test_a_callback_wish_before_the_question_for_the_number_counts_nothing(
    session, tenant_id, call_id
):
    """Only an answer to the question for the number can fail: a model that
    never asks for it does not run the ladder down (Codex PR #347). Each turn
    gives the core a slot, so turns without understanding end nothing here."""
    state = numberless(tenant_id, call_id)
    turns = [
        t
        for size in range(2, 10)
        for t in (
            wants_a_callback(),
            LLMTurn(say="Wie bitte?", state_patch={"party_size": size}),
        )
    ]
    loop = ConversationLoop(
        session, FakeLLM(turns), "system", now=NOW, clock=clock_from([0])
    )

    results = [loop.run_turn(state, "Rufen Sie mich zurück") for _ in range(8)]

    assert not any(result.ended for result in results)


def test_a_callback_reason_that_is_no_string_does_not_crash_the_turn(
    session, tenant_id, call_id
):
    """`in` on a list raised TypeError before dispatch could refuse it
    (Codex PR #347)."""
    state = numberless(tenant_id, call_id)
    call = ToolCall(name="create_callback", args={"phone": None, "reason": []})
    llm = FakeLLM([LLMTurn(tool_call=call), LLMTurn(say="Unter welcher Nummer?")])
    loop = ConversationLoop(session, llm, "system", now=OPEN_NOW, clock=clock_from([0]))

    result = loop.run_turn(state, "Rufen Sie mich zurück")

    assert result.ended is True
    assert state.stage == "transferred"


# The guest's sentences of `eskalation_0016`.
NOISE = [
    "Hallo? ... krrr ... hoeren Sie?",
    "... chhh ... also ...",
    "... krrr ...",
    "... hallo ...",
    "... chhh ...",
    "... krrr ... ja ...",
    "... hallo? ...",
]
SAY_PARDON = "Entschuldigung, ich verstehe Sie schlecht. Wie kann ich helfen?"


def test_a_line_with_only_noise_goes_to_the_team_whatever_the_model_says(
    session, tenant_id, call_id
):
    """`eskalation_0016` on a real model: it answered every noise turn with a
    question and never reported a failed answer, so the ladder never moved
    and the call reached nobody (finding on #37, 29.10.2026)."""
    state = numberless(tenant_id, call_id)
    llm = FakeLLM(
        [LLMTurn(tool_call=ToolCall(name="get_service_status"))]
        + [LLMTurn(say=SAY_PARDON) for _ in NOISE]
    )
    loop = ConversationLoop(session, llm, "system", now=OPEN_NOW, clock=clock_from([0]))

    results = []
    for sentence in NOISE:
        results.append(loop.run_turn(state, sentence))
        if results[-1].ended:
            break

    assert results[-1].ended is True
    assert state.stage == "transferred"
    # The ladder's budget: three steps of two failed answers each (docs/05 §2).
    assert len(results) == 6


def test_noise_with_nobody_reachable_asks_for_a_number_to_call_back(
    session, tenant_id, call_id
):
    """Codex PR #437, P1: with the team not reachable and no number known,
    the count ended the call with nobody to call back, though nobody had
    asked for a number. The core asks once, and the answer makes the
    callback, marked as not understood."""
    state = numberless(tenant_id, call_id)
    llm = FakeLLM([LLMTurn(say=SAY_PARDON) for _ in NOISE])
    loop = ConversationLoop(session, llm, "system", now=NOW, clock=clock_from([0]))

    results = [loop.run_turn(state, sentence) for sentence in NOISE[:6]]

    assert not any(result.ended for result in results)
    assert results[-1].say == [SAY_CALLBACK_NUMBER]
    result = loop.run_turn(state, "0721 5551234")
    assert result.ended is True
    callback = session.scalars(select(Callback)).one()
    assert callback.phone == "+497215551234"
    assert callback.reason == "not_understood"


def test_a_turn_the_core_takes_something_from_counts_noise_from_the_start(
    session, tenant_id, call_id
):
    """Only turns in a row that leave the call where it was count: a slot the
    core takes is understanding, and the noise around it ends nothing."""
    state = numberless(tenant_id, call_id)
    turns = [LLMTurn(say=SAY_PARDON) for _ in range(5)]
    turns.append(LLMTurn(say="Für wann?", state_patch={"party_size": 4}))
    turns += [LLMTurn(say=SAY_PARDON) for _ in range(5)]
    loop = ConversationLoop(
        session, FakeLLM(turns), "system", now=OPEN_NOW, clock=clock_from([0])
    )

    sentences = [*NOISE[:5], "Wir sind vier Personen.", *NOISE[:5]]
    results = [loop.run_turn(state, sentence) for sentence in sentences]

    assert not any(result.ended for result in results)
    assert state.slots["party_size"] == 4


# -- Every handover the guest hears of is carried out, with its card (#655) ----
ORDER_ON_CARD = f"{SUMMARY_OPEN_ORDER} 2x 12 Pizza Margherita."
HANDED_OVER = pytest.mark.parametrize(
    ("now", "stage"), [(NOW, "callback"), (OPEN_NOW, "transferred")]
)


def _two_pizzas(state) -> None:
    pizza = str(uuid.uuid4())
    state.seen_items[pizza] = {"number": "12", "name": "Pizza Margherita"}
    state.cart = [{"menu_item_id": pizza, "quantity": 2}]


def _card(session, state) -> str:
    """The card of the handover: with the call on the team leg, else written."""
    if state.stage == "transferred":
        return state.handoff_summary
    return session.scalars(select(Callback)).one().summary


def _past_the_limit(session, now) -> ConversationLoop:
    """The call is past its limit when the guest's next sentence comes."""
    return ConversationLoop(
        session,
        FakeLLM([]),
        "system",
        now=now,
        max_call_seconds=10,
        clock=clock_from([0, 100]),
    )


@HANDED_OVER
def test_a_failed_tool_that_announces_the_team_is_carried_out(
    session, state, now, stage
):
    """The tablet cancelled the draft the guest says yes to: the tool's
    sentence ends with the handover. Passed back to the model, it was spoken
    and nothing followed; the guest waited and the call ended `abandoned`."""
    reservation = read_back_reservation(session, state)
    reservation.status = "cancelled"
    session.commit()
    state.slots["phone"] = CALLER
    llm = confirming(state, then_say=SAY_CANCELLED)
    loop = ConversationLoop(session, llm, "system", now=now, clock=clock_from([0]))

    result = loop.run_turn(state, "Ja, passt")

    assert result.ended is True and state.stage == stage
    assert result.say[0] == "Diese Reservierung wurde bereits storniert."
    assert len(result.say) == 2 and len(llm.calls) == 1
    assert _card(session, state) == (
        "Diese Reservierung wurde bereits storniert. Kunde sagte: Ja, passt"
    )


def test_a_failed_tool_without_team_or_number_asks_for_one(session, state):
    """Nobody to reach and no number: the core is still on the line and asks
    for one, the last-resort sentence would end the call (Codex PR #658)."""
    reservation = read_back_reservation(session, state)
    reservation.status = "cancelled"
    session.commit()
    assert "phone" not in state.slots
    llm = confirming(state, then_say=SAY_CANCELLED)
    loop = ConversationLoop(session, llm, "system", now=NOW, clock=clock_from([0]))

    result = loop.run_turn(state, "Ja, passt")

    assert result.ended is False
    assert result.say == [
        "Diese Reservierung wurde bereits storniert.",
        SAY_CALLBACK_NUMBER,
    ]
    result = loop.run_turn(state, "0721 5551234")
    assert result.ended is True and len(llm.calls) == 1
    card = session.scalars(select(Callback)).one()
    assert card.phone == "+497215551234"
    assert card.summary == (
        "Diese Reservierung wurde bereits storniert. Kunde sagte: Ja, passt"
    )


def test_a_booking_stands_once_on_a_card_that_waited_for_the_number(
    session, tenant_id, call_id
):
    state = numberless(tenant_id, call_id)
    state.booking_say = SAY_TABLE_BOOKED
    session.add(
        Reservation(
            tenant_id=tenant_id,
            call_id=call_id,
            status="confirmed",
            guest_name="Müller",
            phone=CALLER,
            party_size=2,
            reserved_for=datetime(2026, 9, 16, 19, 0, tzinfo=BERLIN),
            idempotency_key=uuid.uuid4().hex,
        )
    )
    session.commit()
    llm = _model_asks_for_the_team()
    loop = ConversationLoop(session, llm, "system", now=NOW, clock=clock_from([0]))

    loop.run_turn(state, "Ich hätte gern jemanden vom Team")
    loop.run_turn(state, "0721 5551234")

    card = session.scalars(select(Callback)).one().summary
    assert card.count("Bereits gebucht") == 1 and "Kunde sagte: Ich hätte" in card
    assert card.endswith(
        "Bereits gebucht: Reservierung, 2 Personen, 16.09.2026 19:00 Uhr, Name Müller."
    )


def test_a_failed_tool_with_another_sentence_goes_back_to_the_model(session, state):
    """Only the sentence that promises the team is carried out by the core."""
    llm = FakeLLM(
        [
            LLMTurn(tool_call=ToolCall(name="check_slot", args={"party_size": 0})),
            LLMTurn(say="Für wie viele Personen?"),
        ]
    )
    loop = ConversationLoop(session, llm, "system", now=NOW, clock=clock_from([0]))

    result = loop.run_turn(state, "Einen Tisch bitte")

    assert result.ended is False and result.say == ["Für wie viele Personen?"]


@HANDED_OVER
def test_a_handover_names_the_order_that_stands_unplaced(session, state, now, stage):
    """A "Ja" to the read-back just as the call runs out of time: the card
    read "Kunde sagte: Ja" and the team never learned of the order."""
    state.slots["phone"] = CALLER
    _two_pizzas(state)

    result = _past_the_limit(session, now).run_turn(state, "Ja")

    assert result.ended is True and state.stage == stage
    assert result.say[0] == SAY_ORDER_NOT_PLACED and len(result.say) == 2
    card = _card(session, state)
    assert ORDER_ON_CARD in card and card.endswith("Kunde sagte: Ja")


def test_a_handover_without_an_open_order_says_nothing_of_one(session, state):
    state.slots["phone"] = CALLER

    result = _past_the_limit(session, NOW).run_turn(state, "Ja")

    assert len(result.say) == 1 and SUMMARY_OPEN_ORDER not in _card(session, state)


def test_a_handover_names_the_table_read_back(session, state):
    read_back_reservation(session, state)
    state.slots["phone"] = CALLER

    result = _past_the_limit(session, NOW).run_turn(state, "Ja, passt")

    assert result.say[0] == SAY_TABLE_NOT_BOOKED
    assert (
        f"{SUMMARY_OPEN_TABLE} 4 Personen, 16.09.2026 19:00 Uhr, Name Müller."
        in _card(session, state)
    )


def test_an_allergy_and_the_wish_for_a_person_name_the_table_read_back(session, state):
    """#690: the allergy takes the read-back back and the same sentence asks
    for a person. The card still names the table, and the guest hears it is
    not booked."""
    read_back_reservation(session, state)
    state.slots["phone"] = CALLER
    loop = ConversationLoop(
        session, FakeLLM([]), "system", now=NOW, clock=clock_from([0])
    )

    result = loop.run_turn(
        state,
        "Ich habe eine Erdnussallergie, ich möchte mit einem Mitarbeiter sprechen.",
    )

    assert result.ended is True and result.say[0] == SAY_TABLE_NOT_BOOKED
    assert f"{SUMMARY_OPEN_TABLE} 4 Personen, 16.09.2026 19:00 Uhr" in _card(
        session, state
    )


def test_a_callback_without_a_number_keeps_the_table_read_back(session, state):
    """#690: number withheld, a table read back, and the model wants the team
    to call back. The team takes the call, and its card names the table."""
    read_back_reservation(session, state)
    state.caller_phone = None
    state.slots.pop("phone", None)
    llm = FakeLLM([wants_a_callback()])
    loop = ConversationLoop(session, llm, "system", now=OPEN_NOW, clock=clock_from([0]))

    result = loop.run_turn(state, "Rufen Sie mich lieber zurück")

    assert result.ended is True and state.stage == "transferred"
    assert result.say[0] == SAY_TABLE_NOT_BOOKED
    assert f"{SUMMARY_OPEN_TABLE} 4 Personen, 16.09.2026 19:00 Uhr" in _card(
        session, state
    )


def test_a_new_allergy_and_the_models_transfer_name_the_table_read_back(session, state):
    """#737: the allergy takes the read-back back, the escalation check lets
    the sentence pass, and the model hands the call over itself. The card
    still names the table, and the guest hears it is not booked."""
    read_back_reservation(session, state)
    state.slots["phone"] = CALLER
    loop = ConversationLoop(
        session, _model_asks_for_the_team(), "system", now=NOW, clock=clock_from([0])
    )

    result = loop.run_turn(
        state, "Ich habe eine Erdnussallergie. Kann ich bitte den Chef haben?"
    )

    assert result.ended is True and result.say[0] == SAY_TABLE_NOT_BOOKED
    assert f"{SUMMARY_OPEN_TABLE} 4 Personen, 16.09.2026 19:00 Uhr" in _card(
        session, state
    )


def test_a_new_allergy_and_a_callback_without_a_number_name_the_table(session, state):
    """#737: as above, the model asks for a callback and no number is known.
    The team takes the call, and its card names the table."""
    read_back_reservation(session, state)
    state.caller_phone = None
    state.slots.pop("phone", None)
    llm = FakeLLM([wants_a_callback()])
    loop = ConversationLoop(session, llm, "system", now=OPEN_NOW, clock=clock_from([0]))

    result = loop.run_turn(state, "Nussallergie, rufen Sie mich bitte zurück")

    assert result.ended is True and state.stage == "transferred"
    assert result.say[0] == SAY_TABLE_NOT_BOOKED
    assert f"{SUMMARY_OPEN_TABLE} 4 Personen, 16.09.2026 19:00 Uhr" in _card(
        session, state
    )


def _then_to_the_team(first: ToolCall) -> FakeLLM:
    """One tool call, then the model hands the call over itself."""
    return FakeLLM(
        [
            LLMTurn(tool_call=first),
            LLMTurn(
                tool_call=ToolCall(
                    name="transfer_to_team", args={"reason": "human_requested"}
                )
            ),
            LLMTurn(say="Ich verbinde Sie mit dem Team."),
        ]
    )


TABLE_AT_SEVEN = "4 Personen, 16.09.2026 19:00 Uhr, Name Müller."


@HANDED_OVER
def test_a_table_the_domain_refused_this_turn_is_named_as_refused(
    session, state, now, stage
):
    """#862: reservations were switched off before the yes. The card offered
    the team "Offene Reservierung" for a table that could never be booked; it
    names it as refused now, and the guest still hears it is not booked."""
    read_back_reservation(session, state)
    state.slots["phone"] = CALLER
    session.execute(update(ServiceConfig).values(reservations_enabled=False))
    session.commit()
    yes = ToolCall(
        "confirm", {"entity": "reservation", "entity_id": str(state.reservation_id)}
    )
    loop = ConversationLoop(
        session, _then_to_the_team(yes), "system", now=now, clock=clock_from([0])
    )

    result = loop.run_turn(state, "Ja")

    assert result.ended is True and state.stage == stage
    assert result.say[0] == SAY_TABLE_NOT_BOOKED
    card = _card(session, state)
    assert f"{SUMMARY_REFUSED_TABLE} {TABLE_AT_SEVEN}" in card
    assert SUMMARY_OPEN_TABLE not in card


@HANDED_OVER
@pytest.mark.parametrize(
    "other_time",
    [
        ToolCall(
            "check_slot",
            {"party_size": 4, "reserved_for": "2026-09-16T20:00:00+02:00"},
        ),
        # A correction the domain refuses (closed at 23:30): a conflict, but
        # about the new time, not the table read back.
        ToolCall(
            "create_reservation",
            {
                "guest_name": "Müller",
                "phone": CALLER,
                "party_size": 4,
                "reserved_for": "2026-09-16T23:30:00+02:00",
            },
        ),
    ],
    ids=["check_slot", "refused_correction"],
)
def test_a_question_for_another_time_keeps_the_table_open(
    session, state, now, stage, other_time
):
    """Asking for or drafting another time takes the read-back out of the
    state, but the table can still be booked: the card keeps it open."""
    read_back_reservation(session, state)
    state.slots["phone"] = CALLER
    loop = ConversationLoop(
        session, _then_to_the_team(other_time), "system", now=now, clock=clock_from([0])
    )

    result = loop.run_turn(state, "Ginge auch später?")

    assert result.ended is True and result.say[0] == SAY_TABLE_NOT_BOOKED
    assert f"{SUMMARY_OPEN_TABLE} {TABLE_AT_SEVEN}" in _card(session, state)


def test_a_draft_on_the_card_that_the_domain_refuses_is_named_as_refused(
    session, state
):
    """#862, the allergy path: the draft sent without the guest's allergy is
    put on the card (`_open_draft`); sent again, the domain refuses its time.
    The card names that table as refused, not as open."""
    read_back_reservation(session, state)
    state.slots["phone"] = CALLER
    late = ToolCall(
        "create_reservation",
        {
            "guest_name": "Müller",
            "phone": CALLER,
            "party_size": 4,
            "reserved_for": "2026-09-16T23:30:00+02:00",
        },
    )
    llm = FakeLLM(
        [
            LLMTurn(tool_call=late),
            LLMTurn(tool_call=late),
            LLMTurn(
                tool_call=ToolCall(
                    name="transfer_to_team", args={"reason": "human_requested"}
                )
            ),
            LLMTurn(say="Ich verbinde Sie mit dem Team."),
        ]
    )
    loop = ConversationLoop(session, llm, "system", now=NOW, clock=clock_from([0]))

    result = loop.run_turn(state, "Ich habe eine Erdnussallergie, lieber um halb zwölf")

    assert result.ended is True and result.say[0] == SAY_TABLE_NOT_BOOKED
    card = _card(session, state)
    assert f"{SUMMARY_REFUSED_TABLE} 4 Personen, 16.09.2026 23:30 Uhr" in card
    assert SUMMARY_OPEN_TABLE not in card


@pytest.mark.parametrize(
    "said", ["Ich will mit einem Menschen sprechen.", "Ich will mich beschweren."]
)
def test_an_escalation_word_without_a_number_or_team_asks_for_one(
    session, tenant_id, call_id, said
):
    """#690: as the model's `transfer_to_team` does, the core's own handover
    asks for a number where nobody is reachable and none is known, and the
    answer makes the callback."""
    state = numberless(tenant_id, call_id)
    loop = ConversationLoop(
        session, FakeLLM([]), "system", now=NOW, clock=clock_from([0])
    )

    result = loop.run_turn(state, said)

    assert result.ended is False and result.say == [SAY_CALLBACK_NUMBER]
    answered = loop.run_turn(state, "07221 5551234")
    assert answered.ended is True and state.stage == "callback"
    assert session.scalars(select(Callback)).one().phone == CALLER


def test_a_model_failure_names_the_open_order_too(session, state):
    class Down:
        def next_turn(self, *args, **kwargs):
            raise LLMError("down")

    state.slots["phone"] = CALLER
    _two_pizzas(state)
    loop = ConversationLoop(session, Down(), "system", now=NOW, clock=clock_from([0]))

    result = loop.run_turn(state, "Und eine Cola dazu")

    assert result.say[0] == SAY_ORDER_NOT_PLACED
    assert ORDER_ON_CARD in _card(session, state)


def test_the_number_asked_for_after_noise_keeps_the_order_on_the_card(
    session, tenant_id, call_id
):
    """The guest hears once that the order is not placed, with the question
    for the number, and the card of the next turn names it once."""
    state = numberless(tenant_id, call_id)
    _two_pizzas(state)
    turns = [LLMTurn(say=SAY_PARDON) for _ in range(6)]
    loop = ConversationLoop(
        session, FakeLLM(turns), "system", now=NOW, clock=clock_from([0])
    )

    results = [loop.run_turn(state, sentence) for sentence in NOISE[:6]]
    assert results[-1].say == [SAY_ORDER_NOT_PLACED, SAY_CALLBACK_NUMBER]
    result = loop.run_turn(state, "0721 5551234")

    assert SAY_ORDER_NOT_PLACED not in result.say
    assert _card(session, state).count(ORDER_ON_CARD) == 1


def test_a_team_table_whose_callback_fails_keeps_the_order_on_the_card(session, state):
    """The wish went into the handover as the guest's words: cut at 400
    characters, and dropped whole when a line named the guest's allergy."""
    state.slots["phone"] = "12"
    _two_pizzas(state)

    result = _ask_for_nine(session, state, now=OPEN_NOW)

    assert result.say[1:] == [SAY_ORDER_NOT_PLACED, SAY_ESCALATION]
    assert state.handoff_summary == f"{WISH}. {ORDER_ON_CARD}"


def test_a_long_open_sentence_is_repeated_whole(state):
    """Behind "Bitte nicht mehr aufnehmen." the voice said the last 299
    characters of it, from the middle of a word on."""
    asked = "Wir haben heute Pizza, Pasta und Salat. " * 12 + "Was darf es sein?"

    note_said(state, asked)

    assert _still_open(state) == asked


@pytest.mark.parametrize(
    "summary",
    [
        "Kunde verträgt keine Milch, bitte zurückrufen.",
        "Kunde hat Zöliakie, Rückfrage zur 13.",
        "Gast reagiert auf Nüsse.",
        # A word of the guest's sentence about their health (`allergy_said`).
        "Rückfrage zu den Erdnüssen in der 13.",
    ],
)
@pytest.mark.parametrize("said", ["Gegen Erdnüsse.", "Ich habe eine Erdnussallergie."])
def test_a_model_card_naming_the_guests_health_is_rewritten(state, summary, said):
    """L13: a card is kept for the period of a callback, the health note only
    until the night after the order. The reason stays; the bookings of the
    call follow from the database (`create_callback`, #764). A compound
    allergy word names its ingredient too (Codex PR #788)."""
    state.allergy_said = [said]
    call = ToolCall(
        "create_callback", {"reason": "human_requested", "summary": summary}
    )

    card = _card_without_allergy(state, call).args

    assert card["reason"] == "human_requested"
    assert card["summary"] == _handoff_summary("human_requested", None)
    assert "Erdnüss" not in card["summary"] and "Zöliakie" not in card["summary"]


@pytest.mark.parametrize("reason", ["order_open", "cancellation", None, ["x"]])
def test_a_model_card_with_a_reason_it_may_not_name_is_refused(reason):
    """`order_open` is the core's own reason (#977), and pydantic's hint for
    any other wrong reason would offer it to the model: the refusal names the
    four reasons a model may write a card with, never `order_open`. A list
    compares instead of raising (Codex PR #347)."""
    call = ToolCall("create_callback", {"reason": reason, "summary": "Bitte."})

    refused = _callback_reason_refused(call)

    assert refused is not None and refused.error_code == "invalid_input"
    hint = refused.hint or ""
    assert "`human_requested`" in hint and "order_open" not in hint


def test_a_model_card_with_its_own_reason_goes_through():
    for reason in ("complaint", "not_understood", "human_requested", "out_of_scope"):
        assert (
            _callback_reason_refused(ToolCall("create_callback", {"reason": reason}))
            is None
        )
    assert _callback_reason_refused(ToolCall("search_menu", {"query": "x"})) is None


def test_a_model_never_writes_an_open_order_card(session, state):
    """Through the loop: the model's `order_open` card is refused and the
    model asked again, no card reads "Bestellung offen" for a call where no
    order waits (#977)."""
    state.slots["phone"] = CALLER
    call = ToolCall(
        "create_callback",
        {"phone": CALLER, "reason": "order_open", "summary": "Rückruf bitte."},
    )
    llm = FakeLLM([LLMTurn(tool_call=call), LLMTurn(say="Worum geht es genau?")])
    loop = ConversationLoop(session, llm, "system", now=NOW, clock=clock_from([0]))

    result = loop.run_turn(state, "Rufen Sie mich bitte zurück")

    assert result.say == ["Worum geht es genau?"]
    assert session.scalars(select(Callback)).all() == []


def test_a_model_card_without_health_stays(state):
    state.allergy_said = ["Ich habe eine Nussallergie."]
    call = ToolCall(
        "create_callback",
        {
            "reason": "human_requested",
            "summary": "Kunde möchte einen Tisch am Freitag.",
        },
    )
    assert _card_without_allergy(state, call) == call


@pytest.mark.parametrize(
    ("note", "shown"),
    [
        ("WICHTIG !!! Allergie (Ehefrau): Sesam", SUMMARY_NOTE_TO_CLEAR),
        ("bitte alles vegan halten", "bitte alles vegan halten"),
    ],
)
def test_the_order_note_on_a_card_never_names_health(state, note, shown):
    pho = str(uuid.uuid4())
    state.seen_items[pho] = {"number": "13", "name": "Pho Bo"}
    state.cart = [{"menu_item_id": pho, "quantity": 1}]
    state.order_note = note

    assert _order_lines(state, 200) == (
        f"für die ganze Bestellung: {shown}, 1x 13 Pho Bo"
    )


@pytest.mark.parametrize(
    ("said", "summary"),
    [
        ("Ich bin allergisch gegen Soja.", "Rückfrage zu Soja in der 13."),
        ("Ich vertrage keine Eier.", "Kunde fragt nach Ei in der 40."),
        ("Ich habe eine Senfallergie.", "Rückfrage zum Senf."),
    ],
)
def test_a_short_allergen_name_on_a_model_card_is_rewritten(state, said, summary):
    """Words under five letters are no stems: the allergen by name still
    names the guest's health (Codex PR #788)."""
    state.allergy_said = [said]
    call = ToolCall(
        "create_callback", {"reason": "human_requested", "summary": summary}
    )

    assert _card_without_allergy(state, call).args["summary"] != summary


@pytest.mark.parametrize(
    ("said", "summary"),
    [
        ("Ich bin allergisch gegen Kiwi.", "Rückruf wegen Kiwi im Obstsalat"),
        ("Ich vertrage keinen Mais.", "Frage zu Mais in der Suppe"),
        ("Ich bin allergisch gegen Zimt.", "Rückruf: Zimt im Dessert?"),
        ("Ich habe eine Allergie gegen Mohn.", "Rückruf: Mohnbrötchen"),
        ("Meine Frau verträgt kein Lamm.", "Rückruf: Lamm im Curry"),
        ("Ich habe eine Kiwiallergie.", "Rückruf: Kiwis im Obstsalat"),
        ("Ich reagiere auf Reis.", "Rückruf: Reis zur 13"),
        # The plural in the guest's sentence, the singular on the card (#798):
        # "Kiwis" is five letters, "Kiwi" four, and neither reader matched.
        ("Ich bin allergisch gegen Kiwis.", "Rückruf: Obstsalat ohne Kiwi"),
        ("Ich vertrage keine Mohnbrötchen.", "Rückruf: Mohn im Dessert?"),
    ],
)
def test_a_short_ingredient_of_the_guest_on_a_model_card_is_rewritten(
    state, said, summary
):
    """#764 (L13): what the guest said they react to is matched at any
    length, the way the allergen names are since #788 for "Soja"."""
    state.allergy_said = [said]
    call = ToolCall(
        "create_callback", {"reason": "human_requested", "summary": summary}
    )

    assert _card_without_allergy(state, call).args["summary"] != summary


@pytest.mark.parametrize(
    ("said", "summary"),
    [
        # Short words of the sentence that are no ingredient stay off.
        ("Ich bin allergisch gegen Kiwi.", "Kunde möchte einen Tisch am Freitag."),
        ("Ich habe eine Allergie gegen die Kiwi.", "Rückruf: die Bestellung um acht"),
        ("Meine Frau verträgt kein Lamm.", "Rückruf: Frau Meier wegen der 13"),
    ],
)
def test_a_card_without_the_short_ingredient_stays(state, said, summary):
    state.allergy_said = [said]
    call = ToolCall(
        "create_callback", {"reason": "human_requested", "summary": summary}
    )

    assert _card_without_allergy(state, call) == call


# -- older gaps of the allergy safety net (#940) -----------------------------------


ALLERGY_WITH_NUMBER = "0721 5551234, ich habe übrigens eine Erdnussallergie."


def test_an_allergy_in_the_answer_to_the_number_question_marks_the_card(session, state):
    """No number known, nobody reachable: the core asks for one. An allergy
    the guest names with the number is kept for the net, and the card that
    waited tells the team to ask about a kitchen note, never which allergy
    (L13), and before the guest's words, never inside them."""
    llm = _model_asks_for_the_team()
    loop = ConversationLoop(session, llm, "system", now=NOW, clock=clock_from([0]))
    loop.run_turn(state, "Ich hätte gern jemanden vom Team")

    result = loop.run_turn(state, ALLERGY_WITH_NUMBER)

    assert result.ended is True
    assert state.allergy_said == [ALLERGY_WITH_NUMBER]
    card = session.scalars(select(Callback)).one().summary
    assert card.count(SUMMARY_NOTE_TO_CLEAR) == 1
    assert "Erdnuss" not in card
    assert card.index(SUMMARY_NOTE_TO_CLEAR) < card.index("Kunde sagte:")


def test_an_allergy_while_the_number_is_still_missing_marks_the_card(session, state):
    llm = _model_asks_for_the_team()
    loop = ConversationLoop(session, llm, "system", now=NOW, clock=clock_from([0]))
    loop.run_turn(state, "Ich hätte gern jemanden vom Team")

    asked = loop.run_turn(state, "Moment, ich habe eine Erdnussallergie.")
    assert asked.ended is False and asked.say == [SAY_CALLBACK_NUMBER]
    loop.run_turn(state, "0721 5551234")

    card = session.scalars(select(Callback)).one().summary
    assert card.count(SUMMARY_NOTE_TO_CLEAR) == 1
    assert "Erdnuss" not in card


def test_an_allergy_before_the_number_question_marks_the_card_once(session, state):
    llm = _model_asks_for_the_team()
    loop = ConversationLoop(session, llm, "system", now=NOW, clock=clock_from([0]))
    loop.run_turn(state, "Ich hätte gern jemanden vom Team, ich habe eine Nussallergie")

    loop.run_turn(state, "0721 5551234, und ich vertrage auch keinen Sesam.")

    card = session.scalars(select(Callback)).one().summary
    assert card.count(SUMMARY_NOTE_TO_CLEAR) == 1


UNNAMED = [
    "Wegen meiner Allergie bitte vorsichtig sein.",
    "Das mit der Allergie ist mir wichtig.",
    "Ich bin Allergiker.",
    "Bitte denken Sie an meine Allergie.",
    "Meine Allergie ist wirklich schlimm.",
]


@pytest.mark.parametrize(
    "first",
    [
        "Ich habe eine Erdnussallergie.",
        # What the wish parser does not read, the cards do (`_ingredients`).
        "Und ich reagiere auf Erdbeeren.",
        "Ich darf keine Erdbeeren essen.",
        "Kein Koriander bitte, da bin ich allergisch.",
        # An allergen by name in any phrasing (`allergens_named`).
        "Bei Erdnüssen bekomme ich Atemnot, ich bin allergisch.",
    ],
)
def test_the_first_allergy_outlasts_later_sentences_about_health(state, first):
    """Five later kept sentences pushed the first out of `allergy_said`, and
    the net no longer knew the peanut (#940): a sentence that names what the
    guest reacts to stays, the oldest that names nothing goes."""
    _note_allergy_said(state, first)
    for said in [*UNNAMED, "Also bitte aufpassen wegen der Allergie."]:
        assert _note_allergy_said(state, said)

    assert len(state.allergy_said) == ALLERGY_SAID_MAX
    assert state.allergy_said[0] == first
    assert UNNAMED[0] not in state.allergy_said
    assert state.allergy_said[-1] == "Also bitte aufpassen wegen der Allergie."


def test_sentences_that_name_an_allergen_all_stay(state):
    named = [
        "Ich habe eine Erdnussallergie.",
        "Ich bin allergisch gegen Sesam.",
        "Ich vertrage kein Soja.",
        "Ich habe eine Sellerieallergie.",
        "Ich bin allergisch gegen Senf.",
        "Ich bin auch gegen Kiwi allergisch.",
    ]
    for said in named:
        _note_allergy_said(state, said)

    assert state.allergy_said == named


def test_the_newest_sentence_stays_behind_five_that_name_an_allergen(state):
    named = [
        "Ich habe eine Erdnussallergie.",
        "Ich bin allergisch gegen Sesam.",
        "Ich vertrage kein Soja.",
        "Ich habe eine Sellerieallergie.",
        "Ich bin allergisch gegen Senf.",
    ]
    for said in [*named, "Ich bin Allergiker."]:
        _note_allergy_said(state, said)

    assert state.allergy_said == [*named, "Ich bin Allergiker."]


def test_a_sentence_said_again_is_kept_once(state):
    _note_allergy_said(state, "Ich habe eine Erdnussallergie.")
    _note_allergy_said(state, "Ich bin Allergiker.")
    _note_allergy_said(state, " ich habe eine Erdnussallergie! ")

    assert state.allergy_said == [
        "Ich bin Allergiker.",
        "ich habe eine Erdnussallergie!",
    ]


def _callback_after_an_allergy(summary: str) -> FakeLLM:
    call = ToolCall(
        "create_callback",
        {"phone": CALLER, "reason": "human_requested", "summary": summary},
    )
    return FakeLLM(
        [
            LLMTurn(say=SAY_ALLERGY_NOTED),
            LLMTurn(tool_call=call),
            LLMTurn(say="Wir rufen Sie zurück."),
        ]
    )


@pytest.mark.parametrize(
    "summary",
    [
        "Kunde bittet um Rückruf wegen einer Bestellung.",
        # The model named the allergy: the card gets its reason's sentence.
        "Kunde hat eine Erdnussallergie, bitte zurückrufen.",
        # As long as a callback takes: the marker still fits (rule 5).
        "Rückruf. " + "x" * 991,
    ],
)
def test_a_card_the_model_writes_after_an_allergy_carries_the_marker(
    session, state, summary
):
    """The core's cards tell the team to ask about the kitchen note since the
    ledger #764 review; the model's own `create_callback` did not (#940)."""
    state.slots["phone"] = CALLER
    loop = ConversationLoop(
        session, _callback_after_an_allergy(summary), "system", now=NOW
    )

    loop.run_turn(state, "Ich habe eine Erdnussallergie.")
    loop.run_turn(state, "Rufen Sie mich bitte zurück.")

    card = session.scalars(select(Callback)).one().summary
    assert card.count(SUMMARY_NOTE_TO_CLEAR) == 1
    assert "Erdnuss" not in card
    assert len(card) <= 1000


def test_a_card_the_model_writes_without_an_allergy_stays(session, state):
    state.slots["phone"] = CALLER
    summary = "Kunde bittet um Rückruf wegen einer Bestellung."
    loop = ConversationLoop(
        session, _callback_after_an_allergy(summary), "system", now=NOW
    )

    loop.run_turn(state, "Ich möchte etwas bestellen.")
    loop.run_turn(state, "Rufen Sie mich bitte zurück.")

    assert session.scalars(select(Callback)).one().summary == summary


def test_a_table_draft_the_tool_cannot_read_stays_on_the_card_as_open(session, state):
    """A draft with an additive in its note goes to the team (#649). One
    whose time no tool can read left the table off the card, and the guest
    never heard that nothing is booked (#940): the card names an open table
    without the model's unchecked values, the team clears it with the guest."""
    state.slots["phone"] = CALLER
    call = ToolCall(
        "create_reservation",
        {
            "party_size": 4,
            "reserved_for": "morgen um sieben",
            "guest_name": "Müller",
            "phone": CALLER,
            "note": "ohne Glutamat",
        },
    )
    llm = FakeLLM([LLMTurn(tool_call=call)])
    loop = ConversationLoop(session, llm, "system", now=NOW)

    result = loop.run_turn(state, "Einen Tisch für vier morgen um sieben auf Müller.")

    assert result.ended is True
    assert SAY_TABLE_NOT_BOOKED in result.say
    card = session.scalars(select(Callback)).one().summary
    assert SUMMARY_OPEN_TABLE in card
    assert "morgen um sieben" not in card


def test_a_table_for_the_team_after_an_allergy_carries_the_marker(session, state):
    """The table the team decides on is a card of the core's own, written
    straight to `create_callback`: it tells the team to ask about the kitchen
    note like every other card, never which allergy (#940, L13)."""
    state.slots.update(phone=CALLER, guest_name="Müller")
    state.allergy_said = ["Ich habe eine Erdnussallergie."]

    _ask_for_nine(session, state)

    card = session.scalars(select(Callback)).one().summary
    assert card == f"{SUMMARY_NOTE_TO_CLEAR}. {WISH}, Name Müller."


def test_the_card_that_waited_drops_words_the_answer_made_health(session, state):
    """The waiting card was judged before the guest's answer named the
    allergy: its words about the peanut stay off the card now, as on a card
    written in the same turn (L13, #940)."""
    llm = _model_asks_for_the_team()
    loop = ConversationLoop(session, llm, "system", now=NOW, clock=clock_from([0]))
    asked = loop.run_turn(
        state, "Ich möchte mit einem Menschen sprechen, es geht um Erdnüsse in der 13."
    )
    assert asked.ended is False and asked.say[-1] == SAY_CALLBACK_NUMBER

    loop.run_turn(state, "0721 5551234, ich habe nämlich eine Erdnussallergie.")

    card = session.scalars(select(Callback)).one().summary
    assert card.startswith(f"{SUMMARY_NOTE_TO_CLEAR}.")
    assert "Erdnüss" not in card and "Kunde sagte" not in card


def _delivery(state: ConversationState) -> None:
    pho = str(uuid.uuid4())
    state.seen_items[pho] = {"number": "13", "name": "Pho Bo"}
    state.cart = [{"menu_item_id": pho, "quantity": 1}]
    state.intent = "delivery"


def test_the_card_of_an_open_delivery_names_the_address(state):
    """The address the guest gave stands on the card with the order: the team
    calls back and does not ask for it again (docs/24, with T-6.8)."""
    _delivery(state)
    state.slots.update(
        street="Hauptstraße",
        house_number="5a",
        postal_code="76530",
        city="Musterstadt",
        floor_note="3. Stock",
    )

    assert _with_open_order(state, "Kunde wollte mit einem Menschen sprechen.") == (
        f"Kunde wollte mit einem Menschen sprechen. {SUMMARY_OPEN_ORDER} "
        f"{SUMMARY_DELIVERY} Hauptstraße 5a, 76530 Musterstadt, 3. Stock; "
        "1x 13 Pho Bo."
    )


def test_a_delivery_without_an_address_says_none_was_given(state):
    _delivery(state)
    state.slots.update(postal_code="76530")

    assert f"{SUMMARY_DELIVERY} 76530; 1x 13 Pho Bo." in _with_open_order(state, "x")
    state.slots.clear()
    assert f"{SUMMARY_DELIVERY_NO_ADDRESS}; 1x 13 Pho Bo." in _with_open_order(
        state, "x"
    )


def test_a_pickup_card_names_no_way(state):
    _delivery(state)
    state.intent = "pickup"
    state.slots.update(street="Hauptstraße")

    assert _with_open_order(state, "x") == f"x {SUMMARY_OPEN_ORDER} 1x 13 Pho Bo."


def test_a_known_guest_without_a_draft_has_a_stored_address(state):
    """No address said and no draft yet, but `find_customer` knew the guest:
    the team finds the stored one under the number (L12: never said)."""
    _delivery(state)
    state.customer = {"found": True, "address_id": str(uuid.uuid4())}

    assert f"{SUMMARY_DELIVERY_STORED}; 1x 13 Pho Bo." in _with_open_order(state, "x")


def test_only_words_and_numbers_of_a_slot_are_an_address(state):
    """A slot holds what the model wrote: a list or a flag is no address."""
    _delivery(state)
    state.slots.update(
        street=["Hauptstraße", "5a"], house_number=True, postal_code=76530, city="X"
    )

    assert f"{SUMMARY_DELIVERY} 76530 X; 1x 13 Pho Bo." in _with_open_order(state, "x")


def test_a_floor_note_that_names_health_stays_off_the_card(state):
    """A card is kept for the period of a callback, the guest's health never
    stands on it (L13): the team reads that there is a note to clear."""
    _delivery(state)
    state.slots.update(
        street="Hauptstraße", floor_note="Erdnussallergie, bitte klingeln"
    )
    state.allergy_said = ["Ich habe eine Erdnussallergie."]

    card = _with_open_order(state, "x")

    assert (
        f"{SUMMARY_DELIVERY} Hauptstraße, {SUMMARY_FLOOR_NOTE_TO_CLEAR}; 1x 13 Pho Bo."
        in card
    )
    assert "Erdnuss" not in card


@pytest.mark.parametrize(
    "floor",
    [
        "wegen Erdnüssen klingeln",
        "wegen  Erdnüssen klingeln ",
        "3. OG" + chr(10) + "wegen Erdnüssen klingeln",
        "wegen Erdnüssen klingeln, " + "bitte " * 30,
    ],
)
def test_a_floor_note_the_answer_made_health_leaves_the_waiting_card(state, floor):
    """The card waited for a number before the guest's answer named the
    allergy: the floor note goes as the card wrote it, whatever its blanks
    or its cut (L13, deep review of item 16)."""
    _delivery(state)
    state.slots.update(street="Hauptstraße", floor_note=floor)
    card = _with_open_order(state, "x")
    assert "Erdnüssen" in card
    state.allergy_said = ["Ich habe nämlich eine Erdnussallergie."]

    judged = _judged_again(state, card)

    assert judged == (
        f"x {SUMMARY_OPEN_ORDER} {SUMMARY_DELIVERY} Hauptstraße, "
        f"{SUMMARY_FLOOR_NOTE_TO_CLEAR}; 1x 13 Pho Bo."
    )


def test_a_long_delivery_keeps_its_address_and_the_callback_bound(state):
    """Lines that fill the card: they give way to the address, and the card
    stays inside the room it was given (rule 5)."""
    state.intent = "delivery"
    for n in range(30):
        dish = str(uuid.uuid4())
        state.seen_items[dish] = {"number": f"{n:024d}", "name": "Pho Bo"}
        state.cart.append({"menu_item_id": dish, "quantity": 1})
    state.slots.update(street="Hauptstraße " * 30, city="Musterstadt")
    street = " ".join(["Hauptstraße"] * 30)[:120].rstrip()

    card = _with_open_order(state, "x", reserve=100)

    assert f"{SUMMARY_DELIVERY} {street}, Musterstadt; 1x " in card
    assert 850 < len(card) <= 900 and "weitere Position" in card


def test_an_address_longer_than_its_room_is_cut(state):
    """Slots come from the model: an address of any length leaves room for
    the order lines."""
    _delivery(state)
    state.slots.update(street="Hauptstraße " * 100, floor_note="Hof " * 100)

    card = _with_open_order(state, "x")

    # Two parts of 120 characters each, the label and the line.
    assert len(card) < 320 and card.endswith("; 1x 13 Pho Bo.")


def test_a_note_of_the_waiting_card_the_answer_made_health_is_cleared(state):
    pho = str(uuid.uuid4())
    state.seen_items[pho] = {"number": "13", "name": "Pho Bo"}
    state.cart = [{"menu_item_id": pho, "quantity": 1, "note": "ohne Erdnüsse"}]
    card = f"{SUMMARY_OPEN_ORDER} 1x 13 Pho Bo (ohne Erdnüsse)."
    state.allergy_said = ["Ich habe nämlich eine Erdnussallergie."]

    assert _judged_again(state, card) == (
        f"{SUMMARY_OPEN_ORDER} 1x 13 Pho Bo ({SUMMARY_NOTE_TO_CLEAR})."
    )


def test_the_words_inside_a_complaint_card_go_and_the_open_order_stays(state):
    """A complaint's card has the table and the order behind the guest's
    words (`_beyond_the_database`): only the words go (#940 review)."""
    card = (
        "Kunde hat sich beschwert. Kunde sagte: Ist in der 13 Glutamat? Davon "
        f"wird mir übel. {SUMMARY_OPEN_TABLE} 4 Personen, 25.11.2026 19:00 Uhr. "
        f"{SUMMARY_OPEN_ORDER} 2x 13 Pho Bo."
    )
    state.allergy_said = ["0721 5551234, ich habe eine Glutamatunverträglichkeit."]

    assert _judged_again(state, card) == (
        f"Kunde hat sich beschwert. {SUMMARY_OPEN_TABLE} 4 Personen, "
        f"25.11.2026 19:00 Uhr. {SUMMARY_OPEN_ORDER} 2x 13 Pho Bo."
    )


def test_words_a_note_was_taken_from_go_with_it(state):
    """The note came from the guest's words: replacing it first hid what the
    words name, and the marker stood inside them (#940 review)."""
    pho = str(uuid.uuid4())
    state.cart = [{"menu_item_id": pho, "quantity": 1, "note": "ohne Erdnüsse"}]
    card = (
        f"Kunde wollte mit einem Menschen sprechen. {SUMMARY_OPEN_ORDER} 1x 13 Pho "
        "Bo (ohne Erdnüsse). Kunde sagte: Ich möchte mit einem Menschen sprechen, "
        "die 13 bitte ohne Erdnüsse."
    )
    state.allergy_said = ["0721 5551234, ich habe eine Erdnussallergie."]

    assert _judged_again(state, card) == (
        f"Kunde wollte mit einem Menschen sprechen. {SUMMARY_OPEN_ORDER} 1x 13 Pho "
        f"Bo ({SUMMARY_NOTE_TO_CLEAR})."
    )


def test_notes_cleared_on_a_long_waiting_card_keep_it_inside_a_callback(state):
    """Each short note becomes the longer marker: the card stays inside the
    1000 characters a callback takes, or the guest's callback is refused
    (rule 5, #940 review)."""
    lines = []
    for n in range(20):
        pho = str(uuid.uuid4())
        state.cart.append({"menu_item_id": pho, "quantity": 1, "note": "ohne Nüsse"})
        lines.append(f"1x {n} Pho Bo Spezial mit Rind (ohne Nüsse)")
    card = f"{SUMMARY_OPEN_ORDER} " + ", ".join(lines) + "."
    assert 850 < len(card) <= 1000
    state.allergy_said = ["0721 5551234, ich habe eine Nussallergie."]

    judged = _judged_again(state, card)

    assert len(judged) == 1000
    assert "Nüsse" not in judged


def test_a_note_the_answer_did_not_touch_stays_on_the_waiting_card(state):
    pho = str(uuid.uuid4())
    state.cart = [{"menu_item_id": pho, "quantity": 1, "note": "ohne Zwiebeln"}]
    card = (
        f"Kunde wollte mit einem Menschen sprechen. {SUMMARY_OPEN_ORDER} 1x 13 Pho "
        "Bo (ohne Zwiebeln). Kunde sagte: Bitte schnell."
    )
    state.allergy_said = ["Ich habe nämlich eine Erdnussallergie."]

    assert _judged_again(state, card) == card


def test_the_guest_hears_that_neither_the_order_nor_the_table_is_booked(session, state):
    """Both stand open: the table sentence no longer hides the order's
    (#940)."""
    pho = str(uuid.uuid4())
    state.seen_items[pho] = {"number": "13", "name": "Pho Bo"}
    state.cart = [{"menu_item_id": pho, "quantity": 1}]
    loop = ConversationLoop(session, FakeLLM([]), "system", now=NOW)

    card, lead = loop._card(state, "out_of_scope", None, table="4 Personen")

    assert lead == [SAY_ORDER_NOT_PLACED, SAY_TABLE_NOT_BOOKED]
    assert f"{SUMMARY_OPEN_TABLE} 4 Personen." in card and "1x 13 Pho Bo" in card


def test_a_draft_held_back_unread_and_then_refused_is_named_refused(session, state):
    """The first draft no tool could read waits on the card as open; the
    second, readable one the domain refuses: the card names that table as
    refused, never as open (#862, #940)."""
    state.slots["phone"] = CALLER
    unread = {
        "party_size": 4,
        "reserved_for": "heute um halb zwölf",
        "guest_name": "Müller",
        "phone": CALLER,
    }
    late = {
        **unread,
        "reserved_for": "2026-09-15T23:30:00+02:00",
        "note": "WICHTIG !!! Kunde ist allergisch auf Erdnüsse",
    }
    llm = FakeLLM(
        [
            LLMTurn(tool_call=ToolCall("create_reservation", unread)),
            LLMTurn(tool_call=ToolCall("create_reservation", late)),
            LLMTurn(tool_call=ToolCall("transfer_to_team", {"reason": "out_of_scope"})),
        ]
    )
    loop = ConversationLoop(session, llm, "system", now=NOW)

    loop.run_turn(
        state,
        "Ich habe eine Erdnussallergie, einen Tisch für vier um halb zwölf auf Müller.",
    )

    card = session.scalars(select(Callback)).one().summary
    assert SUMMARY_REFUSED_TABLE in card
    assert SUMMARY_TABLE_UNREAD not in card


def test_a_long_open_order_leaves_room_for_booking_and_marker(session, state):
    """The twin of the test above for an allergic guest: the marker leads the
    card and the count of the lines that did not fit still stands before the
    bookings (#764, #940)."""
    state.slots["phone"] = CALLER
    state.allergy_said = ["Ich habe eine Erdnussallergie."]
    session.add(
        Reservation(
            tenant_id=state.tenant_id,
            call_id=state.call_id,
            status="confirmed",
            guest_name="Müller",
            phone=CALLER,
            party_size=2,
            reserved_for=datetime(2026, 9, 16, 19, 0, tzinfo=BERLIN),
            idempotency_key=uuid.uuid4().hex,
        )
    )
    session.commit()
    for n in range(120):
        dish = str(uuid.uuid4())
        state.seen_items[dish] = {"number": f"{100 + n}", "name": "Pizza"}
        state.cart.append({"menu_item_id": dish, "quantity": 2})

    _ask_for_nine(session, state)

    summary = session.scalars(select(Callback)).one().summary
    assert len(summary) <= 1000
    assert summary.startswith(f"{SUMMARY_NOTE_TO_CLEAR}.")
    assert summary.endswith(
        "weitere Positionen. Bereits gebucht: Reservierung, 2 Personen, "
        "16.09.2026 19:00 Uhr, Name Müller."
    )
