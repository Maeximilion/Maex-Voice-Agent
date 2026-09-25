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
from zoneinfo import ZoneInfo

from sqlalchemy import Integer, cast, or_, select
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Session

from api.core.errors import NotFound
from api.core.logging import get_logger, log
from api.events import enqueue
from api.events.outbox import MAX_ATTEMPTS, mark_attempt_failed, mark_sent
from api.events.types import KITCHEN, ORDER_HANDOVER_FAILED
from api.models import AuditLog, Order, OutboxEvent, Tenant

logger = get_logger("api.domain.ordering.handover")

# So lange darf die Bruecke fuer Druck und Rueckmeldung brauchen.
LEASE_SECONDS = 60
# So lange darf ein faelliger Bon unabgeholt liegen, bevor die Karte rot wird.
PICKUP_TIMEOUT_SECONDS = 60
CLAIM_LIMIT_MAX = 10

PENDING = "pending"
SENT = "sent"
FAILED = "failed"

ACTOR = "system"
ACTION_HANDOVER_SENT = "order.handover_sent"
ACTION_HANDOVER_FAILED = "order.handover_failed"


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
        # Nach dem Warten auf die Sperre den Stand der Datenbank, nicht den alten.
        .execution_options(populate_existing=True)
    )


def _to_red(session: Session, event: OutboxEvent, reason: str) -> None:
    """Karte rot, wenn dieser Bon der aktuelle ist und die Kueche ihn noch nicht hat.

    Erst die Bestellung sperren, dann die Revision pruefen: eine Korrektur, die
    gerade committet, haelt dieselbe Sperre. Vorher gelesen, galte ein Bon als
    aktuell, den die Korrektur eben ueberholt hat (Codex PR #143).
    """
    order = _order(session, event)
    if order is None or not _is_current(session, event):
        return
    # Nur aus `pending`: `sent` hat ein anderer Bon derselben Revision schon
    # geschafft ("Nochmal senden"), `failed` ist schon gemeldet.
    if order.handover_state != PENDING:
        return
    order.handover_state = FAILED
    _audit(session, event, order, ACTION_HANDOVER_FAILED, reason)
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
    # Reihenfolge wie in `_to_red`: Sperre vor der Revisionspruefung.
    order = _order(session, event)
    if order is None or not _is_current(session, event):
        return
    if order.handover_state in (PENDING, FAILED):
        order.handover_state = SENT
        _audit(session, event, order, ACTION_HANDOVER_SENT)


def _audit(
    session: Session,
    event: OutboxEvent,
    order: Order,
    action: str,
    reason: str | None = None,
) -> None:
    """Jeder Wechsel der Karte steht im audit_log (docs/02 §7), ohne Personendaten."""
    payload: dict[str, Any] = {
        "call_id": str(order.call_id),
        "event_id": str(event.id),
        "revision": _revision(event),
    }
    if reason:
        payload["reason"] = reason
    session.add(
        AuditLog(
            tenant_id=order.tenant_id,
            actor=ACTOR,
            action=action,
            entity="order",
            entity_id=order.id,
            payload=payload,
        )
    )


def _supersede(event: OutboxEvent, newest: int, now: datetime) -> None:
    """Ueberholter Bon: erledigt, ohne Druck und ohne Alarm."""
    mark_sent(event, now, count_attempt=False)
    event.last_error = f"nicht gedruckt: ueberholt von Revision {newest}"


def _local_times(session: Session, tenant_id: uuid.UUID, now: datetime) -> tuple:
    """Zeitzone des Betriebs: der Bon zeigt Europe/Berlin, egal wo die Bruecke laeuft."""
    tz = ZoneInfo(session.scalar(select(Tenant.timezone).where(Tenant.id == tenant_id)))
    return tz, now.astimezone(tz).strftime("%H:%M")


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
    tz, print_time = _local_times(session, tenant_id, now) if rows else (None, None)
    for event in rows:
        newest = _newest_revision(session, event) or 0
        if _revision(event) < newest:
            _supersede(event, newest, now)
            continue
        event.attempts += 1
        event.next_attempt_at = now + timedelta(seconds=LEASE_SECONDS)
        ready_at = event.payload.get("ready_at")
        tickets.append(
            {
                "id": str(event.id),
                "attempt": event.attempts,
                "ticket": event.payload,
                # Uhrzeiten schon in Ortszeit des Betriebs: die Bruecke kennt
                # keine Zeitzone (CLAUDE.md §8, Anzeige in Europe/Berlin).
                "ready_time": datetime.fromisoformat(ready_at)
                .astimezone(tz)
                .strftime("%H:%M")
                if ready_at
                else None,
                "print_time": print_time,
            }
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

    Sperrreihenfolge wie im Tablet: erst die Bestellung, dann der Bon. Sonst
    ueberspringt "Nochmal senden" den vom Waechter gesperrten Bon, legt einen
    zweiten an, und der Waechter faerbt die Karte gleich danach wieder rot.
    """
    cutoff = now - timedelta(seconds=PICKUP_TIMEOUT_SECONDS)
    stale = select(OutboxEvent.id).where(
        OutboxEvent.event_type.in_(KITCHEN),
        OutboxEvent.status == PENDING,
        OutboxEvent.next_attempt_at <= cutoff,
    )
    # Nur, was etwas zu tun gibt: eine Karte, die noch nicht rot ist, oder ein
    # Bon ohne Versuche mehr. Eine rote Karte wird nicht alle 5 s neu gesperrt.
    candidates = session.scalars(
        stale.join(
            Order,
            Order.id == cast(OutboxEvent.payload["order_id"].astext, PG_UUID),
        )
        .where(
            or_(Order.handover_state == PENDING, OutboxEvent.attempts >= MAX_ATTEMPTS)
        )
        .order_by(OutboxEvent.created_at)
    ).all()
    turned = 0
    for event_id in candidates:
        probe = session.get(OutboxEvent, event_id)
        if probe is None:
            continue
        order = _order(session, probe)
        # Nach der Sperre der Bestellung neu lesen: das Tablet kann den Bon
        # inzwischen neu faellig gemacht oder die Bruecke ihn gemeldet haben.
        event = session.scalar(
            stale.where(OutboxEvent.id == event_id)
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
            .with_only_columns(OutboxEvent)
        )
        if event is None:
            session.commit()
            continue
        if event.attempts >= MAX_ATTEMPTS:
            newest = _newest_revision(session, event) or 0
            if _revision(event) < newest:
                _supersede(event, newest, now)
                session.commit()
                continue
            event.status = FAILED
            log(
                logger,
                logging.ERROR,
                "Alarm: Kuechenbon endgueltig fehlgeschlagen",
                event_id=str(event.id),
                attempts=event.attempts,
                last_error=event.last_error,
            )
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
