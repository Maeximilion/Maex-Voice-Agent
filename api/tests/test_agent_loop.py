"""agent/loop.py: Kundenzug -> Modell -> Tool-Aufrufe -> Antwort, Abbruch bei Zeit/Hops.

Nutzt eine echte Sitzung wie die Domain-Tests, weil `dispatch()` jeden Aufruf --
auch einen mit unbekanntem Toolnamen -- in `calls.tool_calls` protokolliert
(docs/04 §Gemeinsame Regeln) und dafür eine echte `calls`-Zeile braucht.
"""

import json
import uuid
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from api.agent.guards import (
    HINT_CALLBACK_PHONE,
    HINT_CONFIRM,
    HINT_GUEST_NAME,
    HINT_GUEST_PHONE,
    HINT_ITEM,
    HINT_NOT_BOOKED,
    note_said,
)
from api.agent.llm import FakeLLM, LLMError, LLMTurn, ToolCall
from api.agent.loop import (
    MAX_TOOL_HOPS,
    SAY_BOOKED_GOODBYE,
    SAY_ESCALATION,
    SAY_NOBODY_REACHABLE,
    SAY_NOT_A_PERSON,
    SAY_ORDER_NOT_PLACED,
    SAY_ORDER_PLACED,
    SAY_TABLE_BOOKED,
    SAY_TEAM_OFFER,
    SUMMARY_OPEN_ORDER,
    ConversationLoop,
    _booked_say,
    _local_times,
    _order_lines,
)
from api.agent.state import Allergy, ConversationState, initial_state
from api.models import Call, Callback, Reservation
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


def test_transfer_without_a_reachable_team_goes_back_to_the_model(session, state):
    """Before opening nobody takes the call: the model gets the result and can
    take a callback instead, the call does not end on the tool."""
    llm = FakeLLM(
        [
            LLMTurn(
                tool_call=ToolCall(
                    name="transfer_to_team", args={"reason": "human_requested"}
                )
            ),
            LLMTurn(say="Unter welcher Nummer erreichen wir Sie?"),
        ]
    )
    loop = ConversationLoop(session, llm, "system", now=NOW, clock=clock_from([0]))

    result = loop.run_turn(state, "Ich hätte gern jemanden vom Team")

    assert result.say == ["Unter welcher Nummer erreichen wir Sie?"]
    assert result.ended is False
    assert '"available": false' in llm.calls[1][2]


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
    # Nobody was reached: the sentence must not promise the team (Codex PR #208).
    assert result.say == [SAY_NOBODY_REACHABLE]
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
    assert result.say == [SAY_NOBODY_REACHABLE]
    assert len(llm.calls) == MAX_TOOL_HOPS
    assert state.stage == "ended"


@pytest.mark.parametrize(
    "customer", ["Ich möchte mich beschweren!", "Einen Tisch für vier bitte"]
)
def test_last_resort_never_promises_a_transfer(session, state, customer):
    """Team not reachable and no number for a callback: the call ends, and the
    guest hears that, not "Ich verbinde Sie" or "Ich gebe an das Team weiter"
    (Codex PR #208, P1). Holds for an escalation as for a model outage."""
    loop = ConversationLoop(
        session, BrokenLLM(), "system", now=NOW, clock=clock_from([0, 0])
    )

    result = loop.run_turn(state, customer)

    assert result.ended is True
    assert state.stage == "ended"
    (say,) = result.say
    assert "verbinde" not in say
    assert "Team weiter" not in say


class BrokenLLM:
    """A model that is unreachable or answers outside the contract (T-2.4)."""

    def next_turn(self, system_prompt, state_json, input_text):
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


def test_model_failure_without_a_phone_number_says_so_honestly(session, state):
    loop = ConversationLoop(
        session, BrokenLLM(), "system", now=NOW, clock=clock_from([0, 0])
    )

    result = loop.run_turn(state, "Hallo")

    assert result.ended is True
    assert result.say == [SAY_NOBODY_REACHABLE]
    assert state.stage == "ended"


def test_model_failure_after_a_tool_call_still_hands_over(session, state):
    """The model answers once, then drops out in the middle of the turn."""

    class DropsOut(FakeLLM):
        def next_turn(self, system_prompt, state_json, input_text):
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

        def next_turn(self, system_prompt, state_json, input_text):
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
    assert state.stage == "ended"


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


def test_last_sentence_reaches_the_next_turn_but_no_tool_hop(session, state):
    """The guest's words answer the sentence that ended the last turn, so the
    model reads them next to it. On a tool hop it is left out: next to a tool
    result a real model said it again word for word instead of the read-back
    the tool had delivered (first full eval run, 13.10.2026). The first
    sentence of a call also counts as the greeting."""
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
    # NOW is eight in the morning: closed, and known for the rest of the call.
    assert hop["service"] == {"is_open": False, "pickup_enabled": False}
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

        def next_turn(self, system_prompt, state_json, input_text):
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

        def next_turn(self, system_prompt, state_json, input_text):
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


def test_a_team_table_without_a_number_and_without_the_team_says_so(session, state):
    """Before the restaurant opens and without a number there is no way to the
    team: the guest hears that, after the sentence about the table, and is not
    promised a callback."""
    state.caller_phone = None

    result = _ask_for_nine(session, state)

    assert result.ended is True and state.stage == "ended"
    assert result.say[0].startswith(TEAM_SAY)
    assert result.say[1] == SAY_NOBODY_REACHABLE
    assert session.scalars(select(Callback)).all() == []


def test_a_callback_that_fails_still_ends_with_the_team(session, state):
    """The number in the state is none a callback takes: the call goes the way
    of every handover, here to the team that is on the line in the evening."""
    state.slots["phone"] = "12"

    result = _ask_for_nine(session, state, now=OPEN_NOW)

    assert result.ended is True and state.stage == "transferred"
    assert session.scalars(select(Callback)).all() == []


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


def test_what_does_not_fit_on_the_card_is_counted(state):
    for n in range(10):
        dish = str(uuid.uuid4())
        state.seen_items[dish] = {"number": f"{100 + n}", "name": "Salat"}
        state.cart.append({"menu_item_id": dish, "quantity": 1})

    assert _order_lines(state, 200).startswith("1x 100 Salat, 1x 101 Salat")
    assert _order_lines(state, 80).startswith("1x 100, 1x 101, 1x 102")
    assert _order_lines(state, 40) == "1x 100, 1x 101, 8 weitere Positionen"


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


def test_the_card_names_an_allergy_only_as_a_removal(state):
    """Codex PR #369, P1: with the caller's consent the open order on a card
    carried "WICHTIG: Keine Erdnuss. Grund: Allergie". The card is kept for
    the period of a callback, the allergy only until the night after its
    order (L13): the card says what to leave out, never why."""
    pho = str(uuid.uuid4())
    state.seen_items[pho] = {"number": "13", "name": "Pho Bo"}
    state.cart = [{"menu_item_id": pho, "quantity": 1, "note": "ohne Zwiebeln"}]
    state.allergies = [
        Allergy(ingredient="Erdnuss", item=pho),
        Allergy(ingredient="Sesam"),
    ]
    state.allergy_consent = "yes"

    lines = _order_lines(state, 200)

    assert lines == (
        "1x 13 Pho Bo (ohne Erdnuss, ohne Zwiebeln), "
        "für die ganze Bestellung: ohne Sesam"
    )
    assert "Allergie" not in lines
    assert state.allergy_consent == "yes"


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
    assert f"Bereits gebucht: {SAY_TABLE_BOOKED}" in card.summary


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

    results = []
    while not (results and results[-1].ended) and len(results) < 10:
        results.append(loop.run_turn(state, "... chhh ..."))

    assert results[-1].ended is True
    assert results[-1].say == [SAY_NOBODY_REACHABLE]
    assert len(results) < 10


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
