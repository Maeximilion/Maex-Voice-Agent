"""draft_order: Entwurf anlegen, Summe und readback zurückgeben (T-4.5, docs/04).

Die einzige Stelle, an der eine Summe entsteht. Preise und Optionen kommen aus
der Karte, nie aus dem Request (CLAUDE.md §2 Regel 1): der Agent schickt nur
`menu_item_id`, Menge und die Namen der gewählten Optionen. Erst `confirm`
macht den Entwurf gültig.

A delivery (T-6.5) adds the zone of its address, the fee from that zone and
the minimum order value (`domain/delivery/address.py`). Customer and address
are written here, the moment the draft stands; `confirm` makes the address the
customer's default, on the line the call came from only.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import cast
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from api.core.errors import (
    SAY_ON_FAILURE,
    Conflict,
    InvalidInput,
    NotFound,
    ServiceUnavailable,
)
from api.core.time import utcnow
from api.domain.customers.phone import normalize_phone, number_or_none
from api.domain.delivery.address import SAY_WHICH_NAME, keep, place
from api.domain.ordering.labels import ACTION_DRAFT_CREATED, current_labels
from api.domain.ordering.pricing import Line, items_total_cents
from api.domain.ordering.readback import SpokenLine, readback
from api.domain.ordering.validation import (
    require_delivery_open,
    require_minimum,
    require_pickup_open,
    validated_lines,
)
from api.domain.status.service import DELIVERY, PICKUP, service_open_at
from api.domain.status.waits import current_wait
from api.models import Address, AuditLog, Call, Order, OrderItem, ServiceConfig, Tenant
from api.schemas.orders import DraftOrderRequest, OrderDraft, OrderStatus

ACTOR_AGENT = "agent"
# Die zugesagte Zeit liegt nach Schluss der Abholung. Der Agent sagt es dem
# Gast nicht selbst; das Team sieht die Warnung am Entwurf.
WARNING_READY_AFTER_CLOSE = "ready_after_close"


def draft_order(
    session: Session, req: DraftOrderRequest, now: datetime | None = None
) -> OrderDraft:
    now = now or utcnow()
    tenant = session.get(Tenant, req.tenant_id)
    config = session.get(ServiceConfig, req.tenant_id)
    if tenant is None or config is None:
        raise NotFound("Mandant unbekannt")
    zone = ZoneInfo(tenant.timezone)

    # Gleicher Schlüssel → gleiche Antwort, kein zweiter Vorgang (docs/04 §1).
    existing = _by_key(session, req.idempotency_key)
    if existing is not None:
        return _replay(session, existing, req.tenant_id, req.call_id)

    call = session.get(Call, req.call_id)
    if call is None or call.tenant_id != req.tenant_id:
        raise NotFound("Anruf unbekannt", say=SAY_ON_FAILURE)
    # The session of a call does not expire on commit: without this a tap on
    # the tablet in the middle of the call would not count.
    session.refresh(config)

    delivery = req.type == DELIVERY
    name = (req.customer.name or "").strip()
    if not name and not delivery:
        raise InvalidInput("customer.name: darf nicht leer sein", say=SAY_WHICH_NAME)
    phone = normalize_phone(req.customer.phone)

    if delivery:
        require_delivery_open(session, tenant, config, now)
    else:
        require_pickup_open(session, tenant, config, now, req.call_id)
    lines = validated_lines(session, req.tenant_id, req.items, now)
    total = items_total_cents(lines)
    fee = 0
    wait = current_wait(config, PICKUP, now)
    where = customer = address = None
    if delivery:
        # Recognition only on the line the call came from (L12).
        caller_line = number_or_none(call.caller_id)
        where = place(session, req.tenant_id, req.customer, phone, caller_line, now)
        require_minimum(total, where.zone)
        name = where.name
        fee = where.zone.fee_cents
        # The longer of the two: the team raises the wait in a rush, a far
        # zone takes longer whatever the wait.
        wait = max(current_wait(config, DELIVERY, now), where.zone.eta_minutes)
        customer, address = keep(session, req.tenant_id, req.call_id, phone, where)

    order = Order(
        tenant_id=req.tenant_id,
        call_id=req.call_id,
        type=req.type,
        status="draft",
        phone=phone,
        customer_name=name,
        customer_id=customer.id if customer else None,
        address_id=address.id if address else None,
        items_total_cents=total,
        delivery_fee_cents=fee,
        total_cents=total + fee,
        ready_at=_ready_at(now, wait),
        note=(req.note or "").strip() or None,
        idempotency_key=req.idempotency_key,
        # Fester Bezugspunkt für den readback, damit ein Replay dieselbe Zahl sagt.
        created_at=now,
    )
    session.add(order)
    try:
        session.flush()
    except IntegrityError:
        # Zwei gleichzeitige Aufrufe mit demselben Schlüssel: der zweite liest den ersten.
        session.rollback()
        existing = _by_key(session, req.idempotency_key)
        if existing is None:
            raise
        return _replay(session, existing, req.tenant_id, req.call_id)

    for position, line in enumerate(lines):
        session.add(
            OrderItem(
                tenant_id=req.tenant_id,
                order_id=order.id,
                menu_item_id=line.item.id,
                quantity=line.quantity,
                unit_price_cents=line.item.price_cents,
                options=line.options,
                note=line.note,
                # Die Tabelle hat keine Positionsspalte. Der Abstand in
                # Mikrosekunden hält die gesprochene Reihenfolge fest, in der
                # Replay, Tablet und Bon die Positionen lesen.
                created_at=now + timedelta(microseconds=position),
            )
        )
    warnings = _warnings(session, order, zone)
    labels = [[line.item.number, line.item.name] for line in lines]
    name_heard = where is None or where.name_heard
    spoken_address = None if where is None or where.stored else address
    draft = _response(
        order,
        readback(order, _spoken(lines, labels), spoken_address, name_heard),
        warnings,
    )
    session.add(
        AuditLog(
            tenant_id=req.tenant_id,
            actor=ACTOR_AGENT,
            action=ACTION_DRAFT_CREATED,
            entity="order",
            entity_id=order.id,
            payload={
                "call_id": str(req.call_id),
                "type": req.type,
                "positions": len(lines),
                "total_cents": total + fee,
                # Whether the read-back may say them (L12): a replay says the
                # same. The address itself stays out, as every personal word.
                "name_heard": name_heard,
                "address_heard": spoken_address is not None,
                # Stand von Karte und Öffnungszeiten beim Anlegen. Der Replay
                # liest ihn von hier, statt neu zu rechnen, denn beides kann
                # sich seitdem geändert haben (Codex PR #124). Nur Kartendaten:
                # audit_log bleibt länger als die Bestellung, Name, Telefon und
                # Hinweise stehen in orders und order_items und gehen mit ihnen.
                "labels": labels,
                "warnings": warnings,
            },
        )
    )
    session.commit()
    return draft


def _ready_at(now: datetime, wait_minutes: int) -> datetime:
    """Auf die volle Minute aufgerundet: die Zusage wird nie kürzer als die Wartezeit.

    Abgerundet sagte ein Anruf um 18:00:45 bei 20 Minuten Wartezeit "in etwa 19
    Minuten" an (Codex PR #124).
    """
    # In UTC wie jede gespeicherte Zeit (CLAUDE.md §8): Antwort und Replay aus
    # der Datenbank serialisieren sonst verschieden.
    ready = (now + timedelta(minutes=wait_minutes)).astimezone(UTC)
    floored = ready.replace(second=0, microsecond=0)
    return floored if floored == ready else floored + timedelta(minutes=1)


def _by_key(session: Session, key: str) -> Order | None:
    return session.scalar(select(Order).where(Order.idempotency_key == key))


def _replay(
    session: Session, existing: Order, tenant_id: uuid.UUID, call_id: uuid.UUID
) -> OrderDraft:
    # Auch ein anderer Anruf desselben Betriebs bekommt nie die Antwort unter
    # diesem Schluessel: sie traegt Name, Gerichte und Summe (Codex PR #127, P1).
    if existing.tenant_id != tenant_id or existing.call_id != call_id:
        raise Conflict("Idempotenz-Schlüssel gehört zu einem anderen Vorgang")
    snapshot = _snapshot(session, existing.id)
    rows = session.scalars(
        select(OrderItem)
        .where(OrderItem.order_id == existing.id)
        .order_by(OrderItem.created_at)
    ).all()
    spoken = [
        SpokenLine(number, name, row.quantity, row.options, row.note)
        for row, (number, name) in zip(rows, snapshot["labels"], strict=True)
    ]
    address = None
    if snapshot.get("address_heard") and existing.address_id:
        address = session.get(Address, existing.address_id)
    said = readback(existing, spoken, address, snapshot.get("name_heard", True))
    return _response(existing, said, snapshot["warnings"])


def draft_labels(session: Session, order_id: uuid.UUID) -> list[list[str]]:
    """Nummer und Name je Position, wie vorgelesen oder im Tablet korrigiert."""
    return _snapshot(session, order_id)["labels"]


def _snapshot(session: Session, order_id: uuid.UUID) -> dict:
    snapshot = session.scalar(
        select(AuditLog.payload).where(
            AuditLog.entity == "order",
            AuditLog.entity_id == order_id,
            AuditLog.action == ACTION_DRAFT_CREATED,
        )
    )
    if snapshot is None:
        # Entwurf und Audit-Zeile entstehen in derselben Transaktion; fehlt
        # die Zeile, ist der Zustand kaputt und der Anruf geht ans Team.
        raise ServiceUnavailable("Entwurf ohne Audit-Zeile", say=SAY_ON_FAILURE)
    # Hat das Team im Tablet Positionen geaendert (T-4.7), passen die Namen
    # vom Anlegen nicht mehr zu den Zeilen; die Warnungen bleiben die vom Anruf.
    return {**snapshot, "labels": current_labels(session, order_id)}


def _spoken(lines: list[Line], labels: list[list[str]]) -> list[SpokenLine]:
    return [
        SpokenLine(number, name, line.quantity, line.options, line.note)
        for line, (number, name) in zip(lines, labels, strict=True)
    ]


def _warnings(session: Session, order: Order, zone: ZoneInfo) -> list[str]:
    if order.ready_at is not None and not service_open_at(
        session, order.tenant_id, order.ready_at, zone, order.type
    ):
        return [WARNING_READY_AFTER_CLOSE]
    return []


def _response(order: Order, spoken: str, warnings: list[str]) -> OrderDraft:
    return OrderDraft(
        order_id=order.id,
        # The column is text; ck_orders_status keeps it to these values, and
        # OrderDraft checks it again.
        status=cast(OrderStatus, order.status),
        items_total_cents=order.items_total_cents,
        delivery_fee_cents=order.delivery_fee_cents,
        total_cents=order.total_cents,
        ready_at=order.ready_at,
        warnings=warnings,
        readback=spoken,
    )
