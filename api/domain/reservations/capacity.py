"""Freie Plätze je Kapazitätsfenster.

Annahme (docs/01): max_guests ist die Summe aller Gäste, deren Reservierung im Fenster
beginnt. Die Fenster bilden damit die Sitz-Turns des Betriebs ab. Entwürfe zählen mit,
damit während eines Gesprächs nichts doppelt vergeben wird; Stornierte zählen nicht.
"""

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.models import Capacity, Reservation

ACTIVE_STATUSES = ("draft", "confirmed")


@dataclass(frozen=True)
class CapacityWindow:
    start: datetime
    end: datetime
    max_guests: int
    slot_minutes: int

    def contains(self, at: datetime) -> bool:
        return self.start <= at < self.end

    def grid(self) -> list[datetime]:
        step = timedelta(minutes=self.slot_minutes)
        slots, cursor = [], self.start
        while cursor < self.end:
            slots.append(cursor)
            cursor += step
        return slots


def capacity_windows(
    session: Session, tenant_id: uuid.UUID, day: date, zone: ZoneInfo
) -> list[CapacityWindow]:
    rows = session.scalars(
        select(Capacity).where(
            Capacity.tenant_id == tenant_id, Capacity.weekday == day.weekday()
        )
    ).all()
    windows = []
    for row in rows:
        start = datetime.combine(day, row.slot_start, tzinfo=zone)
        end_day = day + timedelta(days=1) if row.slot_end <= row.slot_start else day
        end = datetime.combine(end_day, row.slot_end, tzinfo=zone)
        windows.append(CapacityWindow(start, end, row.max_guests, row.slot_minutes))
    return sorted(windows, key=lambda w: w.start)


def booked_guests(
    session: Session, tenant_id: uuid.UUID, windows: list[CapacityWindow]
) -> dict[CapacityWindow, int]:
    """Eine Abfrage für alle Fenster des Tages, danach Summen im Speicher."""
    if not windows:
        return {}
    rows = session.execute(
        select(Reservation.reserved_for, Reservation.party_size).where(
            Reservation.tenant_id == tenant_id,
            Reservation.status.in_(ACTIVE_STATUSES),
            Reservation.deleted_at.is_(None),
            Reservation.reserved_for >= min(w.start for w in windows),
            Reservation.reserved_for < max(w.end for w in windows),
        )
    ).all()
    booked = dict.fromkeys(windows, 0)
    for reserved_for, party_size in rows:
        for window in windows:
            if window.contains(reserved_for):
                booked[window] += party_size
                break
    return booked
