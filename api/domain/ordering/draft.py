"""draft_order: Entwurf anlegen, Summe und readback zurückgeben (T-4.5, docs/04).

Die einzige Stelle, an der eine Summe entsteht. Preise und Optionen kommen aus
der Karte, nie aus dem Request (CLAUDE.md §2 Regel 1): der Agent schickt nur
`menu_item_id`, Menge und die Namen der gewählten Optionen. Erst `confirm`
macht den Entwurf gültig.

Lieferung kommt mit T-6.5 (Zone, Pauschale, Mindestbestellwert); bis dahin
nimmt das Tool nur Abholungen an.
"""

import uuid
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from api.core.errors import Conflict, InvalidInput, NotFound, ServiceUnavailable
from api.core.time import utcnow
from api.domain.customers.phone import normalize_phone
from api.domain.ordering.pricing import Line, items_total_cents
from api.domain.ordering.readback import SpokenLine, readback
from api.domain.ordering.validation import (
    pickup_open_at,
    require_pickup_open,
    validated_lines,
)
from api.domain.status.service import PICKUP
from api.models import AuditLog, Call, Order, OrderItem, ServiceConfig, Tenant
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
        return _replay(session, existing, req.tenant_id)

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
    ready_at = _ready_at(now, config.pickup_wait_minutes)

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
        return _replay(session, existing, req.tenant_id)

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
    draft = _response(order, readback(order, _spoken(lines, labels)), warnings)
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


def _replay(session: Session, existing: Order, tenant_id: uuid.UUID) -> OrderDraft:
    if existing.tenant_id != tenant_id:
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
    return _response(existing, readback(existing, spoken), snapshot["warnings"])


def draft_labels(session: Session, order_id: uuid.UUID) -> list[list[str]]:
    """Nummer und Name je Position, wie sie beim Anlegen vorgelesen wurden."""
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
        raise ServiceUnavailable("Entwurf ohne Audit-Zeile", say=SAY_CALL_UNKNOWN)
    return snapshot


def _spoken(lines: list[Line], labels: list[list[str]]) -> list[SpokenLine]:
    return [
        SpokenLine(number, name, line.quantity, line.options, line.note)
        for line, (number, name) in zip(lines, labels, strict=True)
    ]


def _warnings(session: Session, order: Order, zone: ZoneInfo) -> list[str]:
    if order.ready_at is not None and not pickup_open_at(
        session, order.tenant_id, order.ready_at, zone
    ):
        return [WARNING_READY_AFTER_CLOSE]
    return []


def _response(order: Order, spoken: str, warnings: list[str]) -> OrderDraft:
    return OrderDraft(
        order_id=order.id,
        status=order.status,
        items_total_cents=order.items_total_cents,
        delivery_fee_cents=order.delivery_fee_cents,
        total_cents=order.total_cents,
        ready_at=order.ready_at,
        warnings=warnings,
        readback=spoken,
    )
