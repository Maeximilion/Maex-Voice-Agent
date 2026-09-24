"""confirm fuer Bestellungen: draft -> confirmed, Abholcode, Uebergabe je Modus (docs/04 §confirm).

Wie bei Reservierungen traegt der Zustand die Idempotenz, nicht der Schluessel:
ein zweiter Aufruf liest `confirmed` und antwortet gleich, ohne zweites
Ereignis.

Wohin die Bestellung danach geht, haengt am Modus (docs/02 §Modus): nur im
Primaerbetrieb geht sie sofort an die Kueche (`order.confirmed` in die Outbox,
`handover_state` pending). In jedem anderen Modus braucht sie die Freigabe im
Tablet (T-4.7); bis dahin entsteht kein Ereignis, und die Antwort sagt
`awaiting_approval`. Die Entscheidung haengt an der Zeile, nicht am Modus
von jetzt: ein spaeterer Moduswechsel aendert die Antwort auf einen Replay nicht.
"""

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from api.core.errors import Conflict, NotFound
from api.core.time import business_day, business_day_bounds_utc
from api.domain.ordering.draft import draft_labels
from api.events import enqueue
from api.events.types import ORDER_CONFIRMED
from api.models import AuditLog, Order, OrderItem, ServiceConfig, Tenant
from api.schemas.confirm import Confirmation, ConfirmRequest

ACTOR_AGENT = "agent"
ACTION_CONFIRMED = "order.confirmed"
# Nur hier geht eine KI-Bestellung ohne Freigabe an die Kueche (docs/02).
DIRECT_MODE = "primary"
# Buchstabe vor der Zahl: am Tresen nicht mit einer Kartennummer zu verwechseln
# ("A17" ist der Abholcode, "Nummer 17" ein Gericht). Format wie im Mockup.
PICKUP_PREFIX = "A"

SAY_STOERUNG = "Bei mir gibt es gerade eine technische Störung. Ich verbinde Sie mit dem Restaurant."
SAY_CANCELLED = (
    "Diese Bestellung wurde bereits storniert. Ich verbinde Sie mit dem Restaurant."
)
ALREADY_CONFIRMED = ("confirmed", "approved", "handed_over")


def confirm_order(session: Session, req: ConfirmRequest) -> Confirmation:
    # Zeilensperre bis zum Ende der Transaktion: zwei gleichzeitige Aufrufe auf
    # dieselbe Bestellung vergeben keinen zweiten Code und kein zweites Ereignis.
    order = session.execute(
        select(Order).where(Order.id == req.entity_id).with_for_update(),
        execution_options={"populate_existing": True},
    ).scalar_one_or_none()

    # Derselbe Anruf wie beim Entwurf: das Ja gehoert dem Gast in der Leitung
    # (CLAUDE.md §2 Regel 3).
    if (
        order is None
        or order.tenant_id != req.tenant_id
        or order.call_id != req.call_id
        or order.deleted_at is not None
    ):
        raise NotFound("Bestellung unbekannt", say=SAY_STOERUNG)

    if order.status == "cancelled":
        raise Conflict("Bestellung ist storniert", say=SAY_CANCELLED)

    if order.status in ALREADY_CONFIRMED:
        # Nichts zu schreiben; commit gibt nur die Sperre frei.
        session.commit()
        return _answer(order)

    # Gemeinsame Sperre auf die Konfiguration: ein laufender Not-Aus (pause_ai
    # sperrt die Zeile FOR UPDATE) wird abgewartet, danach gilt sein Modus.
    # Ein einfacher Lesezugriff saehe noch primary und schickte an die Kueche,
    # obwohl die KI im selben Moment angehalten wird (Codex PR #125, P1).
    # FOR SHARE statt FOR UPDATE: gleichzeitige Bestaetigungen warten nicht
    # aufeinander, nur auf Aenderungen am Modus.
    config = session.execute(
        select(ServiceConfig)
        .where(ServiceConfig.tenant_id == req.tenant_id)
        .with_for_update(read=True),
        execution_options={"populate_existing": True},
    ).scalar_one_or_none()
    tenant = session.get(Tenant, req.tenant_id)
    if config is None or tenant is None:
        raise NotFound("Mandant ohne service_config", say=SAY_STOERUNG)

    direct = config.call_mode == DIRECT_MODE
    order.status = "confirmed"
    order.pickup_code = _next_pickup_code(session, order, tenant.timezone)
    order.handover_state = "pending" if direct else None

    session.add(
        AuditLog(
            tenant_id=req.tenant_id,
            actor=ACTOR_AGENT,
            action=ACTION_CONFIRMED,
            entity="order",
            entity_id=order.id,
            payload={
                "call_id": str(req.call_id),
                "idempotency_key": req.idempotency_key,
                "pickup_code": order.pickup_code,
                "call_mode": config.call_mode,
            },
        )
    )
    if direct:
        enqueue(
            session,
            tenant_id=req.tenant_id,
            event_type=ORDER_CONFIRMED,
            payload=_event_payload(session, order),
        )
    session.commit()
    return _answer(order)


def _answer(order: Order) -> Confirmation:
    return Confirmation(
        status="confirmed",
        handover="queued" if order.handover_state is not None else "awaiting_approval",
        pickup_code=order.pickup_code,
    )


def _next_pickup_code(session: Session, order: Order, tz_name: str) -> str:
    """Laufende Nummer je Mandant und Betriebstag: A1, A2, ...

    Der Tag kommt aus dem Anlagezeitpunkt der Bestellung, nicht aus der Uhr:
    so zaehlen Vergabe und Zaehlung ueber dasselbe Fenster, auch wenn ein
    Entwurf kurz vor 05:00 angelegt und danach bestaetigt wird. Die Sperre je
    Mandant und Tag laesst zwei gleichzeitige Bestaetigungen nacheinander
    zaehlen, sonst bekaemen beide denselben Code.
    """
    day = business_day(order.created_at, tz_name)
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:key)::bigint)"),
        {"key": f"pickup_code:{order.tenant_id}:{day.isoformat()}"},
    )
    start, end = business_day_bounds_utc(day, tz_name)
    issued = session.scalar(
        select(func.count())
        .select_from(Order)
        .where(
            Order.tenant_id == order.tenant_id,
            Order.pickup_code.is_not(None),
            Order.created_at >= start,
            Order.created_at < end,
        )
    )
    return f"{PICKUP_PREFIX}{issued + 1}"


def _event_payload(session: Session, order: Order) -> dict:
    """Vollstaendig, damit der kalte Pfad den Bon ohne zweite Abfrage druckt.

    Nummer und Name kommen aus dem Schnappschuss des Entwurfs, nicht aus der
    Karte von jetzt: auf dem Bon steht, was dem Gast vorgelesen wurde.
    """
    rows = session.scalars(
        select(OrderItem)
        .where(OrderItem.order_id == order.id)
        .order_by(OrderItem.created_at)
    ).all()
    labels = draft_labels(session, order.id)
    return {
        "order_id": str(order.id),
        "call_id": str(order.call_id),
        "type": order.type,
        "pickup_code": order.pickup_code,
        "customer_name": order.customer_name,
        "phone": order.phone,
        "ready_at": order.ready_at.isoformat() if order.ready_at else None,
        "items_total_cents": order.items_total_cents,
        "delivery_fee_cents": order.delivery_fee_cents,
        "total_cents": order.total_cents,
        "items": [
            {
                "number": number,
                "name": name,
                "quantity": row.quantity,
                "unit_price_cents": row.unit_price_cents,
                "options": row.options,
                "note": row.note,
            }
            for row, (number, name) in zip(rows, labels, strict=True)
        ],
    }
