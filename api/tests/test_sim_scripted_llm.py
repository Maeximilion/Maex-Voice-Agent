"""sim/scripted_llm.py: der regelbasierte Modell-Ersatz bis T-2.4.

Geprueft wird, was `agent/loop.py` von einem Modell erwartet: genau ein `say` oder
ein `tool_call`, Angaben als `state_patch`, ein gemeldeter Fehlversuch, wenn nichts
erkannt wurde - und vor allem, dass er nie raet (CLAUDE.md §2 Regel 2) und ohne ein
Ja nicht bestaetigt (Regel 3).
"""

import json
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from sim.scripted_llm import GREETING, QUESTIONS, SAY_ASK_AGAIN, ScriptedLLM

BERLIN = ZoneInfo("Europe/Berlin")
NOW = datetime(2026, 9, 15, 18, 0, tzinfo=BERLIN)  # Dienstag, Abendfenster
WUNSCH = "Guten Tag, ich haette gern einen Tisch fuer vier Personen morgen um 19 Uhr."
START = {"stage": "start", "open": []}


@pytest.fixture
def llm():
    return ScriptedLLM(now=NOW, timezone="Europe/Berlin")


def tool_result(name: str, ok: bool = True, **rest) -> str:
    return json.dumps({"tool": name, "ok": ok, **rest})


def nach_status(llm: ScriptedLLM, state: dict) -> None:
    """Die beiden Zuege, die jedes Gespraech eroeffnen: Status fragen, Status hoeren."""
    llm.next_turn("", state, "Guten Tag")
    llm.next_turn("", state, tool_result("get_service_status", data={}))


def test_erster_zug_fragt_den_status_und_merkt_sich_die_angaben(llm):
    turn = llm.next_turn("", START, WUNSCH)

    assert turn.tool_call is not None
    assert turn.tool_call.name == "get_service_status"
    assert turn.state_patch == {
        "party_size": 4,
        "reserved_for": "2026-09-16T19:00:00+02:00",
    }


def test_status_ergebnis_begruesst_einmal_und_fragt_das_naechste_feld(llm):
    state = {
        **START,
        "slots": {"party_size": 4, "reserved_for": "2026-09-16T19:00:00+02:00"},
    }
    llm.next_turn("", state, WUNSCH)

    turn = llm.next_turn("", state, tool_result("get_service_status", data={}))

    assert turn.tool_call is None
    assert turn.say.startswith(GREETING)
    assert QUESTIONS["guest_name"] in turn.say


def test_vollstaendige_angaben_pruefen_den_slot(llm):
    state = {
        **START,
        "slots": {
            "party_size": 4,
            "reserved_for": "2026-09-16T19:00:00+02:00",
            "guest_name": "Mueller",
            "phone": "0721 5551234",
        },
    }
    nach_status(llm, state)

    turn = llm.next_turn("", state, "Passt das?")

    assert turn.tool_call.name == "check_slot"
    assert turn.tool_call.args == {
        "reserved_for": "2026-09-16T19:00:00+02:00",
        "party_size": 4,
    }


def test_ja_nach_dem_vorlesen_bestaetigt(llm):
    reservation_id = str(uuid.uuid4())
    state = {"stage": "readback_pending", "open": [], "reservation_id": reservation_id}

    turn = llm.next_turn("", state, "Ja, passt so.")

    assert turn.tool_call.name == "confirm"
    assert turn.tool_call.args == {
        "entity": "reservation",
        "entity_id": reservation_id,
    }


def test_ohne_klares_ja_wird_nichts_bestaetigt(llm):
    """CLAUDE.md §2 Regel 3: aus dem Entwurf fuehrt nur ein ausdrueckliches Ja heraus."""
    state = {
        "stage": "readback_pending",
        "open": [],
        "reservation_id": str(uuid.uuid4()),
    }

    for text in ("Nein, lieber spaeter.", "Hm, warten Sie kurz."):
        turn = llm.next_turn("", state, text)
        assert turn.tool_call is None
        assert turn.say == SAY_ASK_AGAIN


def test_nicht_verstandener_zug_meldet_einen_fehlversuch(llm):
    state = {**START, "slots": {}}
    nach_status(llm, state)

    turn = llm.next_turn("", state, "Oehm, ja, also.")

    assert turn.understanding_failure == "party_size"
    assert turn.say == QUESTIONS["party_size"]


def test_anliegen_ausserhalb_von_version_eins_wird_zum_rueckruf(llm):
    state = {**START, "slots": {"phone": "0721 5551234"}}
    nach_status(llm, state)

    turn = llm.next_turn("", state, "Haben Sie eine Speisekarte zum Vorlesen?")

    assert turn.tool_call.name == "create_callback"
    assert turn.tool_call.args["reason"] == "out_of_scope"


def test_fehlgeschlagenes_tool_ohne_satz_uebergibt_wirklich(llm):
    """Kein erfundener Satz und keine blosse Ankuendigung: ohne vorgeschriebene
    Formulierung bleibt nur die echte Uebergabe (CLAUDE.md §2 Regeln 2 und 5)."""
    turn = llm.next_turn(
        "", START, tool_result("create_reservation", ok=False, error_code="conflict")
    )

    assert turn.tool_call.name == "transfer_to_team"


def test_fehlgeschlagenes_tool_mit_satz_geht_an_den_kunden(llm):
    turn = llm.next_turn(
        "",
        START,
        tool_result("check_slot", data={"available": False}, say="Um sieben ist voll."),
    )

    assert turn.tool_call is None
    assert turn.say == "Um sieben ist voll."


@pytest.mark.parametrize(
    ("text", "erwartet"),
    [
        ("Morgen um halb acht fuer zwei Personen", "2026-09-16T19:30:00+02:00"),
        ("Morgen um 11:30 fuer zwei Personen", "2026-09-16T11:30:00+02:00"),
        ("Am 22.09. um 20 Uhr fuer zwei Personen", "2026-09-22T20:00:00+02:00"),
    ],
)
def test_zeitangaben(llm, text, erwartet):
    """ "halb acht" ist der Abend, "11:30" bleibt der Mittagstisch."""
    state = {**START, "slots": {}}
    nach_status(llm, state)

    turn = llm.next_turn("", state, text)

    assert turn.state_patch["reserved_for"] == erwartet


# --- Codex-Review PR #104 ------------------------------------------------


def test_anliegen_ausser_reichweite_ueberlebt_die_nummernfrage(llm):
    """P1: Ohne bekannte Nummer wird erst danach gefragt. Der naechste Zug enthaelt
    dann nur die Nummer und traefe kein Stichwort mehr - ohne gemerktes Anliegen
    liefe der Kunde in die Reservierungsfragen statt in den Rueckruf."""
    state = {**START, "slots": {}}
    nach_status(llm, state)

    frage = llm.next_turn("", state, "Haben Sie eine Speisekarte?")
    assert frage.say == QUESTIONS["phone"]

    state["slots"]["phone"] = "0721 5551234"
    turn = llm.next_turn("", state, "0721 5551234")

    assert turn.tool_call is not None
    assert turn.tool_call.name == "create_callback"
    assert turn.tool_call.args["reason"] == "out_of_scope"
    assert "Speisekarte" in turn.tool_call.args["summary"]


def test_ausser_reichweite_im_ersten_zug_fragt_nicht_nach_der_personenzahl(llm):
    """P1: Die Statusabfrage im ersten Zug darf den Sonderfall nicht verschlucken."""
    state = {**START, "slots": {"phone": "0721 5551234"}}

    turn = llm.next_turn("", state, "Haben Sie eine Speisekarte?")

    assert turn.tool_call is not None
    assert turn.tool_call.name == "create_callback"


def test_neue_uhrzeit_behaelt_den_schon_genannten_tag(llm):
    """P1: Auf eine Alternative antwortet der Kunde nur mit der Uhrzeit. Ohne den
    gemerkten Tag buchte der Simulator denselben Abend auf heute um."""
    state = {
        **START,
        "slots": {"party_size": 4, "reserved_for": "2026-09-16T19:00:00+02:00"},
    }
    nach_status(llm, state)

    turn = llm.next_turn("", state, "Dann 19:30 Uhr.")

    assert turn.state_patch["reserved_for"] == "2026-09-16T19:30:00+02:00"


@pytest.mark.parametrize(
    "text", ["Im Januar muss ich erst nachsehen.", "Ich muss Jana fragen."]
)
def test_ja_in_einem_anderen_wort_ist_kein_ja(llm, text):
    """P1: CLAUDE.md §2 Regel 3 verlangt ein ausdrueckliches Ja. Teilstueck-Treffer
    ("Januar", "Jana") haetten den Entwurf ohne Zustimmung gebucht."""
    state = {
        "stage": "readback_pending",
        "open": [],
        "reservation_id": str(uuid.uuid4()),
    }

    turn = llm.next_turn("", state, text)

    assert turn.tool_call is None
    assert turn.say == SAY_ASK_AGAIN


def test_unmoegliches_datum_beendet_nicht_das_gespraech(llm):
    """P2: "am 31.02." ist ein zu erwartender Erkennungsfehler, kein Programmfehler."""
    state = {**START, "slots": {}}
    nach_status(llm, state)

    turn = llm.next_turn("", state, "Am 31.02. um 19 Uhr fuer zwei Personen.")

    assert "reserved_for" not in (turn.state_patch or {})
    assert turn.state_patch["party_size"] == 2
