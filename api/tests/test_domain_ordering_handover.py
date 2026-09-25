"""Kuechenbon ueber die Druckbruecke: abholen, Rueckmeldung, Revision, Waechter (T-4.6)."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import Session

from api.config import settings
from api.core.errors import NotFound
from api.db import get_db
from api.domain.ordering.board import resend_order
from api.domain.ordering.correction import RowEdit
from api.domain.ordering.handover import (
    LEASE_SECONDS,
    PICKUP_TIMEOUT_SECONDS,
    ack_ticket,
    claim_tickets,
    sweep,
)
from api.events.dispatcher import MAX_ATTEMPTS, dispatch_once
from api.events.types import ORDER_CONFIRMED, ORDER_HANDOVER_FAILED
from api.main import app
from api.models import Order, OrderItem, OutboxEvent
from api.tests.conftest import LATENZ_RUNDEN, p95_ms
from api.tests.test_domain_confirm_order import _confirm, _draft, _mode
from api.tests.test_domain_draft_order import _call, _tenant
from api.tests.test_domain_ordering_correction import _fix
from api.tests.test_events_dispatcher import FakeN8n


@pytest.fixture
def session(migrated_db_url):
    engine = create_engine(migrated_db_url)
    with Session(engine) as s:
        yield s
    engine.dispose()


@pytest.fixture
def tenant_id(session) -> uuid.UUID:
    tid = _tenant(session, "Testbetrieb")
    _mode(session, tid, "primary")
    return tid


@pytest.fixture
def order_id(session, tenant_id) -> uuid.UUID:
    call_id = _call(session, tenant_id)
    oid = _draft(session, tenant_id, call_id)
    _confirm(session, tenant_id, call_id, oid)
    return oid


def _now(seconds: float = 1) -> datetime:
    # next_attempt_at kommt aus der echten Uhr der Datenbank (Server-Default).
    return datetime.now(UTC) + timedelta(seconds=seconds)


def _order(session, order_id) -> Order:
    return session.scalars(
        select(Order)
        .where(Order.id == order_id)
        .execution_options(populate_existing=True)
    ).one()


def _events(session, order_id, event_type=ORDER_CONFIRMED) -> list[OutboxEvent]:
    return list(
        session.scalars(
            select(OutboxEvent)
            .where(
                OutboxEvent.event_type == event_type,
                OutboxEvent.payload["order_id"].astext == str(order_id),
            )
            .order_by(OutboxEvent.created_at)
            .execution_options(populate_existing=True)
        )
    )


def _fix_quantity(session, tenant_id, order_id):
    row = session.scalars(
        select(OrderItem).where(OrderItem.order_id == order_id)
    ).first()
    return _fix(session, tenant_id, order_id, rows=(RowEdit(row.id, row.quantity + 1),))


# --- Normalfall -------------------------------------------------------------------


def test_bon_abholen_drucken_melden(session, tenant_id, order_id):
    assert _order(session, order_id).handover_state == "pending"
    tickets = claim_tickets(session, tenant_id, _now())
    assert len(tickets) == 1
    ticket = tickets[0]
    assert ticket["attempt"] == 1
    assert ticket["ticket"]["order_id"] == str(order_id)
    assert ticket["ticket"]["revision"] == 0
    # Ausgeliehen: kein zweites Abholen, solange die Bruecke druckt.
    assert claim_tickets(session, tenant_id, _now()) == []

    assert (
        ack_ticket(session, tenant_id, uuid.UUID(ticket["id"]), True, _now()) == "sent"
    )
    assert _order(session, order_id).handover_state == "sent"
    [event] = _events(session, order_id)
    assert (event.status, event.attempts, event.last_error) == ("sent", 1, None)
    # Wiederholte Rueckmeldung (Netz weg nach dem Druck) aendert nichts.
    again = ack_ticket(session, tenant_id, event.id, False, _now(), "zu spaet")
    assert again == "sent"
    assert _order(session, order_id).handover_state == "sent"


def test_dispatcher_laesst_den_bon_der_bruecke(session, tenant_id, order_id):
    n8n = FakeN8n()
    dispatch_once(session, n8n, now=_now())
    assert n8n.received == []
    [event] = _events(session, order_id)
    assert (event.status, event.attempts) == ("pending", 0)


# --- Fehler und Wiederholung ------------------------------------------------------


def test_druckfehler_macht_die_karte_sofort_rot_und_heilt(session, tenant_id, order_id):
    [ticket] = claim_tickets(session, tenant_id, _now())
    event_id = uuid.UUID(ticket["id"])
    status = ack_ticket(session, tenant_id, event_id, False, _now(), "Drucker offline")
    assert status == "pending"
    assert _order(session, order_id).handover_state == "failed"
    [alarm] = _events(session, order_id, ORDER_HANDOVER_FAILED)
    assert alarm.payload["reason"] == "Drucker offline"
    [event] = _events(session, order_id)
    assert event.last_error == "Drucker offline"

    # Backoff: erst nach 5 s wieder faellig, dann klappt der Druck.
    assert claim_tickets(session, tenant_id, _now(1)) == []
    [retry] = claim_tickets(session, tenant_id, _now(6))
    assert (retry["id"], retry["attempt"]) == (ticket["id"], 2)
    ack_ticket(session, tenant_id, event_id, True, _now(6))
    assert _order(session, order_id).handover_state == "sent"
    # Nur ein Alarm fuer den einen Wechsel auf rot.
    assert len(_events(session, order_id, ORDER_HANDOVER_FAILED)) == 1


def test_ohne_rueckmeldung_nach_der_leihfrist_wieder_faellig(
    session, tenant_id, order_id
):
    [first] = claim_tickets(session, tenant_id, _now())
    assert claim_tickets(session, tenant_id, _now(LEASE_SECONDS - 5)) == []
    [again] = claim_tickets(session, tenant_id, _now(LEASE_SECONDS + 2))
    assert (again["id"], again["attempt"]) == (first["id"], 2)


def test_letzter_fehlversuch_ist_endgueltig(session, tenant_id, order_id):
    [ticket] = claim_tickets(session, tenant_id, _now())
    event_id = uuid.UUID(ticket["id"])
    session.execute(
        update(OutboxEvent)
        .where(OutboxEvent.id == event_id)
        .values(attempts=MAX_ATTEMPTS)
    )
    session.commit()
    assert (
        ack_ticket(session, tenant_id, event_id, False, _now(), "Papier leer")
        == "failed"
    )
    assert _order(session, order_id).handover_state == "failed"


# --- Revision: nie ein ueberholter Stand ----------------------------------------


def test_ueberholter_bon_wird_nicht_mehr_ausgeliefert(session, tenant_id, order_id):
    [first] = claim_tickets(session, tenant_id, _now())
    # Die Kueche hat Revision 0 nie bestaetigt, das Team korrigiert.
    _fix_quantity(session, tenant_id, order_id)
    old, new = _events(session, order_id)
    assert (old.payload["revision"], new.payload["revision"]) == (0, 1)

    tickets = claim_tickets(session, tenant_id, _now(LEASE_SECONDS + 2))
    assert [t["ticket"]["revision"] for t in tickets] == [1]
    old, new = _events(session, order_id)
    assert old.status == "sent"
    assert "ueberholt von Revision 1" in old.last_error
    assert first["id"] == str(old.id)


def test_fehler_des_alten_bons_faerbt_die_karte_nicht(session, tenant_id, order_id):
    [first] = claim_tickets(session, tenant_id, _now())
    _fix_quantity(session, tenant_id, order_id)
    [second] = claim_tickets(session, tenant_id, _now())
    assert second["ticket"]["revision"] == 1
    ack_ticket(session, tenant_id, uuid.UUID(second["id"]), True, _now())
    assert _order(session, order_id).handover_state == "sent"

    ack_ticket(session, tenant_id, uuid.UUID(first["id"]), False, _now(), "Timeout")
    assert _order(session, order_id).handover_state == "sent"
    assert _events(session, order_id, ORDER_HANDOVER_FAILED) == []


# --- Waechter: Bruecke aus, Netz weg ----------------------------------------------


def test_unabgeholter_bon_macht_die_karte_rot(session, tenant_id, order_id):
    assert sweep(session, _now(PICKUP_TIMEOUT_SECONDS - 5)) == 0
    assert _order(session, order_id).handover_state == "pending"

    assert sweep(session, _now(PICKUP_TIMEOUT_SECONDS + 2)) == 1
    assert _order(session, order_id).handover_state == "failed"
    [alarm] = _events(session, order_id, ORDER_HANDOVER_FAILED)
    assert alarm.payload["reason"] == "Druckbruecke hat den Bon nicht abgeholt"
    # Zweiter Durchlauf: kein zweiter Alarm.
    assert sweep(session, _now(PICKUP_TIMEOUT_SECONDS + 10)) == 0
    assert len(_events(session, order_id, ORDER_HANDOVER_FAILED)) == 1

    # Die Bruecke kommt zurueck: der Bon wartet noch und wird gedruckt.
    [ticket] = claim_tickets(session, tenant_id, _now())
    ack_ticket(session, tenant_id, uuid.UUID(ticket["id"]), True, _now())
    assert _order(session, order_id).handover_state == "sent"


def test_nochmal_senden_bei_unabgeholtem_bon_ohne_zweiten_bon(
    session, tenant_id, order_id
):
    sweep(session, _now(PICKUP_TIMEOUT_SECONDS + 2))
    resend_order(session, tenant_id, order_id)
    assert _order(session, order_id).handover_state == "pending"
    [event] = _events(session, order_id)
    assert (event.status, event.attempts) == ("pending", 0)


def test_waechter_beendet_bon_ohne_versuche(session, tenant_id, order_id):
    [ticket] = claim_tickets(session, tenant_id, _now())
    session.execute(
        update(OutboxEvent)
        .where(OutboxEvent.id == uuid.UUID(ticket["id"]))
        .values(attempts=MAX_ATTEMPTS)
    )
    session.commit()
    # Nach Leihfrist plus Frist: niemand holt ihn mehr ab.
    later = _now(LEASE_SECONDS + PICKUP_TIMEOUT_SECONDS + 2)
    assert claim_tickets(session, tenant_id, later) == []
    sweep(session, later)
    [event] = _events(session, order_id)
    assert event.status == "failed"
    assert _order(session, order_id).handover_state == "failed"


# --- Mandanten und HTTP -----------------------------------------------------------


def test_fremder_mandant_sieht_keinen_bon(session, tenant_id, order_id):
    other = _tenant(session, "Anderer Betrieb")
    assert claim_tickets(session, other, _now()) == []
    [ticket] = claim_tickets(session, tenant_id, _now())
    with pytest.raises(NotFound):
        ack_ticket(session, other, uuid.UUID(ticket["id"]), True, _now())


@pytest.fixture
def http(migrated_db_url, monkeypatch):
    engine = create_engine(migrated_db_url)

    def override_get_db():
        with Session(engine) as s:
            yield s

    app.dependency_overrides[get_db] = override_get_db
    monkeypatch.setattr(settings, "kitchen_bridge_token", "bruecke-geheim")
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def test_http_token_und_huelle(http, session, tenant_id, order_id, monkeypatch):
    body = {"tenant_id": str(tenant_id)}
    agent = {"Authorization": f"Bearer {settings.agent_api_token}"}
    assert http.post("/v1/kitchen/claim", json=body, headers=agent).status_code == 401
    auth = {"Authorization": "Bearer bruecke-geheim"}

    response = http.post("/v1/kitchen/claim", json=body, headers=auth)
    assert response.status_code == 200
    [ticket] = response.json()["data"]["tickets"]
    ack = http.post(
        "/v1/kitchen/ack",
        json={**body, "event_id": ticket["id"], "ok": True},
        headers=auth,
    )
    assert ack.json()["data"] == {"status": "sent"}
    unknown = http.post(
        "/v1/kitchen/ack",
        json={**body, "event_id": str(uuid.uuid4()), "ok": True},
        headers=auth,
    )
    # Huelle wie bei den Tools: Fehler als ok=false mit Code (docs/04 §1).
    assert unknown.json()["ok"] is False
    assert unknown.json()["error"]["code"] == "not_found"

    # Ohne gesetztes Token ist der Eingang zu, auch fuer "Bearer ".
    monkeypatch.setattr(settings, "kitchen_bridge_token", "")
    empty = {"Authorization": "Bearer "}
    assert http.post("/v1/kitchen/claim", json=body, headers=empty).status_code == 401


def test_http_abholen_unter_300_ms(http, tenant_id):
    auth = {"Authorization": "Bearer bruecke-geheim"}
    body = {"tenant_id": str(tenant_id)}
    ms = p95_ms(
        lambda: http.post("/v1/kitchen/claim", json=body, headers=auth),
        rounds=LATENZ_RUNDEN,
    )
    print(f"p95 /v1/kitchen/claim: {ms:.1f} ms")
    assert ms < 300
