"""Spalte "Neue Bestellungen": Auswahl, Reihenfolge, Passt, Nochmal senden (T-4.7, docs/06 §3)."""

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import create_engine, func, select, update
from sqlalchemy.orm import Session

from api.core.errors import Conflict, NotFound
from api.domain.ordering.board import (
    approve_order,
    list_new,
    new_orders_change_token,
    resend_order,
)
from api.events.types import ORDER_CONFIRMED
from api.models import AuditLog, Order, OutboxEvent
from api.tests.test_domain_confirm_order import (
    _confirm,
    _draft,
    _mode,
)
from api.tests.test_domain_draft_order import NOW, _call, _tenant

TZ = "Europe/Berlin"


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
def call_id(session, tenant_id) -> uuid.UUID:
    return _call(session, tenant_id)


def _order(session, tenant_id, call_id, now=NOW, note=None) -> uuid.UUID:
    order_id = _draft(session, tenant_id, call_id, now=now, note=note)
    _confirm(session, tenant_id, call_id, order_id)
    return order_id


def _events(session, order_id) -> list[OutboxEvent]:
    return list(
        session.scalars(
            select(OutboxEvent)
            .where(
                OutboxEvent.event_type == ORDER_CONFIRMED,
                OutboxEvent.payload["order_id"].astext == str(order_id),
            )
            .order_by(OutboxEvent.created_at)
        )
    )


def _set(session, order_id, **values) -> None:
    session.execute(update(Order).where(Order.id == order_id).values(**values))
    session.commit()


def _ids(session, tenant_id, now=NOW) -> list[uuid.UUID]:
    return [o.order_id for o in list_new(session, tenant_id, TZ, now)]


# --- Auswahl und Darstellung ------------------------------------------------------


def test_bestaetigte_bestellung_steht_mit_positionen(session, tenant_id, call_id):
    order_id = _order(session, tenant_id, call_id, note="ohne Zwiebeln")

    [card] = list_new(session, tenant_id, TZ, NOW)
    assert card.order_id == order_id
    assert card.pickup_code == "A1"
    assert card.handover_state == "pending"  # primary: Kueche hat den Bon
    assert card.total_cents == 1380
    assert card.corrected is None
    [line] = card.lines
    assert (line.number, line.name, line.quantity) == ("23", "Frühlingsrollen", 2)
    assert line.note == "ohne Zwiebeln"


def test_entwurf_storno_fremder_mandant_und_gestern_fehlen(session, tenant_id, call_id):
    _draft(session, tenant_id, call_id)  # nur Entwurf
    storniert = _order(session, tenant_id, call_id)
    _set(session, storniert, status="cancelled")
    gestern = _order(session, tenant_id, call_id, now=NOW - timedelta(days=7))
    fremd_tid = _tenant(session, "Anderer Betrieb")
    _mode(session, fremd_tid, "primary")
    _order(session, fremd_tid, _call(session, fremd_tid))
    geloescht = _order(session, tenant_id, call_id)
    _set(session, geloescht, deleted_at=NOW)

    assert _ids(session, tenant_id) == []
    # Eine Woche davor steht er am eigenen Betriebstag noch.
    assert _ids(session, tenant_id, NOW - timedelta(days=7)) == [gestern]


def test_rote_karte_zuerst_auch_nach_passt_und_ueber_den_tag(
    session, tenant_id, call_id
):
    erste = _order(session, tenant_id, call_id)
    rot = _order(session, tenant_id, call_id, now=NOW - timedelta(days=7))
    approve_order(session, tenant_id, rot)
    assert rot not in _ids(session, tenant_id)

    _set(session, rot, handover_state="failed")
    # Abgehakt und vom Vortag, aber ohne Bon in der Kueche: bleibt oben stehen.
    assert _ids(session, tenant_id) == [rot, erste]


# --- Passt ------------------------------------------------------------------------


def test_passt_im_primaerbetrieb_hakt_ab_ohne_zweiten_bon(session, tenant_id, call_id):
    order_id = _order(session, tenant_id, call_id)
    approve_order(session, tenant_id, order_id)

    order = session.get(Order, order_id)
    session.refresh(order)
    assert order.status == "approved"
    assert order.handover_state == "pending"
    assert len(_events(session, order_id)) == 1
    assert _ids(session, tenant_id) == []
    audit = session.scalar(
        select(AuditLog).where(
            AuditLog.entity_id == order_id, AuditLog.action == "order.approved"
        )
    )
    assert audit.payload["released"] is False
    assert audit.actor == "gui:tablet"


@pytest.mark.parametrize("mode", ["overflow", "paused", "shadow"])
def test_passt_ausserhalb_primary_gibt_an_die_kueche(session, tenant_id, call_id, mode):
    _mode(session, tenant_id, mode)
    order_id = _order(session, tenant_id, call_id)
    assert _events(session, order_id) == []
    assert list_new(session, tenant_id, TZ, NOW)[0].handover_state is None

    approve_order(session, tenant_id, order_id)
    approve_order(session, tenant_id, order_id)  # zweites Tablet, gleicher Tap

    order = session.get(Order, order_id)
    session.refresh(order)
    assert (order.status, order.handover_state) == ("approved", "pending")
    [event] = _events(session, order_id)
    assert event.payload["revision"] == 0
    assert event.payload["correction_reason"] is None
    assert event.payload["pickup_code"] == "A1"
    count = session.scalar(
        select(func.count(AuditLog.id)).where(
            AuditLog.entity_id == order_id, AuditLog.action == "order.approved"
        )
    )
    assert count == 1


def test_passt_auf_roter_karte_ist_conflict(session, tenant_id, call_id):
    order_id = _order(session, tenant_id, call_id)
    _set(session, order_id, handover_state="failed")
    with pytest.raises(Conflict):
        approve_order(session, tenant_id, order_id)
    session.rollback()
    assert _ids(session, tenant_id) == [order_id]


def test_passt_unbekannt_fremd_oder_storniert(session, tenant_id, call_id):
    order_id = _order(session, tenant_id, call_id)
    fremd = _tenant(session, "Anderer Betrieb")
    with pytest.raises(NotFound):
        approve_order(session, fremd, order_id)
    session.rollback()
    with pytest.raises(NotFound):
        approve_order(session, tenant_id, uuid.uuid4())
    session.rollback()
    _set(session, order_id, status="cancelled")
    with pytest.raises(Conflict):
        approve_order(session, tenant_id, order_id)


# --- Nochmal senden ---------------------------------------------------------------


def test_nochmal_senden_ueberschreibt_unversuchten_bon(session, tenant_id, call_id):
    order_id = _order(session, tenant_id, call_id)
    _set(session, order_id, handover_state="failed")
    resend_order(session, tenant_id, order_id)

    order = session.get(Order, order_id)
    session.refresh(order)
    assert order.handover_state == "pending"
    # Der erste Bon wurde nie versucht: derselbe Eintrag, kein zweiter Bon.
    assert len(_events(session, order_id)) == 1
    assert _ids(session, tenant_id) == [order_id]  # noch nicht abgehakt


def test_nochmal_senden_nach_versuch_legt_neuen_bon_an(session, tenant_id, call_id):
    order_id = _order(session, tenant_id, call_id)
    session.execute(
        update(OutboxEvent)
        .where(OutboxEvent.payload["order_id"].astext == str(order_id))
        .values(status="failed", attempts=4)
    )
    _set(session, order_id, handover_state="failed")
    resend_order(session, tenant_id, order_id)
    resend_order(session, tenant_id, order_id)  # schon pending: nichts mehr

    events = _events(session, order_id)
    assert [e.status for e in events] == ["failed", "pending"]
    assert events[1].payload["correction_reason"] is None


# --- Ereignisstrom ----------------------------------------------------------------


def test_token_aendert_sich_bei_neu_passt_und_rot(session, tenant_id, call_id):
    leer = new_orders_change_token(session, tenant_id, TZ, NOW)
    order_id = _order(session, tenant_id, call_id)
    neu = new_orders_change_token(session, tenant_id, TZ, NOW)
    assert neu != leer
    assert new_orders_change_token(session, tenant_id, TZ, NOW) == neu

    approve_order(session, tenant_id, order_id)
    abgehakt = new_orders_change_token(session, tenant_id, TZ, NOW)
    assert abgehakt == leer

    # Rohes UPDATE ohne ORM: der Strom sieht es trotzdem.
    _set(session, order_id, handover_state="failed")
    assert new_orders_change_token(session, tenant_id, TZ, NOW) != abgehakt
