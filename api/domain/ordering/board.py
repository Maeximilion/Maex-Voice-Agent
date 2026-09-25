"""Spalte "Neue Bestellungen" der Betriebsansicht (docs/06 §3, T-4.7).

In der Spalte steht eine Bestellung, solange das Team etwas tun muss:
- bestaetigt (`confirmed`) am laufenden Betriebstag und noch nicht mit "Passt"
  abgehakt, oder
- rot: Uebergabe an die Kueche fehlgeschlagen (`handover_state = failed`), auch
  nach "Passt" und ueber 05:00 hinaus - bis sie gelingt (docs/06 §3). Sonst
  hat die Kueche keinen Bon und niemand merkt es (CLAUDE.md §2 Regel 5).

Rote Karten zuerst, danach die aelteste: wer am laengsten wartet, kommt zuerst.

"Passt" (`approve_order`) setzt `approved`. Ausserhalb von `primary` wartet die
Bestellung bis dahin ohne Bon (domain/ordering/confirm.py) - dann ist "Passt"
die Freigabe an die Kueche. Korrekturen stehen in `correction.py`.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Text, and_, case, cast, func, literal, or_, select
from sqlalchemy.dialects.postgresql import aggregate_order_by
from sqlalchemy.orm import Session

from api.core.errors import Conflict, NotFound
from api.core.time import business_day, business_day_bounds_utc
from api.domain.ordering.labels import (
    ACTION_CORRECTED,
    current_labels_many,
    labels_for_rows,
)
from api.domain.ordering.ticket import send_ticket
from api.models import AuditLog, Order, OrderItem

CONFIRMED = "confirmed"
APPROVED = "approved"
FAILED = "failed"
ACTOR_TABLET = "gui:tablet"
ACTION_APPROVED = "order.approved"
ACTION_RESENT = "order.resent"


@dataclass(frozen=True)
class BoardLine:
    number: str
    name: str
    quantity: int
    # Gewaehlte Optionen in Worten ("Erdnuss"), die Gruppe steht nicht auf dem Bon.
    options: list[str]
    note: str | None


@dataclass(frozen=True)
class BoardOrder:
    order_id: uuid.UUID
    created_at: datetime
    status: str
    type: str
    pickup_code: str | None
    customer_name: str
    phone: str
    total_cents: int
    ready_at: datetime | None
    # None: wartet auf Freigabe im Tablet · pending/sent: Kueche hat oder
    # bekommt den Bon · failed: Kueche nicht erreicht.
    handover_state: str | None
    # Grund der juengsten Korrektur im Tablet, sonst None.
    corrected: str | None
    lines: list[BoardLine]


def _window(tz_name: str, now: datetime | None) -> tuple[datetime, datetime]:
    """Betriebstag wie beim Abholcode: gezaehlt wird nach dem Anlagezeitpunkt."""
    return business_day_bounds_utc(business_day(now, tz_name), tz_name)


def _filters(tenant_id: uuid.UUID, start: datetime, end: datetime) -> tuple:
    return (
        Order.tenant_id == tenant_id,
        Order.deleted_at.is_(None),
        or_(
            and_(
                Order.status == CONFIRMED,
                Order.created_at >= start,
                Order.created_at < end,
            ),
            # Ohne Tagesgrenze: ein fehlender Bon wird um 05:00 nicht besser.
            and_(
                Order.status.in_((CONFIRMED, APPROVED)),
                Order.handover_state == FAILED,
            ),
        ),
    )


def list_new(
    session: Session,
    tenant_id: uuid.UUID,
    tz_name: str,
    now: datetime | None = None,
) -> list[BoardOrder]:
    start, end = _window(tz_name, now)
    failed_first = case((Order.handover_state == FAILED, 0), else_=1)
    orders = session.scalars(
        select(Order)
        .where(*_filters(tenant_id, start, end))
        .order_by(failed_first, Order.created_at, Order.id)
    ).all()
    if not orders:
        return []
    ids = [o.id for o in orders]

    items: dict[uuid.UUID, list[OrderItem]] = {oid: [] for oid in ids}
    for row in session.scalars(
        select(OrderItem)
        .where(OrderItem.tenant_id == tenant_id, OrderItem.order_id.in_(ids))
        .order_by(OrderItem.order_id, OrderItem.created_at, OrderItem.id)
    ):
        items[row.order_id].append(row)

    labels = current_labels_many(session, ids)
    corrected = _last_correction_reasons(session, tenant_id, ids)
    for oid in ids:
        labels[oid] = labels_for_rows(session, oid, items[oid], labels.get(oid))
    return [
        BoardOrder(
            order_id=o.id,
            created_at=o.created_at,
            status=o.status,
            type=o.type,
            pickup_code=o.pickup_code,
            customer_name=o.customer_name,
            phone=o.phone,
            total_cents=o.total_cents,
            ready_at=o.ready_at,
            handover_state=o.handover_state,
            corrected=corrected.get(o.id),
            lines=[
                BoardLine(
                    number=number,
                    name=name,
                    quantity=row.quantity,
                    options=[opt["option"] for opt in row.options],
                    note=row.note,
                )
                for row, (number, name) in zip(items[o.id], labels[o.id], strict=True)
            ],
        )
        for o in orders
    ]


def _last_correction_reasons(
    session: Session, tenant_id: uuid.UUID, order_ids: list[uuid.UUID]
) -> dict[uuid.UUID, str]:
    reasons: dict[uuid.UUID, str] = {}
    for entity_id, payload in session.execute(
        select(AuditLog.entity_id, AuditLog.payload)
        .where(
            AuditLog.tenant_id == tenant_id,
            AuditLog.entity == "order",
            AuditLog.entity_id.in_(order_ids),
            AuditLog.action == ACTION_CORRECTED,
        )
        .order_by(AuditLog.id)
    ):
        reasons[entity_id] = payload["reason"]
    return reasons


def new_orders_change_token(
    session: Session,
    tenant_id: uuid.UUID,
    tz_name: str,
    now: datetime | None = None,
) -> str:
    """Fingerabdruck der Spalte fuer den Ereignisstrom (gui/sse.py).

    Je Bestellung id, Status, Uebergabe-Zustand, Summe und updated_at. Die
    ersten vier sieht der Strom auch nach einem rohen UPDATE in der Datenbank
    (Befund Codex PR #111); updated_at faengt eine Korrektur, die die Summe
    gleich laesst - `apply_correction` setzt es dafuer ausdruecklich.
    """
    start, end = _window(tz_name, now)
    row = func.concat_ws(
        ":",
        cast(Order.id, Text),
        Order.status,
        func.coalesce(Order.handover_state, ""),
        cast(Order.total_cents, Text),
        cast(Order.updated_at, Text),
    )
    count, digest = session.execute(
        select(
            func.count(Order.id),
            func.md5(
                func.coalesce(
                    func.string_agg(row, aggregate_order_by(literal(","), row)), ""
                )
            ),
        ).where(*_filters(tenant_id, start, end))
    ).one()
    return f"{count}:{digest}"


def _locked(session: Session, tenant_id: uuid.UUID, order_id: uuid.UUID) -> Order:
    order = session.execute(
        select(Order)
        .where(
            Order.id == order_id,
            Order.tenant_id == tenant_id,
            Order.deleted_at.is_(None),
        )
        .with_for_update(),
        execution_options={"populate_existing": True},
    ).scalar_one_or_none()
    if order is None:
        raise NotFound(f"Bestellung {order_id} nicht gefunden")
    return order


def approve_order(
    session: Session,
    tenant_id: uuid.UUID,
    order_id: uuid.UUID,
    actor: str = ACTOR_TABLET,
) -> Order:
    """ "Passt". Zweimal getippt oder an zwei Tablets: ein Eintrag, ein Bon.

    Wartet die Bestellung noch auf die Freigabe (kein handover_state), geht sie
    jetzt an die Kueche. Hat die Kueche den Bon schon (primary), bleibt es beim
    Abhaken. Eine rote Karte hakt "Passt" nicht ab: sie braucht "Nochmal senden".
    """
    order = _locked(session, tenant_id, order_id)
    if order.status == APPROVED:
        session.commit()
        return order
    if order.status != CONFIRMED:
        raise Conflict(f"Bestellung {order.id} ist {order.status}")
    if order.handover_state == FAILED:
        raise Conflict(f"Bestellung {order.id}: Kueche nicht erreicht")

    released = order.handover_state is None
    order.status = APPROVED
    if released:
        send_ticket(session, order)
        order.handover_state = "pending"
    session.add(
        AuditLog(
            tenant_id=tenant_id,
            actor=actor,
            action=ACTION_APPROVED,
            entity="order",
            entity_id=order.id,
            payload={"call_id": str(order.call_id), "released": released},
        )
    )
    session.commit()
    return order


def resend_order(
    session: Session,
    tenant_id: uuid.UUID,
    order_id: uuid.UUID,
    actor: str = ACTOR_TABLET,
) -> Order:
    """ "Nochmal senden" auf einer roten Karte (docs/06 §3). Nur bei `failed`."""
    order = _locked(session, tenant_id, order_id)
    if order.handover_state != FAILED or order.status not in (CONFIRMED, APPROVED):
        # Anderes Tablet war schneller oder die Seite ist alt: nichts zu tun.
        session.commit()
        return order
    send_ticket(session, order)
    order.handover_state = "pending"
    session.add(
        AuditLog(
            tenant_id=tenant_id,
            actor=actor,
            action=ACTION_RESENT,
            entity="order",
            entity_id=order.id,
            payload={"call_id": str(order.call_id)},
        )
    )
    session.commit()
    return order
