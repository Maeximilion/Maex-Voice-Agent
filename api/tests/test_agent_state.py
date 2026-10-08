"""agent/state.py: kompakter Zustand statt Verlauf (docs/05 §5), Update aus Tool-Ergebnissen."""

import uuid

import pytest

from api.agent.dispatch import ToolResult
from api.agent.intent import heard
from api.agent.outcome import call_outcome
from api.agent.state import (
    ConversationState,
    apply_state_patch,
    apply_tool_result,
    initial_state,
    note_intent,
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


def test_wechsel_zur_reservierung_mit_belegtem_slot_setzt_die_absicht():
    """Codex PR #127, P2: die Bestellung wartet aufs Ja, der Gast will doch einen
    Tisch, der Slot ist belegt. Ohne create_reservation bliebe intent "pickup",
    und das Modell spraeche beim naechsten Zug ueber die falsche Sache."""
    state = make_state(stage="readback_pending", intent="pickup", order_id=uuid.uuid4())
    apply_tool_result(
        state, "check_slot", ToolResult(ok=True, data={"available": False})
    )
    assert state.intent == "reservation"
    assert state.order_id is None


def test_gescheiterte_bestellung_nach_reservierung_setzt_die_absicht():
    state = make_state(
        stage="readback_pending", intent="reservation", reservation_id=uuid.uuid4()
    )
    apply_tool_result(
        state, "draft_order", ToolResult(ok=False, error_code="invalid_input")
    )
    assert state.intent == "pickup"


def test_menuesuche_nach_dem_vorlesen_nimmt_den_alten_entwurf_raus():
    """Codex PR #127, P1: eine Korrektur der Gerichte beginnt mit search_menu.
    Endet der Zug mit einer Rueckfrage, bliebe der alte Entwurf readback_pending,
    und ein Ja auf die Rueckfrage bestaetigte ihn. Die Absicht bleibt Abholung."""
    state = make_state(stage="readback_pending", intent="pickup", order_id=uuid.uuid4())
    apply_tool_result(
        state, "search_menu", ToolResult(ok=True, data={"match_type": "ambiguous"})
    )
    assert state.order_id is None
    assert state.stage == "collecting"
    assert state.intent == "pickup"


def test_frage_zu_einem_gericht_laesst_den_entwurf_stehen():
    """Codex PR #127, P2: get_item_details beantwortet eine Frage (Allergene,
    Beschreibung) und aendert nichts. Der Gast hoert die Antwort und sagt ja -
    der vorgelesene Entwurf muss dann noch bestaetigbar sein. Eine Aenderung
    der Optionen geht nur ueber draft_order, und das loest ihn ab."""
    order_id = uuid.uuid4()
    state = make_state(stage="readback_pending", intent="pickup", order_id=order_id)
    apply_tool_result(
        state, "get_item_details", ToolResult(ok=True, data={"number": "47"})
    )
    assert state.order_id == order_id
    assert state.stage == "readback_pending"


def test_greeted_reaches_the_model_only_once_set():
    """The phone greeting comes from the code (telephony/handler.py); the model
    learns it was said and does not greet a second time."""
    state = make_state()
    assert "greeted" not in state.to_prompt_json()

    state.greeted = True

    assert state.to_prompt_json()["greeted"] is True


def test_available_transfer_keeps_the_target_number():
    state = make_state()
    apply_tool_result(
        state,
        "transfer_to_team",
        ToolResult(ok=True, data={"available": True, "transfer_to": "+497215550000"}),
    )
    assert state.transfer_to == "+497215550000"
    # The number is for the phone line, not for the model.
    assert "transfer_to" not in state.to_prompt_json()


def test_unavailable_transfer_keeps_no_target_number():
    state = make_state()
    apply_tool_result(
        state,
        "transfer_to_team",
        ToolResult(ok=True, data={"available": False, "transfer_to": "+497215550000"}),
    )
    assert state.transfer_to is None


def test_what_the_guest_called_for_is_kept():
    """Seen on a real model (07.10.2026): after "ich möchte etwas zum Abholen
    bestellen" the next turn began from a state without that wish, and "Es
    zwölf bitte" was read as a table for twelve. A model keeps nothing between
    turns and cannot write `intent`, so the core hears it."""
    state = make_state()

    note_intent(state, "Ich möchte etwas zum Abholen bestellen")
    note_intent(state, "Es zwölf bitte")

    assert state.to_prompt_json()["intent"] == "pickup"


@pytest.mark.parametrize(
    "later",
    [
        "Dürfen wir unseren Hund mitnehmen?",
        "Kann man die Reste dann mitnehmen?",
        "Wir kommen zu viert, zwei davon muss ich noch abholen",
        "Können wir das Essen schon vorbestellen?",
    ],
)
def test_word_in_a_later_sentence_does_not_replace_the_table(later):
    """Own review of PR #241: a side question with a pickup word flipped a
    call about a table to a pickup. Only the first wish of a call is taken."""
    state = make_state()
    assert heard(later) == "pickup"  # on its own the sentence is a pickup

    note_intent(state, "Einen Tisch für vier morgen um sieben")
    note_intent(state, later)

    assert state.intent == "reservation"


@pytest.mark.parametrize(
    "later",
    [
        "Ich hole es selbst, an welchem Tisch melde ich mich?",
        "Können wir solange an einem Tisch warten?",
    ],
)
def test_word_in_a_later_sentence_does_not_replace_the_pickup(later):
    state = make_state()
    assert heard(later) == "reservation"  # on its own the sentence is a table

    note_intent(state, "Ich möchte etwas zum Abholen bestellen")
    note_intent(state, later)

    assert state.intent == "pickup"


def test_wish_is_not_taken_against_the_table_details_in_the_state():
    """Second review of PR #241: a guest asks for a table without the word
    ("haben Sie morgen um sieben noch was frei für vier?"), the model notes
    the party size, and a side question then made the call a pickup."""
    state = make_state(slots={"party_size": 4})

    note_intent(state, "Können wir die Reste dann mitnehmen?")
    note_intent(state, "Können wir das Essen schon vorbestellen?")
    assert state.intent is None

    note_intent(state, "Ich möchte einen Tisch reservieren")
    assert state.intent == "reservation"


def test_wish_is_not_taken_against_the_order_in_the_state():
    state = make_state(cart=[{"menu_item_id": str(uuid.uuid4()), "quantity": 1}])

    note_intent(state, "An welchem Tisch warte ich dann?")
    assert state.intent is None

    note_intent(state, "Das ist zum Mitnehmen")
    assert state.intent == "pickup"


def test_wish_set_by_a_draft_is_not_replaced_either():
    """From the draft on `intent` follows the tools (`apply_tool_result`,
    `_drop_readback`), and it goes into the call log with the booking."""
    state = make_state(stage="readback_pending", intent="reservation")

    note_intent(state, "Und kann ich auch etwas zum Abholen bestellen?")

    assert state.intent == "reservation"


def test_first_wish_comes_after_a_sentence_without_one():
    state = make_state()

    note_intent(state, "Guten Tag")
    assert state.intent is None

    note_intent(state, "Einen Tisch für zwei bitte")
    assert state.intent == "reservation"


def test_call_that_ends_before_a_draft_is_logged_with_its_wish():
    state = make_state()

    note_intent(state, "Ich möchte etwas zum Abholen bestellen")

    assert call_outcome(state) == ("abandoned", "pickup")
