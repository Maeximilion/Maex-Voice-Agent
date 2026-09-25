"""Outbox schreiben: der einzige Weg aus der Fachlogik nach draussen (docs/11 §events).

Der Eintrag entsteht in derselben Transaktion wie der Fachvorgang und wird deshalb
hier nicht committet: faellt der Vorgang zurueck, faellt das Ereignis mit zurueck.

Hier stehen auch die Regeln fuer Erfolg und Fehlversuch eines Ereignisses. Zwei
Zusteller benutzen sie: der Dispatcher Richtung n8n und die Druckbruecke fuer
den Kuechenbon (domain/ordering/handover.py). Beide sollen gleich zaehlen.
"""

import uuid
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from api.events.types import ALL as EVENT_TYPES
from api.models import OutboxEvent

# docs/03 §outbox: 5 s, 30 s, 2 min, 10 min, danach failed plus Alarm.
BACKOFF_SECONDS = (5, 30, 120, 600)
MAX_ATTEMPTS = len(BACKOFF_SECONDS) + 1
LAST_ERROR_MAX = 500


def enqueue(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    event_type: str,
    payload: dict,
) -> OutboxEvent:
    """Legt ein Ereignis als 'pending' ab. Faellig ist es sofort (Default next_attempt_at)."""
    if event_type not in EVENT_TYPES:
        # Programmierfehler, kein Fachfehler: der Agent bekommt das nie zu sehen.
        raise ValueError(f"unbekannter Ereignistyp: {event_type}")
    event = OutboxEvent(tenant_id=tenant_id, event_type=event_type, payload=payload)
    session.add(event)
    return event


def mark_sent(event: OutboxEvent, now: datetime, *, count_attempt: bool = True) -> None:
    """Zugestellt. `count_attempt=False`, wenn der Versuch schon beim Abholen zaehlte."""
    event.status = "sent"
    event.sent_at = now
    if count_attempt:
        event.attempts += 1
    event.last_error = None


def mark_attempt_failed(
    event: OutboxEvent, error: str, now: datetime, *, count_attempt: bool = True
) -> bool:
    """Fehlversuch buchen. True heisst endgueltig: `failed`, ab hier holt es niemand nach."""
    if count_attempt:
        event.attempts += 1
    event.last_error = error[:LAST_ERROR_MAX]
    if event.attempts >= MAX_ATTEMPTS:
        event.status = "failed"
        return True
    event.next_attempt_at = now + timedelta(seconds=BACKOFF_SECONDS[event.attempts - 1])
    return False
