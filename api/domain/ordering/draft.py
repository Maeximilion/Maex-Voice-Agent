"""draft_order: Entwurf anlegen, Summe und readback zurückgeben (T-4.5, docs/04).

Die einzige Stelle, an der eine Summe entsteht. Preise und Optionen kommen aus
der Karte, nie aus dem Request (CLAUDE.md §2 Regel 1): der Agent schickt nur
`menu_item_id`, Menge und die Namen der gewählten Optionen. Erst `confirm`
macht den Entwurf gültig.

Lieferung kommt mit T-6.5 (Zone, Pauschale, Mindestbestellwert); bis dahin
nimmt das Tool nur Abholungen an.
"""

import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from api.core.errors import Conflict, InvalidInput, NotFound
from api.core.time import utcnow
from api.domain.customers.phone import normalize_phone
from api.domain.ordering.pricing import Line, items_total_cents
from api.domain.ordering.readback import readback
from api.domain.ordering.validation import (
    pickup_open_at,
    require_pickup_open,
    validated_lines,
)
from api.domain.status.service import PICKUP
from api.models import AuditLog, Call, MenuItem, Order, OrderItem, ServiceConfig, Tenant
from api.schemas.orders import DraftOrderRequest, OrderDraft

ACTOR_AGENT = "agent"
ACTION_DRAFT_CREATED = "order.draft_created"
# Die zugesagte Zeit liegt nach Schluss der Abholung. Der Agent sagt es dem
# Gast nicht selbst; das Team sieht die Warnung am Entwurf.
WARNING_READY_AFTER_CLOSE = "ready_after_close"

SAY_CALL_UNKNOWN = "Bei mir gibt es gerade eine technische Störung. Ich verbinde Sie mit dem Restaurant."
SAY_NO_DELIVERY = (
    "Lieferungen kann ich gerade noch nicht aufnehmen. Möchten Sie abholen?"
)


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
        return _replay(session, existing, req.tenant_id, zone)

    call = session.get(Call, req.call_id)
    if call is None or call.tenant_id != req.tenant_id:
        raise NotFound("Anruf unbekannt", say=SAY_CALL_UNKNOWN)
    if req.type != PICKUP:
        raise InvalidInput("type: Lieferung erst ab T-6.5", say=SAY_NO_DELIVERY)

    name = req.customer.name.strip()
    if not name:
        raise InvalidInput("customer.name: darf nicht leer sein")
    phone = normalize_phone(req.customer.phone)

    require_pickup_open(session, req.tenant_id, now, zone)
    lines = validated_lines(session, req.tenant_id, req.items, now)
    total = items_total_cents(lines)
    # Minutengenau: vorgelesen wird "in etwa 20 Minuten", nicht Sekunden.
    ready_at = now.replace(second=0, microsecond=0) + timedelta(
        minutes=config.pickup_wait_minutes
    )

    order = Order(
        tenant_id=req.tenant_id,
        call_id=req.call_id,
        type=req.type,
        status="draft",
        phone=phone,
        customer_name=name,
        items_total_cents=total,
        delivery_fee_cents=0,
        total_cents=total,
        ready_at=ready_at,
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
        return _replay(session, existing, req.tenant_id, zone)

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
                # Mikrosekunden hält die gesprochene Reihenfolge fest, damit der
                # Replay die Positionen in derselben Folge vorliest.
                created_at=now + timedelta(microseconds=position),
            )
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
                "total_cents": total,
            },
        )
    )
    session.commit()
    return _draft(session, order, lines, zone)


def _by_key(session: Session, key: str) -> Order | None:
    return session.scalar(select(Order).where(Order.idempotency_key == key))


def _replay(
    session: Session, existing: Order, tenant_id: uuid.UUID, zone: ZoneInfo
) -> OrderDraft:
    if existing.tenant_id != tenant_id:
        raise Conflict("Idempotenz-Schlüssel gehört zu einem anderen Vorgang")
    rows = session.execute(
        select(OrderItem, MenuItem)
        .join(MenuItem, MenuItem.id == OrderItem.menu_item_id)
        .where(OrderItem.order_id == existing.id)
        .order_by(OrderItem.created_at)
    ).all()
    lines = [
        Line(item=menu, quantity=row.quantity, options=row.options, note=row.note)
        for row, menu in rows
    ]
    return _draft(session, existing, lines, zone)


def _draft(
    session: Session, order: Order, lines: list[Line], zone: ZoneInfo
) -> OrderDraft:
    warnings = []
    # Aus den gespeicherten Werten, nicht aus der Uhr: ein Replay warnt gleich.
    if order.ready_at is not None and not pickup_open_at(
        session, order.tenant_id, order.ready_at, zone
    ):
        warnings.append(WARNING_READY_AFTER_CLOSE)
    return OrderDraft(
        order_id=order.id,
        status=order.status,
        items_total_cents=order.items_total_cents,
        delivery_fee_cents=order.delivery_fee_cents,
        total_cents=order.total_cents,
        ready_at=order.ready_at,
        warnings=warnings,
        readback=readback(order, lines),
    )
