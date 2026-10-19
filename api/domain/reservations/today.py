"""Reservierungen des laufenden Betriebstags (docs/06 §3, Spalte "Heute").

Nur lesend. Die Betriebsansicht zeigt ausschliesslich bestätigte Reservierungen:
ein Entwurf ist nach CLAUDE.md §2 Regel 3 keine Buchung und darf auf dem Tablet
nicht wie eine aussehen.

Der Tag ist der Betriebstag aus core/time.py, nicht der Kalendertag: eine
Reservierung um 00:30 gehört zum Abend davor, so wie Küche und Kasse rechnen.
"""

import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from api.core.time import business_day, business_day_bounds_utc
from api.models import Reservation
from api.schemas.gui import TodayReservation

CONFIRMED = "confirmed"


def _window(tz_name: str, now: datetime | None) -> tuple[datetime, datetime]:
    return business_day_bounds_utc(business_day(now, tz_name), tz_name)


def _filters(tenant_id: uuid.UUID, start: datetime, end: datetime) -> tuple:
    return (
        Reservation.tenant_id == tenant_id,
        Reservation.status == CONFIRMED,
        Reservation.deleted_at.is_(None),
        Reservation.reserved_for >= start,
        Reservation.reserved_for < end,
    )


def list_today(
    session: Session,
    tenant_id: uuid.UUID,
    tz_name: str,
    now: datetime | None = None,
) -> list[TodayReservation]:
    """Bestätigte Reservierungen des Betriebstags, nach Uhrzeit aufsteigend."""
    start, end = _window(tz_name, now)
    rows = session.scalars(
        select(Reservation)
        .where(*_filters(tenant_id, start, end))
        .order_by(Reservation.reserved_for, Reservation.created_at)
    ).all()
    return [
        TodayReservation(
            reservation_id=row.id,
            status=CONFIRMED,
            reserved_for=row.reserved_for,
            party_size=row.party_size,
            guest_name=row.guest_name,
            phone=row.phone,
            note=row.note,
        )
        for row in rows
    ]


def today_change_token(
    session: Session,
    tenant_id: uuid.UUID,
    tz_name: str,
    now: datetime | None = None,
) -> str:
    """Billiger Fingerabdruck derselben Menge für den Ereignisstrom (gui/sse.py).

    Anzahl plus jüngstes updated_at: eine neue, geänderte oder stornierte
    Reservierung verändert mindestens einen der beiden Werte. Bewusst über die
    Datenbank statt über einen prozessinternen Kanal, weil auch sim/ und später
    die Telefonie in eigenen Prozessen schreiben - die Tablet-Ansicht muss deren
    Buchungen genauso sehen wie die der API.
    """
    start, end = _window(tz_name, now)
    count, newest = session.execute(
        select(func.count(Reservation.id), func.max(Reservation.updated_at)).where(
            *_filters(tenant_id, start, end)
        )
    ).one()
    return f"{count}:{newest.isoformat() if newest else '-'}"
