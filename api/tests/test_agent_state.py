"""agent/state.py: kompakter Zustand statt Verlauf (docs/05 §5), Update aus Tool-Ergebnissen."""

import uuid

import pytest

from api.agent.dispatch import ToolResult
from api.agent.state import (
    ConversationState,
    apply_state_patch,
    apply_tool_result,
    initial_state,
)

CALL_ID = uuid.uuid4()
TENANT_ID = uuid.uuid4()


def make_state(**overrides) -> ConversationState:
    return ConversationState(call_id=CALL_ID, tenant_id=TENANT_ID, **overrides)


def test_frischer_zustand_ist_im_start():
    state = make_state()
    assert state.stage == "start"
    assert state.to_prompt_json() == {"stage": "start", "open": []}


def test_prompt_json_traegt_nur_belegte_felder():
    state = make_state(intent="reservation", slots={"party_size": 4})
    data = state.to_prompt_json()
    assert data["intent"] == "reservation"
    assert data["slots"] == {"party_size": 4}


def test_erfolgreiche_reservierung_setzt_readback_pending():
    state = make_state()
    reservation_id = uuid.uuid4()
    result = ToolResult(ok=True, data={"reservation_id": str(reservation_id)})

    apply_tool_result(state, "create_reservation", result)

    assert state.stage == "readback_pending"
    assert state.reservation_id == reservation_id
    assert state.intent == "reservation"


def test_reservation_id_erscheint_im_prompt_json_sobald_bekannt():
    """Ohne das kann ein zustandsloses LLMClient auf dem "Ja" nach dem readback
    keine entity_id für `confirm` liefern (Codex-Review PR #101, P1)."""
    state = make_state()
    reservation_id = uuid.uuid4()
    apply_tool_result(
        state,
        "create_reservation",
        ToolResult(ok=True, data={"reservation_id": str(reservation_id)}),
    )

    assert state.to_prompt_json()["reservation_id"] == str(reservation_id)


def test_apply_state_patch_schreibt_in_die_slots():
    state = make_state()
    apply_state_patch(state, {"guest_name": "Müller"})
    apply_state_patch(state, {"party_size": 4})

    assert state.slots == {"guest_name": "Müller", "party_size": 4}


def test_confirm_setzt_bestaetigt():
    state = make_state(stage="readback_pending")
    apply_tool_result(
        state, "confirm", ToolResult(ok=True, data={"status": "confirmed"})
    )
    assert state.stage == "confirmed"


def test_fehlgeschlagenes_tool_aendert_den_zustand_nicht():
    state = make_state(stage="collecting")
    apply_tool_result(
        state, "create_reservation", ToolResult(ok=False, error_code="conflict")
    )
    assert state.stage == "collecting"
    assert state.reservation_id is None


def test_transfer_ohne_erreichbarkeit_bleibt_ohne_wirkung():
    state = make_state()
    apply_tool_result(
        state, "transfer_to_team", ToolResult(ok=True, data={"available": False})
    )
    assert state.transferred is False
    assert state.stage == "start"


def test_transfer_mit_erreichbarkeit_setzt_transferred():
    state = make_state()
    apply_tool_result(
        state, "transfer_to_team", ToolResult(ok=True, data={"available": True})
    )
    assert state.transferred is True
    assert state.stage == "transferred"


def _ok(data: dict) -> ToolResult:
    return ToolResult(ok=True, data=data)


def test_wechsel_von_reservierung_zu_bestellung_macht_die_bestellung_aktiv():
    """Codex PR #127: nach einem Reservierungsentwurf und dann einer Bestellung
    standen beide IDs im Prompt, das naechste Ja konnte die verlassene
    Reservierung bestaetigen. Aktiv ist, was zuletzt vorgelesen wurde."""
    state = ConversationState(call_id=uuid.uuid4(), tenant_id=uuid.uuid4())
    apply_tool_result(
        state, "create_reservation", _ok({"reservation_id": str(uuid.uuid4())})
    )
    order_id = uuid.uuid4()
    apply_tool_result(state, "draft_order", _ok({"order_id": str(order_id)}))

    assert state.intent == "pickup"
    assert state.order_id == order_id and state.reservation_id is None
    prompt = state.to_prompt_json()
    assert prompt["order_id"] == str(order_id) and "reservation_id" not in prompt


def test_wechsel_von_bestellung_zu_reservierung_macht_die_reservierung_aktiv():
    state = ConversationState(call_id=uuid.uuid4(), tenant_id=uuid.uuid4())
    apply_tool_result(state, "draft_order", _ok({"order_id": str(uuid.uuid4())}))
    reservation_id = uuid.uuid4()
    apply_tool_result(
        state, "create_reservation", _ok({"reservation_id": str(reservation_id)})
    )

    assert state.intent == "reservation"
    assert state.reservation_id == reservation_id and state.order_id is None
    assert "order_id" not in state.to_prompt_json()


# --- Rufnummernerkennung (Maxi, PR #127) --------------------------------------------


def test_rufnummer_aus_der_erkennung_ist_vorbelegt():
    """Der Gast ruft an: seine Nummer ist schon da und wird nicht erfragt."""
    state = initial_state(CALL_ID, TENANT_ID, caller_id="+497215551234")
    assert state.slots == {"phone": "+497215551234"}
    assert state.to_prompt_json()["slots"] == {"phone": "+497215551234"}


@pytest.mark.parametrize("caller_id", [None, "", "anonymous", "sip:unbekannt"])
def test_ohne_gueltige_rufnummer_bleibt_der_slot_leer(caller_id):
    """Unterdrueckt oder keine Nummer: dann fragt der Agent wie bisher."""
    assert initial_state(CALL_ID, TENANT_ID, caller_id=caller_id).slots == {}


def test_nationale_schreibweise_wird_normalisiert():
    state = initial_state(CALL_ID, TENANT_ID, caller_id="0721 5551234")
    assert state.slots["phone"] == "+497215551234"


# --- Gescheiterte Korrektur nach dem Vorlesen (Codex PR #127, P1) --------------------


def test_gescheiterte_korrektur_der_bestellung_nimmt_den_alten_entwurf_raus():
    """Der Gast korrigiert nach dem Vorlesen, der neue draft_order scheitert
    (z. B. Menge ueber dem Limit). Der alte Entwurf ist nicht mehr, was der Gast
    will: bliebe er im Zustand, bestaetigte das naechste Ja ihn."""
    state = make_state(stage="readback_pending", order_id=uuid.uuid4())
    apply_tool_result(
        state, "draft_order", ToolResult(ok=False, error_code="invalid_input")
    )
    assert state.order_id is None
    assert state.stage == "collecting"
    assert "order_id" not in state.to_prompt_json()


def test_gescheiterte_korrektur_der_reservierung_nimmt_den_alten_entwurf_raus():
    state = make_state(stage="readback_pending", reservation_id=uuid.uuid4())
    apply_tool_result(
        state, "create_reservation", ToolResult(ok=False, error_code="conflict")
    )
    assert state.reservation_id is None
    assert state.stage == "collecting"


def test_neue_slotpruefung_nach_dem_vorlesen_nimmt_den_alten_entwurf_raus():
    """Eine Korrektur der Reservierung beginnt mit check_slot. Ist der neue
    Wunsch belegt, darf ein spaeteres Ja nicht den alten bestaetigen."""
    state = make_state(stage="readback_pending", reservation_id=uuid.uuid4())
    apply_tool_result(
        state, "check_slot", ToolResult(ok=True, data={"available": False})
    )
    assert state.reservation_id is None
    assert state.stage == "collecting"


def test_fehler_ohne_offenes_vorlesen_aendert_nichts():
    order_id = uuid.uuid4()
    state = make_state(stage="confirmed", order_id=order_id)
    apply_tool_result(
        state, "draft_order", ToolResult(ok=False, error_code="invalid_input")
    )
    assert state.stage == "confirmed"
    assert state.order_id == order_id
