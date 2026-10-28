"""Spalte "Neue Bestellungen" der Betriebsansicht (docs/06 §3, T-4.7).

In der Spalte steht eine Bestellung, solange das Team etwas tun muss:
- bestaetigt und noch nicht als in die Kasse eingegeben markiert ("In Kasse
  eingegeben", T-11.6, docs/09 §1 L18), ohne Tagesgrenze: every order the
  guest said yes to is a sale the register has to book. That holds an order
  waiting for "Passt" too; the mark comes after it. From the next business
  day on an unmarked card is red until somebody marks it, oder
- rot: Uebergabe an die Kueche fehlgeschlagen (`handover_state = failed`), auch
  nach "Passt" und ueber 05:00 hinaus - bis sie gelingt (docs/06 §3). Sonst
  hat die Kueche keinen Bon und niemand merkt es (CLAUDE.md §2 Regel 5).

Rote Karten zuerst, danach die aelteste: wer am laengsten wartet, kommt zuerst.

"Passt" (`approve_order`) setzt `approved`. Ausserhalb von `primary` wartet die
Bestellung bis dahin ohne Bon (domain/ordering/confirm.py) - dann ist "Passt"
die Freigabe an die Kueche. The mark (`mark_entered`) comes after "Passt":
the team types the order in from the slip it releases. Korrekturen stehen in
`correction.py`; a correction clears the mark.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import Text, and_, case, cast, func, literal, or_, select
from sqlalchemy.dialects.postgresql import aggregate_order_by
from sqlalchemy.orm import Session

from api.core.errors import Conflict, NotFound
from api.core.time import business_day, business_day_bounds_utc, utcnow
from api.domain.ordering.confirm import ACTION_CONFIRMED, ALREADY_CONFIRMED
from api.domain.ordering.labels import (
    ACTION_CORRECTED,
    current_labels_many,
    labels_for_rows,
)
from api.domain.ordering.ticket import last_reason, revision, send_ticket
from api.domain.status.config import ACTOR_TABLET
from api.models import AuditLog, Order, OrderItem, ServiceConfig

CONFIRMED = "confirmed"
APPROVED = "approved"
FAILED = "failed"
ACTION_APPROVED = "order.approved"
ACTION_RESENT = "order.resent"
ACTION_REGISTER_ENTERED = "order.register_entered"
# The register is typed from the slip that "Passt" releases (docs/02 §2a).
MARKABLE = (APPROVED, "handed_over")
CORRECTED_SINCE = (
    "Die Bestellung wurde inzwischen korrigiert. Bitte die neue Fassung eingeben."
)
# The order's creation is stamped by the application, the audit entry by the
# database: how far the search for the entry reaches back before the creation.
CLOCK_MARGIN = timedelta(hours=1)


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
    # The note of the whole order (D24), such as the guest's allergy.
    note: str | None = None
    # Since when the order waits for "Passt": the guest's yes. None once the
    # kitchen has, or should have had, its ticket.
    waiting_since: datetime | None = None
    # Waiting longer than the restaurant allows
    # (`service_config.order_approval_overdue_minutes`, #252): nobody has
    # approved it, so nobody cooks it.
    overdue: bool = False
    # Marked as typed into the register (T-11.6); a correction clears it.
    register_entered_at: datetime | None = None
    # Not marked, and from an earlier business day: the register may lack it.
    register_late: bool = False
    # How often the team corrected it. The mark names the revision it was typed
    # from, so a tablet showing an older one cannot mark the newer one.
    revision: int = 0


def _window(tz_name: str, now: datetime | None) -> tuple[datetime, datetime]:
    """Betriebstag wie beim Abholcode: gezaehlt wird nach dem Anlagezeitpunkt."""
    return business_day_bounds_utc(business_day(now, tz_name), tz_name)


def _filters(tenant_id: uuid.UUID) -> tuple:
    return (
        Order.tenant_id == tenant_id,
        Order.deleted_at.is_(None),
        or_(
            # Without a day limit: until somebody marks it as typed into the
            # register (L18). This holds an order waiting for "Passt" too, whose
            # only button would otherwise vanish at 05:00 (Codex PR #140).
            _unmarked(),
            # Ohne Tagesgrenze: ein fehlender Bon wird um 05:00 nicht besser.
            and_(
                Order.status.in_((CONFIRMED, APPROVED)),
                Order.handover_state == FAILED,
            ),
        ),
    )


def _unmarked():
    """An order the guest said yes to that nobody has marked as typed in."""
    return and_(
        Order.status.in_(ALREADY_CONFIRMED), Order.register_entered_at.is_(None)
    )


def list_new(
    session: Session,
    tenant_id: uuid.UUID,
    tz_name: str,
    now: datetime | None = None,
) -> list[BoardOrder]:
    start, _ = _window(tz_name, now)
    revisions = _revisions(session, tenant_id)
    failed_first = case((Order.handover_state == FAILED, 0), else_=1)
    orders = session.scalars(
        select(Order)
        .where(*_filters(tenant_id))
        .order_by(failed_first, Order.created_at, Order.id)
    ).all()
    if not orders:
        return []
    ids = [o.id for o in orders]
    waiting = _waiting_since(session, tenant_id)
    overdue = _overdue(session, tenant_id, waiting, now)
    late = {
        o.id for o in orders if o.register_entered_at is None and o.created_at < start
    }
    # Red cards stay first; then the ones nobody has approved in time, then the
    # ones of an earlier day nobody has marked as typed in, then the rest.
    # Stable: oldest first within each.
    orders = sorted(
        orders,
        key=lambda o: (
            0
            if o.handover_state == FAILED
            else 1
            if o.id in overdue
            else 2
            if o.id in late
            else 3
        ),
    )

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
            note=o.note,
            waiting_since=waiting.get(o.id),
            overdue=o.id in overdue,
            register_entered_at=o.register_entered_at,
            register_late=o.id in late,
            revision=revisions.get(o.id, 0),
        )
        for o in orders
    ]


def _waiting_since(session: Session, tenant_id: uuid.UUID) -> dict[uuid.UUID, datetime]:
    """Every order of the tenant that still needs "Passt", and since when it
    waits: the guest's yes, which is the audit entry `confirm` writes.

    Without that entry the order's creation counts: a missing row must not
    leave a card that never turns red (CLAUDE.md §2 rule 5). The column polls
    this every few seconds, so the entry is looked up from the order's own
    creation on (less a margin for two clocks), which the index on
    `audit_log (tenant_id, at)` answers without reading the older log.
    """
    yes = (
        select(func.min(AuditLog.at))
        .where(
            AuditLog.tenant_id == Order.tenant_id,
            AuditLog.entity == "order",
            AuditLog.entity_id == Order.id,
            AuditLog.action == ACTION_CONFIRMED,
            AuditLog.at >= Order.created_at - CLOCK_MARGIN,
        )
        .correlate(Order)
        .scalar_subquery()
    )
    rows = session.execute(
        select(Order.id, func.coalesce(yes, Order.created_at)).where(
            Order.tenant_id == tenant_id,
            Order.deleted_at.is_(None),
            Order.status == CONFIRMED,
            Order.handover_state.is_(None),
        )
    )
    return {row[0]: row[1] for row in rows}


def _overdue(
    session: Session,
    tenant_id: uuid.UUID,
    waiting: dict[uuid.UUID, datetime],
    now: datetime | None,
) -> set[uuid.UUID]:
    """The waiting orders nobody has approved within the restaurant's minutes
    (`service_config.order_approval_overdue_minutes`, never a number here)."""
    if not waiting:
        return set()
    minutes = session.execute(
        select(ServiceConfig.order_approval_overdue_minutes).where(
            ServiceConfig.tenant_id == tenant_id
        )
    ).scalar_one_or_none()
    if minutes is None:
        # A tenant without its configuration has no rule to be late by.
        return set()
    late = (now or utcnow()) - timedelta(minutes=minutes)
    return {order_id for order_id, since in waiting.items() if since <= late}


def _revisions(session: Session, tenant_id: uuid.UUID) -> dict[uuid.UUID, int]:
    """How often each order of the column was corrected: `ticket.revision` for
    all of them in one statement.

    Read before the orders and their lines (READ COMMITTED, api/db.py): a
    correction that commits in between leaves a card with the new lines and
    the old count, whose mark is refused, never the old lines with the new
    count, whose mark would pass (ultrareview #423).
    """
    rows = session.execute(
        select(Order.id, func.count(AuditLog.id))
        .join(AuditLog, AuditLog.entity_id == Order.id)
        .where(
            *_filters(tenant_id),
            AuditLog.tenant_id == tenant_id,
            AuditLog.entity == "order",
            AuditLog.action == ACTION_CORRECTED,
        )
        .group_by(Order.id)
    )
    return {row[0]: row[1] for row in rows}


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
    start, _ = _window(tz_name, now)
    row = func.concat_ws(
        ":",
        cast(Order.id, Text),
        Order.status,
        func.coalesce(Order.handover_state, ""),
        cast(Order.total_cents, Text),
        cast(Order.updated_at, Text),
        func.coalesce(cast(Order.register_entered_at, Text), ""),
    )
    count, digest = session.execute(
        select(
            func.count(Order.id),
            func.md5(
                func.coalesce(
                    func.string_agg(row, aggregate_order_by(literal(","), row)), ""
                )
            ),
        ).where(*_filters(tenant_id))
    ).one()
    # No row changes when an order becomes overdue, only the clock: which
    # orders are overdue is part of the fingerprint, or no tablet would redraw.
    # The same for an unmarked card turning red at the start of a business day.
    waiting = _waiting_since(session, tenant_id)
    overdue = ",".join(
        sorted(str(i) for i in _overdue(session, tenant_id, waiting, now))
    )
    return f"{count}:{digest}:{overdue}:{start.isoformat()}"


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
    send_ticket(session, order, correction_reason=last_reason(session, order))
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


def mark_entered(
    session: Session,
    tenant_id: uuid.UUID,
    order_id: uuid.UUID,
    seen_revision: int,
    actor: str = ACTOR_TABLET,
    now: datetime | None = None,
) -> Order:
    """ "In Kasse eingegeben" (T-11.6, docs/09 §1 L18). Twice tapped or on two
    tablets: one mark.

    `seen_revision` is the revision the tablet showed: the team typed that one.
    A correction in between (`ticket.revision` grew) is refused, so a tablet
    with an old card never marks a state the register does not hold.
    """
    order = _locked(session, tenant_id, order_id)
    if order.register_entered_at is not None:
        session.commit()
        return order
    if order.status not in MARKABLE:
        raise Conflict(f"Bestellung {order.id} ist {order.status}")
    current = revision(session, order)
    if current != seen_revision:
        raise Conflict(
            f"Bestellung {order.id}: Revision {seen_revision} gesehen, {current} aktuell",
            say=CORRECTED_SINCE,
        )
    order.register_entered_at = now or utcnow()
    session.add(
        AuditLog(
            tenant_id=tenant_id,
            actor=actor,
            action=ACTION_REGISTER_ENTERED,
            entity="order",
            entity_id=order.id,
            payload={"call_id": str(order.call_id), "revision": current},
        )
    )
    session.commit()
    return order


def unmarked_before(
    session: Session, tenant_id: uuid.UUID, before: datetime
) -> list[Order]:
    """The orders created before `before` that nobody has marked as typed into
    the register, oldest first: the daily report (api/jobs/daily_report.py)."""
    return list(
        session.scalars(
            select(Order)
            .where(
                Order.tenant_id == tenant_id,
                Order.deleted_at.is_(None),
                Order.created_at < before,
                _unmarked(),
            )
            .order_by(Order.created_at, Order.id)
        )
    )
