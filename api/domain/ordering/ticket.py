"""Bon fuer die Kueche in die Outbox legen, ohne dass ein alter Stand einen neuen ueberholt.

Drei Wege schicken eine Bestellung an die Kueche: `confirm` im Primaerbetrieb,
"Passt" im Tablet (Freigabe, T-4.7) und eine Korrektur, nachdem die Kueche den
Bon schon hat. Der Dispatcher stellt nach `next_attempt_at` zu, nicht nach
Bestellung: haengt der erste Bon im Backoff und die Korrektur ist sofort
faellig, kaeme der alte Stand nach dem neuen an und die Kueche kocht das
falsche Gericht (Design-Review T-4.7).

Deshalb:
- Ein Bon, den noch niemand zu senden versucht hat (`pending`, `attempts = 0`),
  wird ueberschrieben statt ergaenzt. Dieselbe Ereignis-id heisst ein Bon, und
  der traegt den neuesten Stand.
- Sonst entsteht ein neues Ereignis. Jeder Bon traegt `revision` (Anzahl der
  Korrekturen); die Kueche verwirft eine Revision, die kleiner ist als eine schon
  gedruckte (Vertrag fuer T-4.6, docs/04 §confirm). Einen Bon mit Versuchen
  anzufassen waere falsch: n8n kann ihn schon haben und entdoppelt ueber die id.
"""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from api.domain.ordering.confirm import order_event_payload
from api.domain.ordering.labels import ACTION_CORRECTED
from api.events import enqueue
from api.events.types import ORDER_CONFIRMED
from api.models import AuditLog, Order, OutboxEvent


def revision(session: Session, order: Order) -> int:
    """Wie oft das Team die Positionen korrigiert hat."""
    return session.scalar(
        select(func.count(AuditLog.id)).where(
            AuditLog.tenant_id == order.tenant_id,
            AuditLog.entity == "order",
            AuditLog.entity_id == order.id,
            AuditLog.action == ACTION_CORRECTED,
        )
    )


def send_ticket(
    session: Session, order: Order, correction_reason: str | None = None
) -> OutboxEvent:
    """Aktuellen Stand der Bestellung an die Kueche. Committet nicht.

    `correction_reason` nur, wenn die Kueche schon einen Bon hatte: dann
    druckt sie "KORREKTUR". Eine Bestellung, die das Team vor der Freigabe
    korrigiert, ist fuer die Kueche ein erster Bon.
    """
    # SKIP LOCKED: eine Zeile, die der Dispatcher gerade sendet, ist unterwegs
    # und darf nicht mehr umgeschrieben werden - dann eben ein neues Ereignis.
    unsent = session.scalars(
        select(OutboxEvent)
        .where(
            OutboxEvent.tenant_id == order.tenant_id,
            OutboxEvent.event_type == ORDER_CONFIRMED,
            OutboxEvent.status == "pending",
            OutboxEvent.attempts == 0,
            OutboxEvent.payload["order_id"].astext == str(order.id),
        )
        .order_by(OutboxEvent.created_at)
        .with_for_update(skip_locked=True)
    ).all()
    current = revision(session, order)
    if unsent:
        event = unsent[-1]
        # Die Kueche hat diesen Bon nie gesehen: war er ein erster Bon, bleibt
        # er einer, auch wenn inzwischen korrigiert wurde.
        reason = correction_reason if event.payload.get("correction_reason") else None
        event.payload = order_event_payload(session, order, current, reason)
        event.next_attempt_at = func.now()
        return event
    return enqueue(
        session,
        tenant_id=order.tenant_id,
        event_type=ORDER_CONFIRMED,
        payload=order_event_payload(session, order, current, correction_reason),
    )
