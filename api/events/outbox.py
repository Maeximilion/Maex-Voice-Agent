"""Outbox schreiben: der einzige Weg aus der Fachlogik nach draussen (docs/11 §events).

Der Eintrag entsteht in derselben Transaktion wie der Fachvorgang und wird deshalb
hier nicht committet: faellt der Vorgang zurueck, faellt das Ereignis mit zurueck.
"""

import uuid

from sqlalchemy.orm import Session

from api.events.types import ALL as EVENT_TYPES
from api.models import OutboxEvent


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
