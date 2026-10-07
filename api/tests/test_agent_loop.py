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

from api.agent.guards import HINT_CONFIRM, HINT_ITEM
from api.agent.llm import FakeLLM, LLMError, LLMTurn, ToolCall
from api.agent.loop import MAX_TOOL_HOPS, SAY_NOBODY_REACHABLE, ConversationLoop
from api.agent.state import ConversationState
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


@pytest.fixture
def state(tenant_id, call_id):
    return ConversationState(call_id=call_id, tenant_id=tenant_id)


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


def read_back_reservation(session, state):
    """One guest turn that ends with a draft read back to the guest."""
    llm = FakeLLM(
        [
            LLMTurn(
                tool_call=ToolCall(
                    "create_reservation",
                    {
                        "guest_name": "Müller",
                        "phone": "+4972215551234",
                        "party_size": 4,
                        "reserved_for": "2026-09-16T19:00:00+02:00",
                    },
                )
            ),
            LLMTurn(say="Ein Tisch für vier morgen um sieben. Passt das so?"),
        ]
    )
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

    loop.run_turn(state, "Einmal die Ente bitte")

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
