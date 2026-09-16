"""Zeit-Helfer: UTC speichern, in der Mandanten-Zeitzone rechnen, Betriebstag statt Kalendertag."""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from api.config import settings

# Annahme (docs/01_STATUS.md): Der Betriebstag beginnt um 05:00 Ortszeit. Eine Bestellung
# um 00:30 gehört damit noch zum Vortag, so wie Küche und Kasse den Abend abrechnen.
DAY_STARTS_AT = time(5, 0)


def tz(name: str | None = None) -> ZoneInfo:
    return ZoneInfo(name or settings.tenant_timezone)


def utcnow() -> datetime:
    return datetime.now(UTC)


def to_local(dt: datetime, tz_name: str | None = None) -> datetime:
    if dt.tzinfo is None:
        raise ValueError("naive datetime: Zeiten werden nur mit Zeitzone verarbeitet")
    return dt.astimezone(tz(tz_name))


def to_utc(dt: datetime, tz_name: str | None = None) -> datetime:
    """Naive Werte gelten als Ortszeit des Mandanten (z. B. aus der GUI)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz(tz_name))
    return dt.astimezone(UTC)


def business_day(
    dt: datetime | None = None,
    tz_name: str | None = None,
    day_starts_at: time = DAY_STARTS_AT,
) -> date:
    local = to_local(dt or utcnow(), tz_name)
    if local.time() < day_starts_at:
        return local.date() - timedelta(days=1)
    return local.date()


def business_day_bounds_utc(
    day: date,
    tz_name: str | None = None,
    day_starts_at: time = DAY_STARTS_AT,
) -> tuple[datetime, datetime]:
    """[Beginn, Ende) des Betriebstags in UTC, korrekt über Sommerzeit-Umstellungen hinweg."""
    zone = tz(tz_name)
    start = datetime.combine(day, day_starts_at, tzinfo=zone)
    end = datetime.combine(day + timedelta(days=1), day_starts_at, tzinfo=zone)
    return start.astimezone(UTC), end.astimezone(UTC)
