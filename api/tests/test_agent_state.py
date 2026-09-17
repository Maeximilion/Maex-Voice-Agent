"""agent/state.py: kompakter Zustand statt Verlauf (docs/05 §5), Update aus Tool-Ergebnissen."""

import uuid

from api.agent.dispatch import ToolResult
from api.agent.state import ConversationState, apply_tool_result

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
