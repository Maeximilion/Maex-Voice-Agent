"""agent/dispatch.py: Menue-Tools fuer den Agenten (search_menu, get_item_details,
draft_order, confirm fuer Bestellungen).

Die Tools laufen wie die Stufe-1-Tools ohne HTTP-Umweg. Zwei Dinge sind hier
anders als ueber HTTP: search_menu zerlegt einen Satz mit mehreren Positionen
vorher und sucht je Teil (docs/04 §search_menu: die Zerlegung gehoert zum
Bestellfluss), und draft_order bekommt seinen idempotency_key vom Code, nie
vom Modell.
"""

import uuid

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from api.agent.dispatch import dispatch
from api.agent.state import ConversationState, apply_tool_result
from api.models import Call, MenuItem, Order
from api.tests.test_domain_draft_order import NOW, _call, _tenant


@pytest.fixture
def session(migrated_db_url):
    engine = create_engine(migrated_db_url)
    with Session(engine) as s:
        yield s
    engine.dispose()


@pytest.fixture
def tenant_id(session) -> uuid.UUID:
    return _tenant(session, "Testbetrieb")


@pytest.fixture
def call_id(session, tenant_id) -> uuid.UUID:
    return _call(session, tenant_id)


def run(session, tenant_id, call_id, name, args):
    return dispatch(session, call_id, tenant_id, name, args, now=NOW)


def item_id(session, tenant_id, number: str) -> str:
    return str(
        session.scalar(
            select(MenuItem.id).where(
                MenuItem.tenant_id == tenant_id, MenuItem.number == number
            )
        )
    )


def order_body(session, tenant_id, **over) -> dict:
    return {
        "type": "pickup",
        "customer": {"name": "Müller", "phone": "0721 555 1234"},
        "items": [{"menu_item_id": item_id(session, tenant_id, "23"), "quantity": 2}],
        **over,
    }


# --- search_menu -------------------------------------------------------------------


def test_search_menu_eine_position(session, tenant_id, call_id):
    result = run(session, tenant_id, call_id, "search_menu", {"query": "die 23"})

    assert result.ok
    assert result.data["match_type"] == "exact_number"
    assert [r["number"] for r in result.data["results"]] == ["23"]


def test_search_menu_zerlegt_mehrere_positionen(session, tenant_id, call_id):
    """Ueber HTTP waere das ambiguous ("eins nach dem anderen"). Der Agent fragt
    stattdessen je Teil und bekommt alle Positionen in einem Zug zurueck."""
    result = run(
        session,
        tenant_id,
        call_id,
        "search_menu",
        {"query": "die 23 und einmal Pho Bo"},
    )

    assert result.ok
    assert result.data["match_type"] == "positions"
    parts = result.data["positions"]
    assert [p["query"] for p in parts] == ["die 23", "einmal Pho Bo"]
    assert [p["results"][0]["number"] for p in parts] == ["23", "13"]
    assert all(p["ok"] for p in parts)


def test_search_menu_teil_nicht_gefunden_bleibt_sichtbar(session, tenant_id, call_id):
    """Ein Teil ohne Treffer faellt nicht weg: er steht mit error_code und say da."""
    result = run(
        session, tenant_id, call_id, "search_menu", {"query": "die 23 und die 99"}
    )

    assert result.ok
    first, second = result.data["positions"]
    assert first["ok"] and first["results"][0]["number"] == "23"
    assert not second["ok"]
    assert second["error_code"] == "not_found"
    assert "99" in second["say"]


def test_search_menu_nicht_gefunden_als_ganzes(session, tenant_id, call_id):
    result = run(session, tenant_id, call_id, "search_menu", {"query": "Schnitzel"})

    assert not result.ok
    assert result.error_code == "not_found"
    assert result.say


# --- get_item_details --------------------------------------------------------------


def test_get_item_details(session, tenant_id, call_id):
    result = run(
        session,
        tenant_id,
        call_id,
        "get_item_details",
        {"menu_item_id": item_id(session, tenant_id, "47"), "allergen_question": False},
    )

    assert result.ok
    assert result.data["number"] == "47"
    assert {g["group"] for g in result.data["option_groups"]} == {"Fleisch", "Sauce"}


def test_get_item_details_ohne_allergenfrage_ist_invalid_input(
    session, tenant_id, call_id
):
    result = run(
        session,
        tenant_id,
        call_id,
        "get_item_details",
        {"menu_item_id": item_id(session, tenant_id, "47")},
    )
    assert not result.ok and result.error_code == "invalid_input"


# --- draft_order und confirm -------------------------------------------------------


def test_draft_order_erzeugt_eigenen_idempotency_key(session, tenant_id, call_id):
    body = order_body(session, tenant_id)
    first = run(session, tenant_id, call_id, "draft_order", body)
    # Modell-Retry mit denselben Angaben: kein zweiter Entwurf.
    again = run(session, tenant_id, call_id, "draft_order", body)

    assert first.ok and again.ok
    assert first.data["order_id"] == again.data["order_id"]
    assert first.data["readback"].startswith("Zweimal Nummer 23 Frühlingsrollen.")
    assert session.scalar(select(func.count()).select_from(Order)) == 1


def test_draft_order_mit_geaenderter_menge_ist_ein_neuer_entwurf(
    session, tenant_id, call_id
):
    """Korrigiert der Gast nach dem Vorlesen, entsteht ein neuer Entwurf mit
    neuem readback - sonst bestaetigte das naechste Ja die alte Menge."""
    first = run(
        session, tenant_id, call_id, "draft_order", order_body(session, tenant_id)
    )
    items = [{"menu_item_id": item_id(session, tenant_id, "23"), "quantity": 3}]
    second = run(
        session,
        tenant_id,
        call_id,
        "draft_order",
        order_body(session, tenant_id, items=items),
    )

    assert second.data["order_id"] != first.data["order_id"]
    assert second.data["readback"].startswith("Dreimal")


def test_draft_order_fehler_kommt_mit_say(session, tenant_id, call_id):
    items = [{"menu_item_id": item_id(session, tenant_id, "47"), "quantity": 1}]
    result = run(
        session,
        tenant_id,
        call_id,
        "draft_order",
        order_body(session, tenant_id, items=items),
    )

    assert not result.ok
    assert result.error_code == "invalid_input"
    assert result.say == "Welche Auswahl bei Fleisch möchten Sie zu Ente knusprig?"


def test_confirm_fuer_bestellung(session, tenant_id, call_id):
    draft = run(
        session, tenant_id, call_id, "draft_order", order_body(session, tenant_id)
    )
    result = run(
        session,
        tenant_id,
        call_id,
        "confirm",
        {"entity": "order", "entity_id": draft.data["order_id"]},
    )

    assert result.ok
    assert result.data["pickup_code"] == "A1"


def test_jeder_aufruf_landet_in_tool_calls(session, tenant_id, call_id):
    run(session, tenant_id, call_id, "search_menu", {"query": "die 23"})
    run(session, tenant_id, call_id, "draft_order", order_body(session, tenant_id))
    session.expire_all()
    names = [e["name"] for e in session.get(Call, call_id).tool_calls]
    assert names == ["search_menu", "draft_order"]


# --- Zustand -----------------------------------------------------------------------


def test_draft_order_setzt_readback_pending_und_order_id(session, tenant_id, call_id):
    state = ConversationState(call_id=call_id, tenant_id=tenant_id)
    result = run(
        session, tenant_id, call_id, "draft_order", order_body(session, tenant_id)
    )
    apply_tool_result(state, "draft_order", result)

    assert state.stage == "readback_pending"
    assert state.intent == "pickup"
    assert str(state.order_id) == result.data["order_id"]
    # Ohne order_id im Prompt kann das Modell beim Ja kein entity_id liefern.
    assert state.to_prompt_json()["order_id"] == result.data["order_id"]
