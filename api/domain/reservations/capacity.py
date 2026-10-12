"""Freie Plätze je Kapazitätsfenster.

Annahme (docs/01): max_guests ist die Summe aller Gäste, deren Reservierung im Fenster
beginnt. Die Fenster bilden damit die Sitz-Turns des Betriebs ab. Entwürfe zählen mit,
damit während eines Gesprächs nichts doppelt vergeben wird; Stornierte zählen nicht.

A draft counts only while its call can still confirm it: the call has not ended
and the draft is younger than the longest a call may last
(`service_config.max_call_seconds`). Without that, a caller who asks for forty
seats and hangs up at the read-back keeps them for good, and the tablet, which
lists confirmed tables only, shows nothing (#250). Because a draft can lose its
seats before the yes, `confirm` asks `has_room` again under the day lock.
"""

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import and_, or_, select, text
from sqlalchemy.orm import Session

from api.models import Call, Capacity, Reservation


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


def lock_capacity_days(session: Session, tenant_id: uuid.UUID, day: date) -> None:
    """Prüfen und Anlegen laufen je Betrieb und Tag nacheinander, sonst überbuchen
    zwei gleichzeitige Anrufe dasselbe Fenster: beide lesen die Kapazität, bevor
    einer schreibt. Die Sperre hält bis zum Ende der Transaktion.

    Gesperrt wird, was check_slot liest: die Fenster von Vortag und Tag. Montag
    23:30 und Dienstag 00:30 fallen ins selbe Fenster über Mitternacht und teilen
    sich so die Sperre "Montag" (Review PR #152). Immer aufsteigend, damit zwei
    Anrufe nicht übers Kreuz aufeinander warten."""
    for locked in (day - timedelta(days=1), day):
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:key)::bigint)"),
            {"key": f"reservations:{tenant_id}:{locked.isoformat()}"},
        )


def booked_guests(
    session: Session,
    tenant_id: uuid.UUID,
    windows: list[CapacityWindow],
    *,
    now: datetime,
    draft_lifetime: timedelta,
    exclude: uuid.UUID | None = None,
) -> dict[CapacityWindow, int]:
    """Eine Abfrage für alle Fenster des Tages, danach Summen im Speicher.

    `draft_lifetime` is the longest a call may last, from the database.
    `exclude` leaves one reservation out: the one whose own seats are asked for.
    """
    if not windows:
        return {}
    live_draft = and_(
        Reservation.status == "draft",
        Call.ended_at.is_(None),
        Reservation.created_at > now - draft_lifetime,
    )
    query = (
        select(Reservation.reserved_for, Reservation.party_size)
        .join(Call, Call.id == Reservation.call_id)
        .where(
            Reservation.tenant_id == tenant_id,
            or_(Reservation.status == "confirmed", live_draft),
            Reservation.deleted_at.is_(None),
            Reservation.reserved_for >= min(w.start for w in windows),
            Reservation.reserved_for < max(w.end for w in windows),
        )
    )
    if exclude is not None:
        query = query.where(Reservation.id != exclude)
    rows = session.execute(query).all()
    booked = dict.fromkeys(windows, 0)
    for reserved_for, party_size in rows:
        for window in windows:
            if window.contains(reserved_for):
                booked[window] += party_size
                break
    return booked


def has_room(
    session: Session,
    reservation: Reservation,
    zone: ZoneInfo,
    *,
    now: datetime,
    draft_lifetime: timedelta,
) -> bool:
    """True while the window of `reservation` holds its party next to everything
    else that counts. Its own seats are left out, so a live draft does not stand
    in its own way. Seats only: opening hours and lead times were checked when
    the draft was made, and minutes passing during the read-back must not turn
    a yes into "too late". The caller holds `lock_capacity_days`.
    """
    day = reservation.reserved_for.astimezone(zone).date()
    # Same two days and the same order as check_slot, so both pick the same window.
    windows = [
        window
        for d in (day - timedelta(days=1), day)
        for window in capacity_windows(session, reservation.tenant_id, d, zone)
    ]
    window = next((w for w in windows if w.contains(reservation.reserved_for)), None)
    if window is None:
        return False
    booked = booked_guests(
        session,
        reservation.tenant_id,
        windows,
        now=now,
        draft_lifetime=draft_lifetime,
        exclude=reservation.id,
    )
    return booked[window] + reservation.party_size <= window.max_guests
