"""Kuechenbon ueber die Druckbruecke: abholen, Rueckmeldung, Waechter (T-4.6, docs/04 §Kuechenbon).

Der Server steht im Rechenzentrum, der Bondrucker im Lokal. Der Server erreicht
den Drucker nicht, die Druckbruecke im Lokal den Server schon: sie holt faellige
Bons ab (`claim_tickets`), druckt und meldet zurueck (`ack_ticket`). Kein Loch in
der Firewall des Restaurants, und faellt die Bruecke aus, warten die Bons in der
Outbox, statt verloren zu gehen.

Regeln:
- Abholen zaehlt als Versuch und leiht den Bon fuer `LEASE_SECONDS` aus. Kommt
  keine Rueckmeldung, ist er danach wieder faellig. Die Bruecke erkennt einen
  schon gedruckten Bon und druckt ihn nicht zweimal.
- Ein Bon, zu dem es schon eine hoehere Revision gibt, wird nicht mehr
  ausgeliefert: die Kueche soll nie einen ueberholten Stand kochen (docs/04
  §confirm, Vertrag b). Er gilt als erledigt, `last_error` sagt warum.
- `handover_state` folgt nur dem Bon mit der neuesten Revision (Vertrag c).
- Rot wird die Karte, sobald die Kueche den Bon nicht hat: beim ersten
  Druckfehler oder wenn ein faelliger Bon `PICKUP_TIMEOUT_SECONDS` lang nicht
  abgeholt wurde (Bruecke aus, Netz weg). Das Team sieht es sofort, nicht erst
  nach dem letzten Backoff. Der Bon bleibt faellig: kommt die Bruecke zurueck,
  druckt sie ihn, und die Karte wird wieder normal (CLAUDE.md §2 Regel 5).
- Beim Wechsel auf rot: Alarm im Log und `order.handover_failed` fuer n8n.
"""

import logging
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Integer, cast, select
from sqlalchemy.orm import Session

from api.core.errors import NotFound
from api.core.logging import get_logger, log
from api.events import enqueue
from api.events.outbox import MAX_ATTEMPTS, mark_attempt_failed, mark_sent
from api.events.types import KITCHEN, ORDER_HANDOVER_FAILED
from api.models import Order, OutboxEvent

logger = get_logger("api.domain.ordering.handover")

# So lange darf die Bruecke fuer Druck und Rueckmeldung brauchen.
LEASE_SECONDS = 60
# So lange darf ein faelliger Bon unabgeholt liegen, bevor die Karte rot wird.
PICKUP_TIMEOUT_SECONDS = 60
CLAIM_LIMIT_MAX = 10

PENDING = "pending"
SENT = "sent"
FAILED = "failed"


def _revision(event: OutboxEvent) -> int:
    return int(event.payload.get("revision") or 0)


def _order_id(event: OutboxEvent) -> uuid.UUID:
    return uuid.UUID(event.payload["order_id"])


def _newest_revision(session: Session, event: OutboxEvent) -> int:
    """Hoechste Revision unter allen Bons dieser Bestellung."""
    return session.scalar(
        select(cast(OutboxEvent.payload["revision"].astext, Integer))
        .where(
            OutboxEvent.tenant_id == event.tenant_id,
            OutboxEvent.event_type.in_(KITCHEN),
            OutboxEvent.payload["order_id"].astext == event.payload["order_id"],
        )
        .order_by(
            cast(OutboxEvent.payload["revision"].astext, Integer).desc().nulls_last()
        )
        .limit(1)
    )


def _is_current(session: Session, event: OutboxEvent) -> bool:
    return _revision(event) >= (_newest_revision(session, event) or 0)


def _order(session: Session, event: OutboxEvent) -> Order | None:
    return session.scalar(
        select(Order)
        .where(Order.id == _order_id(event), Order.tenant_id == event.tenant_id)
        .with_for_update()
    )


def _to_red(session: Session, event: OutboxEvent, reason: str) -> None:
    """Karte rot, wenn dieser Bon der aktuelle ist und die Kueche ihn noch nicht hat."""
    if not _is_current(session, event):
        return
    order = _order(session, event)
    # Nur aus `pending`: `sent` hat ein anderer Bon derselben Revision schon
    # geschafft ("Nochmal senden"), `failed` ist schon gemeldet.
    if order is None or order.handover_state != PENDING:
        return
    order.handover_state = FAILED
    # Alarm: die Kueche hat den Bon nicht, ein Mensch muss ran (docs/02 §Ausfall).
    log(
        logger,
        logging.ERROR,
        "Alarm: Kuechenbon nicht angekommen",
        event_id=str(event.id),
        order_id=str(order.id),
        revision=_revision(event),
        reason=reason,
    )
    enqueue(
        session,
        tenant_id=event.tenant_id,
        event_type=ORDER_HANDOVER_FAILED,
        payload={
            "order_id": str(order.id),
            "call_id": str(order.call_id),
            "pickup_code": order.pickup_code,
            "revision": _revision(event),
            "reason": reason,
        },
    )


def _to_sent(session: Session, event: OutboxEvent) -> None:
    if not _is_current(session, event):
        return
    order = _order(session, event)
    if order is not None and order.handover_state in (PENDING, FAILED):
        order.handover_state = SENT


def claim_tickets(
    session: Session, tenant_id: uuid.UUID, now: datetime, limit: int = 5
) -> list[dict[str, Any]]:
    """Faellige Bons fuer die Druckbruecke, aelteste zuerst. Committet.

    SKIP LOCKED: zwei Bruecken oder ein Bon, den das Tablet gerade neu schreibt,
    blockieren sich nicht, und kein Bon geht zweimal gleichzeitig raus.
    """
    limit = max(1, min(limit, CLAIM_LIMIT_MAX))
    rows = session.scalars(
        select(OutboxEvent)
        .where(
            OutboxEvent.tenant_id == tenant_id,
            OutboxEvent.event_type.in_(KITCHEN),
            OutboxEvent.status == PENDING,
            OutboxEvent.next_attempt_at <= now,
            # Ein Bon ohne Versuche mehr raeumt der Waechter ab.
            OutboxEvent.attempts < MAX_ATTEMPTS,
        )
        .order_by(OutboxEvent.created_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    ).all()
    tickets = []
    for event in rows:
        newest = _newest_revision(session, event) or 0
        if _revision(event) < newest:
            mark_sent(event, now)
            event.last_error = f"nicht gedruckt: ueberholt von Revision {newest}"
            continue
        event.attempts += 1
        event.next_attempt_at = now + timedelta(seconds=LEASE_SECONDS)
        tickets.append(
            {"id": str(event.id), "attempt": event.attempts, "ticket": event.payload}
        )
    session.commit()
    return tickets


def ack_ticket(
    session: Session,
    tenant_id: uuid.UUID,
    event_id: uuid.UUID,
    ok: bool,
    now: datetime,
    error: str | None = None,
) -> str:
    """Rueckmeldung der Bruecke. Liefert den Stand des Bons. Committet.

    Wiederholte Rueckmeldung (Netz weg nach dem Druck) aendert nichts mehr.
    """
    event = session.scalar(
        select(OutboxEvent)
        .where(
            OutboxEvent.id == event_id,
            OutboxEvent.tenant_id == tenant_id,
            OutboxEvent.event_type.in_(KITCHEN),
        )
        .with_for_update()
    )
    if event is None:
        raise NotFound("ticket_not_found")
    if event.status != PENDING:
        session.commit()
        return event.status
    if ok:
        mark_sent(event, now, count_attempt=False)
        _to_sent(session, event)
    else:
        reason = (error or "Druck fehlgeschlagen").strip() or "Druck fehlgeschlagen"
        final = mark_attempt_failed(event, reason, now, count_attempt=False)
        if final:
            log(
                logger,
                logging.ERROR,
                "Alarm: Kuechenbon endgueltig fehlgeschlagen",
                event_id=str(event.id),
                attempts=event.attempts,
                last_error=event.last_error,
            )
        _to_red(session, event, reason)
    session.commit()
    return event.status


def sweep(session: Session, now: datetime) -> int:
    """Waechter, laeuft im Dispatcher-Prozess. Liefert, wie viele Karten rot wurden.

    Faellt die Bruecke aus, fragt niemand nach Bons, und ohne Waechter bliebe die
    Karte gruen, waehrend die Kueche nichts weiss.
    """
    stale = session.scalars(
        select(OutboxEvent)
        .where(
            OutboxEvent.event_type.in_(KITCHEN),
            OutboxEvent.status == PENDING,
            OutboxEvent.next_attempt_at
            <= now - timedelta(seconds=PICKUP_TIMEOUT_SECONDS),
        )
        .order_by(OutboxEvent.created_at)
        .with_for_update(skip_locked=True)
    ).all()
    turned = 0
    for event in stale:
        if event.attempts >= MAX_ATTEMPTS:
            event.status = FAILED
            log(
                logger,
                logging.ERROR,
                "Alarm: Kuechenbon endgueltig fehlgeschlagen",
                event_id=str(event.id),
                attempts=event.attempts,
                last_error=event.last_error,
            )
        order = _order(session, event)
        before = order.handover_state if order is not None else None
        reason = (
            "Druckbruecke hat nicht zurueckgemeldet"
            if event.attempts
            else "Druckbruecke hat den Bon nicht abgeholt"
        )
        _to_red(session, event, reason)
        if order is not None and before != order.handover_state:
            turned += 1
    session.commit()
    return turned
