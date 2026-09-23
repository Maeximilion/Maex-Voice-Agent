"""confirm fuer Bestellungen: Abholcode, Uebergabe je Modus, Idempotenz (docs/04 §confirm)."""

import threading
import uuid
from datetime import datetime, time, timedelta
from functools import partial

import pytest
from sqlalchemy import create_engine, func, select, update
from sqlalchemy.orm import Session

from api.core.errors import Conflict, NotFound
from api.domain.confirm import confirm
from api.domain.ordering import draft_order
from api.events.types import ORDER_CONFIRMED
from api.models import AuditLog, Order, OutboxEvent, ServiceConfig
from api.schemas.confirm import ConfirmRequest
from api.tests.test_domain_draft_order import (
    BERLIN,
    DIENSTAG,
    NOW,
    _call,
    _tenant,
    item,
    request,
)


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


def _mode(session, tenant_id, mode: str) -> None:
    session.execute(
        update(ServiceConfig)
        .where(ServiceConfig.tenant_id == tenant_id)
        .values(call_mode=mode)
    )
    session.commit()


def _draft(session, tenant_id, call_id, now=NOW, note=None) -> uuid.UUID:
    items = [
        {"menu_item_id": item(session, tenant_id, "23"), "quantity": 2, "note": note}
    ]
    return draft_order(
        session, request(session, tenant_id, call_id, items), now=now
    ).order_id


def _confirm(session, tenant_id, call_id, order_id, key=None):
    req = ConfirmRequest(
        call_id=call_id,
        tenant_id=tenant_id,
        entity="order",
        entity_id=order_id,
        idempotency_key=key or uuid.uuid4().hex,
    )
    return confirm(session, req)


def _events(session) -> list[OutboxEvent]:
    return list(
        session.scalars(
            select(OutboxEvent).where(OutboxEvent.event_type == ORDER_CONFIRMED)
        )
    )


# --- Normalfall --------------------------------------------------------------------


def test_bestaetigt_mit_abholcode_audit_und_ereignis(session, tenant_id, call_id):
    order_id = _draft(session, tenant_id, call_id, note="ohne Zwiebeln")
    result = _confirm(session, tenant_id, call_id, order_id)

    assert result.status == "confirmed"
    assert result.handover == "queued"
    assert result.pickup_code == "A1"

    order = session.get(Order, order_id)
    session.refresh(order)
    assert order.status == "confirmed"
    assert order.pickup_code == "A1"
    assert order.handover_state == "pending"

    audit = session.scalar(
        select(AuditLog).where(
            AuditLog.entity_id == order_id, AuditLog.action == "order.confirmed"
        )
    )
    assert audit is not None and audit.payload["pickup_code"] == "A1"

    [event] = _events(session)
    # Vollstaendig, damit die Kueche ohne zweite Abfrage druckt.
    assert event.payload["order_id"] == str(order_id)
    assert event.payload["pickup_code"] == "A1"
    assert event.payload["total_cents"] == 1380
    assert event.payload["items"] == [
        {
            "number": "23",
            "name": "Frühlingsrollen",
            "quantity": 2,
            "unit_price_cents": 690,
            "options": [],
            "note": "ohne Zwiebeln",
        }
    ]


def test_abholcodes_zaehlen_je_betriebstag_hoch(session, tenant_id, call_id):
    codes = [
        _confirm(
            session, tenant_id, call_id, _draft(session, tenant_id, call_id)
        ).pickup_code
        for _ in range(3)
    ]
    assert codes == ["A1", "A2", "A3"]


def test_neuer_betriebstag_beginnt_wieder_bei_eins(session, tenant_id, call_id):
    _confirm(session, tenant_id, call_id, _draft(session, tenant_id, call_id))
    mittwoch = datetime.combine(
        DIENSTAG + timedelta(days=1), time(18, 0), tzinfo=BERLIN
    )
    order_id = _draft(session, tenant_id, call_id, now=mittwoch)
    assert _confirm(session, tenant_id, call_id, order_id).pickup_code == "A1"


def test_betriebstag_nach_mitternacht_zaehlt_zum_vortag(session, tenant_id, call_id):
    """Der Betriebstag beginnt um 05:00 (core/time): 00:30 gehoert zum Abend davor."""
    _confirm(session, tenant_id, call_id, _draft(session, tenant_id, call_id))
    # Abholung bis 22:00, deshalb direkt in die Tabelle statt ueber draft_order.
    spaet = datetime.combine(DIENSTAG + timedelta(days=1), time(0, 30), tzinfo=BERLIN)
    order_id = _draft(session, tenant_id, call_id)
    session.execute(update(Order).where(Order.id == order_id).values(created_at=spaet))
    session.commit()
    assert _confirm(session, tenant_id, call_id, order_id).pickup_code == "A2"


# --- Modus -------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["overflow", "shadow", "paused"])
def test_ohne_primaerbetrieb_wartet_die_bestellung_auf_freigabe(
    session, tenant_id, call_id, mode
):
    """docs/02: jede KI-Bestellung ausserhalb von primary braucht die Freigabe
    in der GUI. Bis dahin geht nichts an die Kueche."""
    _mode(session, tenant_id, mode)
    order_id = _draft(session, tenant_id, call_id)
    result = _confirm(session, tenant_id, call_id, order_id)

    assert result.status == "confirmed"
    assert result.handover == "awaiting_approval"
    assert result.pickup_code == "A1"
    order = session.get(Order, order_id)
    session.refresh(order)
    assert order.handover_state is None
    assert _events(session) == []


# --- Idempotenz --------------------------------------------------------------------


def test_zweiter_aufruf_liefert_dasselbe_ohne_zweites_ereignis(
    session, tenant_id, call_id
):
    order_id = _draft(session, tenant_id, call_id)
    first = _confirm(session, tenant_id, call_id, order_id)
    # Anderer Schluessel: der Zustand traegt die Idempotenz, nicht der Schluessel.
    again = _confirm(session, tenant_id, call_id, order_id)

    assert again == first
    assert len(_events(session)) == 1
    audits = session.scalar(
        select(func.count())
        .select_from(AuditLog)
        .where(AuditLog.entity_id == order_id, AuditLog.action == "order.confirmed")
    )
    assert audits == 1


def test_modus_wechsel_danach_aendert_die_antwort_nicht(session, tenant_id, call_id):
    order_id = _draft(session, tenant_id, call_id)
    first = _confirm(session, tenant_id, call_id, order_id)
    _mode(session, tenant_id, "overflow")
    assert _confirm(session, tenant_id, call_id, order_id) == first


# --- Grenzfaelle -------------------------------------------------------------------


def test_stornierte_bestellung_ist_conflict(session, tenant_id, call_id):
    order_id = _draft(session, tenant_id, call_id)
    session.execute(
        update(Order).where(Order.id == order_id).values(status="cancelled")
    )
    session.commit()
    with pytest.raises(Conflict) as err:
        _confirm(session, tenant_id, call_id, order_id)
    assert "storniert" in err.value.say


def test_geloeschte_bestellung_ist_not_found(session, tenant_id, call_id):
    order_id = _draft(session, tenant_id, call_id)
    session.execute(update(Order).where(Order.id == order_id).values(deleted_at=NOW))
    session.commit()
    with pytest.raises(NotFound):
        _confirm(session, tenant_id, call_id, order_id)


def test_bestellung_aus_einem_anderen_anruf_ist_not_found(session, tenant_id, call_id):
    """Das Ja gehoert dem Gast in der Leitung (CLAUDE.md §2 Regel 3)."""
    order_id = _draft(session, tenant_id, call_id)
    other_call = _call(session, tenant_id)
    with pytest.raises(NotFound):
        _confirm(session, tenant_id, other_call, order_id)
    assert session.get(Order, order_id).status == "draft"


def test_bestellung_eines_anderen_mandanten_ist_not_found(session, tenant_id, call_id):
    order_id = _draft(session, tenant_id, call_id)
    other = _tenant(session, "Anderer Betrieb")
    with pytest.raises(NotFound):
        _confirm(session, other, _call(session, other), order_id)


def test_unbekannte_bestellung_ist_not_found(session, tenant_id, call_id):
    with pytest.raises(NotFound):
        _confirm(session, tenant_id, call_id, uuid.uuid4())


# --- Nebenlaeufigkeit --------------------------------------------------------------


def _parallel(engine, calls) -> list[object]:
    start = threading.Barrier(len(calls))
    results: list[object] = []
    lock = threading.Lock()

    def run(fn) -> None:
        start.wait(timeout=10)
        with Session(engine) as own:
            try:
                outcome: object = fn(own)
            except Exception as exc:  # noqa: BLE001 - im Test soll jeder Fehler auffallen
                outcome = exc
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=run, args=(fn,)) for fn in calls]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    return results


def test_parallel_auf_dieselbe_bestellung_ein_ereignis(migrated_db_url):
    engine = create_engine(migrated_db_url)
    with Session(engine) as s:
        tid = _tenant(s, "Testbetrieb")
        _mode(s, tid, "primary")
        cid = _call(s, tid)
        order_id = _draft(s, tid, cid)

    results = _parallel(
        engine,
        [lambda own: _confirm(own, tid, cid, order_id).pickup_code for _ in range(8)],
    )
    with Session(engine) as s:
        events = len(_events(s))
    engine.dispose()

    assert results == ["A1"] * 8, results
    assert events == 1


def _code_for(own, *, tid, cid, oid) -> str:
    return _confirm(own, tid, cid, oid).pickup_code


def test_parallel_auf_verschiedene_bestellungen_verschiedene_codes(migrated_db_url):
    engine = create_engine(migrated_db_url)
    with Session(engine) as s:
        tid = _tenant(s, "Testbetrieb")
        _mode(s, tid, "primary")
        cid = _call(s, tid)
        orders = [_draft(s, tid, cid) for _ in range(6)]

    results = _parallel(
        engine,
        [partial(_code_for, tid=tid, cid=cid, oid=oid) for oid in orders],
    )
    engine.dispose()

    assert sorted(results) == sorted(f"A{n}" for n in range(1, 7)), results


def test_confirm_wartet_auf_laufenden_not_aus(migrated_db_url):
    """Codex PR #125, P1: ein Not-Aus, der gerade laeuft (Zeile gesperrt, Modus
    schon paused, noch nicht committed), darf von confirm nicht ueberholt werden.
    Sonst las confirm den alten Modus primary und schickte an die Kueche."""
    engine = create_engine(migrated_db_url)
    with Session(engine) as s:
        tid = _tenant(s, "Testbetrieb")
        _mode(s, tid, "primary")
        cid = _call(s, tid)
        order_id = _draft(s, tid, cid)

    results: list[object] = []

    def run() -> None:
        with Session(engine) as own:
            try:
                results.append(_confirm(own, tid, cid, order_id))
            except Exception as exc:  # noqa: BLE001 - im Test soll jeder Fehler auffallen
                results.append(exc)

    with Session(engine) as pauser:
        config = pauser.scalars(
            select(ServiceConfig)
            .where(ServiceConfig.tenant_id == tid)
            .with_for_update()
        ).one()
        config.call_mode = "paused"
        pauser.flush()

        thread = threading.Thread(target=run)
        thread.start()
        thread.join(timeout=1.5)
        blocked = thread.is_alive()
        pauser.commit()
    thread.join(timeout=10)

    with Session(engine) as s:
        events = len(_events(s))
    engine.dispose()

    assert blocked, "confirm muss auf den laufenden Not-Aus warten"
    [result] = results
    assert result.handover == "awaiting_approval", result
    assert events == 0
