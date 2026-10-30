"""Öffnungsfenster aus opening_hours und special_days. Reine Funktionen über geladenen Zeilen.

Ein Fenster ist ein Paar zeitzonenbewusster datetimes [opens, closes). Endet closes_at nicht
nach opens_at (z. B. 18:00 bis 01:00), reicht das Fenster in den Folgetag.
"""

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.models import OpeningHours, SpecialDay

Window = tuple[datetime, datetime]

LOOKAHEAD_DAYS = 14


@dataclass(frozen=True)
class HoursData:
    regular: tuple[OpeningHours, ...]
    special: dict[date, SpecialDay]


def load_hours(
    session: Session, tenant_id: uuid.UUID, start: date, days: int = LOOKAHEAD_DAYS
) -> HoursData:
    regular = tuple(
        session.scalars(
            select(OpeningHours).where(OpeningHours.tenant_id == tenant_id)
        ).all()
    )
    special_rows = session.scalars(
        select(SpecialDay).where(
            SpecialDay.tenant_id == tenant_id,
            # A suggested holiday counts once the team said so (T-9.5).
            SpecialDay.status == "set",
            SpecialDay.date >= start - timedelta(days=1),
            SpecialDay.date <= start + timedelta(days=days),
        )
    ).all()
    return HoursData(regular=regular, special={row.date: row for row in special_rows})


def _window(day: date, opens: time, closes: time, zone: ZoneInfo) -> Window:
    start = datetime.combine(day, opens, tzinfo=zone)
    end_day = day + timedelta(days=1) if closes <= opens else day
    return start, datetime.combine(end_day, closes, tzinfo=zone)


def windows_for_day(
    data: HoursData, day: date, service: str, zone: ZoneInfo
) -> list[Window]:
    """Sondertag schlägt Wochentag: geschlossen heißt keine Fenster, Sonderzeiten gelten für alle Services.

    A special day like another weekday ("wie Sonntag", T-9.5) takes that
    weekday's windows of the service, on its own date."""
    special = data.special.get(day)
    weekday = day.weekday()
    if special is not None:
        if special.closed:
            return []
        if special.opens_at is not None and special.closes_at is not None:
            return [_window(day, special.opens_at, special.closes_at, zone)]
        if special.like_weekday is not None:
            weekday = special.like_weekday
    return sorted(
        _window(day, row.opens_at, row.closes_at, zone)
        for row in data.regular
        if row.service == service and row.weekday == weekday
    )


def open_window_at(
    data: HoursData, now: datetime, service: str, zone: ZoneInfo
) -> Window | None:
    """Das Fenster, in dem now liegt. Prüft auch den Vortag, weil Fenster über Mitternacht reichen."""
    local = now.astimezone(zone)
    for day in (local.date() - timedelta(days=1), local.date()):
        for opens, closes in windows_for_day(data, day, service, zone):
            if opens <= local < closes:
                return opens, closes
    return None


def next_opening(
    data: HoursData,
    now: datetime,
    services: Iterable[str],
    zone: ZoneInfo,
    days: int = LOOKAHEAD_DAYS,
) -> datetime | None:
    local = now.astimezone(zone)
    candidates: list[datetime] = []
    for offset in range(days + 1):
        day = local.date() + timedelta(days=offset)
        for service in services:
            candidates.extend(
                opens
                for opens, _ in windows_for_day(data, day, service, zone)
                if opens > local
            )
        if candidates:
            return min(candidates)
    return None
