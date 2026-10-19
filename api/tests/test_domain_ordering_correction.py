"""Korrektur im Tablet: Mengen, Tauschen, Dazunehmen, Hinweise, Preise, Bon (T-4.7, docs/06 §3)."""

import uuid

import pytest
from sqlalchemy import create_engine, func, select, update
from sqlalchemy.orm import Session

from api.core.errors import Conflict, InvalidInput
from api.domain.ordering import draft_order
from api.domain.ordering.board import approve_order, list_new
from api.domain.ordering.correction import (
    NO_CHANGE,
    NO_POSITION,
    AddEdit,
    Choice,
    CorrectionRequest,
    RowEdit,
    apply_correction,
    find_by_number,
    preview_correction,
)
from api.events.types import ORDER_CONFIRMED
from api.models import AuditLog, MenuItem, Order, OrderItem, OutboxEvent
from api.tests.test_domain_confirm_order import (
    _confirm,
    _mode,
)
from api.tests.test_domain_draft_order import NOW, _call, _tenant, item, request

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


def _order(session, tenant_id, call_id, items=None, mode="overflow") -> uuid.UUID:
    _mode(session, tenant_id, mode)
    items = items or [
        {"menu_item_id": item(session, tenant_id, "23"), "quantity": 2, "note": None}
    ]
    req = request(session, tenant_id, call_id, items)
    order_id = draft_order(session, req, now=NOW).order_id
    _confirm(session, tenant_id, call_id, order_id)
    return order_id


def _rows(session, order_id) -> list[OrderItem]:
    return list(
        session.scalars(
            select(OrderItem)
            .where(OrderItem.order_id == order_id)
            .order_by(OrderItem.created_at)
            .execution_options(populate_existing=True)
        )
    )


def _req(
    session, tenant_id, order_id, rows=(), added=(), edit_id=""
) -> CorrectionRequest:
    version = preview_correction(session, tenant_id, order_id, now=NOW).version
    return CorrectionRequest(version=version, rows=rows, added=added, edit_id=edit_id)


def _fix(session, tenant_id, order_id, reason="wrong_quantity", **kw):
    req = _req(session, tenant_id, order_id, **kw)
    return apply_correction(session, tenant_id, order_id, req, reason, now=NOW)


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


def _order_row(session, order_id) -> Order:
    order = session.get(Order, order_id)
    session.refresh(order)
    return order


def _corrections(session, order_id) -> list[AuditLog]:
    return list(
        session.scalars(
            select(AuditLog)
            .where(AuditLog.entity_id == order_id, AuditLog.action == "order.corrected")
            .order_by(AuditLog.id)
        )
    )


# --- Normalfall --------------------------------------------------------------------


def test_menge_aendern_rechnet_neu_und_zaehlt_die_korrektur(
    session, tenant_id, call_id
):
    order_id = _order(session, tenant_id, call_id)
    [row] = _rows(session, order_id)
    _fix(session, tenant_id, order_id, rows=(RowEdit(row.id, 3),))

    order = _order_row(session, order_id)
    assert (order.items_total_cents, order.total_cents) == (2070, 2070)
    assert _rows(session, order_id)[0].quantity == 3
    [audit] = _corrections(session, order_id)
    assert audit.actor == "gui:tablet"
    assert audit.payload["reason"] == "wrong_quantity"
    assert audit.payload["before"][0]["quantity"] == 2
    assert audit.payload["after"][0]["quantity"] == 3
    assert (
        audit.payload["before_total_cents"],
        audit.payload["after_total_cents"],
    ) == (
        1380,
        2070,
    )
    # Wartet noch auf Freigabe: die Kueche hat nichts, also auch kein Bon.
    assert _events(session, order_id) == []
    [card] = list_new(session, tenant_id, TZ, NOW)
    assert card.corrected == "wrong_quantity"


def test_tauschen_behaelt_menge_und_hinweis(session, tenant_id, call_id):
    allergie = "WICHTIG: Keine Erdnüsse. Grund: Allergie"
    items = [
        {
            "menu_item_id": item(session, tenant_id, "23"),
            "quantity": 2,
            "note": allergie,
        }
    ]
    order_id = _order(session, tenant_id, call_id, items)
    [row] = _rows(session, order_id)
    pho = item(session, tenant_id, "13")
    _fix(
        session,
        tenant_id,
        order_id,
        reason="wrong_item",
        rows=(RowEdit(row.id, 2, swap_to=pho),),
    )

    [row] = _rows(session, order_id)
    assert (row.menu_item_id, row.quantity, row.note) == (pho, 2, allergie)
    assert row.unit_price_cents == 1190
    assert _order_row(session, order_id).total_cents == 2380
    [line] = list_new(session, tenant_id, TZ, NOW)[0].lines
    assert (line.number, line.name) == ("13", "Pho Bo")

    # Freigabe danach: die Kueche bekommt den korrigierten Stand als ersten Bon.
    approve_order(session, tenant_id, order_id)
    [event] = _events(session, order_id)
    assert event.payload["revision"] == 1
    assert event.payload["correction_reason"] is None
    assert event.payload["items"][0]["number"] == "13"
    assert event.payload["items"][0]["note"] == allergie


def test_dazunehmen_mit_pflichtauswahl(session, tenant_id, call_id):
    order_id = _order(session, tenant_id, call_id)
    ente = item(session, tenant_id, "47")

    plan = preview_correction(
        session,
        tenant_id,
        order_id,
        _req(session, tenant_id, order_id, added=(AddEdit(ente, 1),)),
        now=NOW,
    )
    # Pflichtgruppe nie mit der Voreinstellung gefuellt (CLAUDE.md §2 Regel 2).
    assert plan.added[0].missing == ["Fleisch"]
    assert any("Fleisch" in b for b in plan.blockers)

    choices = (Choice("Fleisch", "Huhn"), Choice("sauce", "erdnuss"))
    _fix(
        session, tenant_id, order_id, reason="other", added=(AddEdit(ente, 1, choices),)
    )
    rows = _rows(session, order_id)
    assert len(rows) == 2
    assert rows[1].unit_price_cents == 1550  # Grundpreis, Optionen tragen die Differenz
    assert [o["option"] for o in rows[1].options] == ["Huhn", "Erdnuss"]
    # 13,80 + (15,50 - 1,00 + 0,50)
    assert _order_row(session, order_id).total_cents == 2880


def test_kartenpreis_geaendert_behaltene_position_bleibt_eingefroren(
    session, tenant_id, call_id
):
    order_id = _order(session, tenant_id, call_id)
    session.execute(
        update(MenuItem)
        .where(MenuItem.tenant_id == tenant_id, MenuItem.number == "23")
        .values(price_cents=990)
    )
    session.commit()
    [row] = _rows(session, order_id)
    pho = item(session, tenant_id, "13")
    _fix(
        session,
        tenant_id,
        order_id,
        rows=(RowEdit(row.id, 1),),
        added=(AddEdit(pho, 1),),
    )
    # 1 x 6,90 (eingefroren) + 11,90 - nicht 9,90 aus der Karte von jetzt.
    assert _order_row(session, order_id).items_total_cents == 1880


# --- Hinweise ------------------------------------------------------------------------


def test_position_mit_hinweis_faellt_nicht_still_weg(session, tenant_id, call_id):
    allergie = "WICHTIG: Keine Erdnüsse. Grund: Allergie"
    items = [
        {
            "menu_item_id": item(session, tenant_id, "23"),
            "quantity": 1,
            "note": allergie,
        },
        {"menu_item_id": item(session, tenant_id, "13"), "quantity": 1, "note": None},
    ]
    order_id = _order(session, tenant_id, call_id, items)
    noted, _ = _rows(session, order_id)
    ente = item(session, tenant_id, "47")
    with pytest.raises(InvalidInput):
        _fix(
            session,
            tenant_id,
            order_id,
            reason="wrong_item",
            rows=(RowEdit(noted.id, 0),),
            added=(AddEdit(ente, 1, (Choice("Fleisch", "Ente"),)),),
        )
    session.rollback()
    assert len(_rows(session, order_id)) == 2

    _fix(
        session,
        tenant_id,
        order_id,
        reason="other",
        rows=(RowEdit(noted.id, 0, drop_note=True),),
    )
    assert [r.note for r in _rows(session, order_id)] == [None]
    [audit] = _corrections(session, order_id)
    assert audit.payload["note_dropped"] is True
    # audit_log bleibt laenger als die Bestellung: kein Hinweistext, keine Person.
    text = str(audit.payload)
    assert "Erdn" not in text and "Müller" not in text and "+49" not in text


# --- Grenzen ------------------------------------------------------------------------


def test_nichts_geaendert_oder_alles_entfernt_blockiert(session, tenant_id, call_id):
    order_id = _order(session, tenant_id, call_id)
    [row] = _rows(session, order_id)
    assert preview_correction(session, tenant_id, order_id, now=NOW).blockers == [
        NO_CHANGE
    ]
    with pytest.raises(InvalidInput):
        _fix(session, tenant_id, order_id)
    session.rollback()
    with pytest.raises(InvalidInput) as exc:
        _fix(session, tenant_id, order_id, rows=(RowEdit(row.id, 0),))
    assert exc.value.say == NO_POSITION


def test_veralteter_stand_und_doppelter_tap(session, tenant_id, call_id):
    order_id = _order(session, tenant_id, call_id)
    [row] = _rows(session, order_id)
    stale = _req(session, tenant_id, order_id, rows=(RowEdit(row.id, 5),))
    req = _req(session, tenant_id, order_id, rows=(RowEdit(row.id, 3),), edit_id="e1")
    apply_correction(session, tenant_id, order_id, req, "wrong_quantity", now=NOW)
    apply_correction(session, tenant_id, order_id, req, "wrong_quantity", now=NOW)
    assert len(_corrections(session, order_id)) == 1

    with pytest.raises(Conflict):
        apply_correction(session, tenant_id, order_id, stale, "wrong_quantity", now=NOW)
    session.rollback()
    assert _rows(session, order_id)[0].quantity == 3


def test_adresse_nur_bei_lieferung_und_storno_nicht_korrigierbar(
    session, tenant_id, call_id
):
    order_id = _order(session, tenant_id, call_id)
    [row] = _rows(session, order_id)
    with pytest.raises(InvalidInput):
        _fix(
            session,
            tenant_id,
            order_id,
            reason="wrong_address",
            rows=(RowEdit(row.id, 3),),
        )
    session.rollback()
    session.execute(
        update(Order).where(Order.id == order_id).values(status="cancelled")
    )
    session.commit()
    with pytest.raises(Conflict):
        _fix(session, tenant_id, order_id, rows=(RowEdit(row.id, 3),))


def test_fremde_position_und_heute_aus(session, tenant_id, call_id):
    order_id = _order(session, tenant_id, call_id)
    other = _order(session, tenant_id, call_id)
    [foreign] = _rows(session, other)
    with pytest.raises(Conflict):
        _fix(session, tenant_id, order_id, rows=(RowEdit(foreign.id, 3),))
    session.rollback()

    pho = item(session, tenant_id, "13")
    session.execute(
        update(MenuItem)
        .where(MenuItem.id == pho)
        .values(sold_out_until=NOW.replace(hour=23))
    )
    session.commit()
    plan = preview_correction(
        session,
        tenant_id,
        order_id,
        _req(session, tenant_id, order_id, added=(AddEdit(pho, 1),)),
        now=NOW,
    )
    # Das Team darf, sieht aber den Hinweis.
    assert plan.added[0].sold_out is True
    assert plan.blockers == []


def test_nummer_wie_search_menu(session, tenant_id):
    assert find_by_number(session, tenant_id, " 023 ").number == "23"
    assert find_by_number(session, tenant_id, "99") is None
    assert find_by_number(session, tenant_id, "50") is None  # inaktiv
    assert find_by_number(session, tenant_id, "") is None


# --- Bon, wenn die Kueche schon einen hat -------------------------------------------


def test_korrektur_nach_gesendetem_bon_schickt_korrektur(session, tenant_id, call_id):
    order_id = _order(session, tenant_id, call_id, mode="primary")
    [row] = _rows(session, order_id)

    # Erster Bon noch nie versucht: er wird ueberschrieben und bleibt ein erster Bon.
    _fix(session, tenant_id, order_id, rows=(RowEdit(row.id, 3),))
    [event] = _events(session, order_id)
    assert event.payload["revision"] == 1
    assert event.payload["correction_reason"] is None
    assert event.payload["items"][0]["quantity"] == 3

    session.execute(
        update(OutboxEvent)
        .where(OutboxEvent.id == event.id)
        .values(status="sent", attempts=1)
    )
    session.execute(
        update(Order).where(Order.id == order_id).values(handover_state="sent")
    )
    session.commit()
    _fix(session, tenant_id, order_id, rows=(RowEdit(row.id, 4),))
    _first, second = _events(session, order_id)
    assert second.payload["revision"] == 2
    assert second.payload["correction_reason"] == "wrong_quantity"
    assert _order_row(session, order_id).handover_state == "pending"


def test_draft_replay_nach_korrektur(session, tenant_id, call_id):
    _mode(session, tenant_id, "overflow")
    items = [{"menu_item_id": item(session, tenant_id, "23"), "quantity": 2}]
    req = request(session, tenant_id, call_id, items)
    order_id = draft_order(session, req, now=NOW).order_id
    _confirm(session, tenant_id, call_id, order_id)
    pho = item(session, tenant_id, "13")
    _fix(session, tenant_id, order_id, reason="other", added=(AddEdit(pho, 1),))

    # Ein spaeter Replay des Agenten liest zwei Positionen mit zwei Namen.
    replay = draft_order(session, req, now=NOW)
    assert "Pho Bo" in replay.readback
    count = session.scalar(
        select(func.count(OrderItem.id)).where(OrderItem.order_id == order_id)
    )
    assert count == 2


# --- Befunde aus dem Review von PR #140 ----------------------------------------------


def test_korrektur_nach_fehlgeschlagener_uebergabe_ist_erster_bon(
    session, tenant_id, call_id
):
    order_id = _order(session, tenant_id, call_id, mode="primary")
    session.execute(
        update(OutboxEvent)
        .where(OutboxEvent.payload["order_id"].astext == str(order_id))
        .values(status="failed", attempts=4)
    )
    session.execute(
        update(Order).where(Order.id == order_id).values(handover_state="failed")
    )
    session.commit()
    [row] = _rows(session, order_id)
    _fix(session, tenant_id, order_id, rows=(RowEdit(row.id, 3),))

    # Die Kueche hat nie einen Bon bekommen: kein "KORREKTUR" auf dem neuen.
    _old, new = _events(session, order_id)
    assert new.payload["correction_reason"] is None
    assert new.payload["revision"] == 1
    assert _order_row(session, order_id).handover_state == "pending"


def test_stornierte_bestellung_oeffnet_keinen_editor(session, tenant_id, call_id):
    order_id = _order(session, tenant_id, call_id)
    session.execute(
        update(Order).where(Order.id == order_id).values(status="cancelled")
    )
    session.commit()
    with pytest.raises(Conflict):
        preview_correction(session, tenant_id, order_id, now=NOW)


def test_menge_ueber_der_grenze_des_agenten(session, tenant_id, call_id):
    order_id = _order(session, tenant_id, call_id)
    [row] = _rows(session, order_id)
    with pytest.raises(InvalidInput):
        _fix(session, tenant_id, order_id, rows=(RowEdit(row.id, 31),))


def test_kaputte_namensliste_bricht_nicht_ab(session, tenant_id, call_id):
    order_id = _order(session, tenant_id, call_id)
    session.execute(
        update(AuditLog)
        .where(AuditLog.entity_id == order_id, AuditLog.action == "order.draft_created")
        .values(payload={"labels": [], "warnings": []})
    )
    session.commit()
    [row] = _rows(session, order_id)
    # Namen dann aus der Karte von jetzt, statt eines Fehlers 500.
    _fix(session, tenant_id, order_id, rows=(RowEdit(row.id, 3),))
    [card] = list_new(session, tenant_id, TZ, NOW)
    assert card.lines[0].name == "Frühlingsrollen"
