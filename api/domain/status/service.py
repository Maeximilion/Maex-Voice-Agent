"""get_service_status: Geht gerade etwas, und was? Alles aus der Datenbank, nichts geschätzt."""

import uuid
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from api.core.errors import NotFound
from api.core.time import utcnow
from api.domain.status.hours import (
    HoursData,
    all_services,
    load_hours,
    next_opening,
    open_window_at,
)
from api.models import ServiceConfig, Tenant
from api.schemas.status import ServiceStatus

WEEKDAYS = (
    "Montag",
    "Dienstag",
    "Mittwoch",
    "Donnerstag",
    "Freitag",
    "Samstag",
    "Sonntag",
)
DELIVERY = "delivery"
PICKUP = "pickup"


def get_service_status(
    session: Session, tenant_id: uuid.UUID, now: datetime | None = None
) -> ServiceStatus:
    tenant = session.get(Tenant, tenant_id)
    config = session.get(ServiceConfig, tenant_id)
    if tenant is None or config is None:
        raise NotFound("Mandant unbekannt oder ohne service_config")

    zone = ZoneInfo(tenant.timezone)
    now = now or utcnow()
    data = load_hours(session, tenant_id, now.astimezone(zone).date())

    # Ein pausierter Service zählt nicht als offen, auch wenn sein Fenster läuft.
    enabled = [s for s in all_services() if s != DELIVERY or config.delivery_enabled]
    windows = {s: open_window_at(data, now, s, zone) for s in all_services()}
    active = [windows[s] for s in enabled if windows[s] is not None]
    is_open = bool(active)
    closes_at = max(closes for _, closes in active).astimezone(UTC) if is_open else None
    pickup_open = windows[PICKUP] is not None
    delivery_open = windows[DELIVERY] is not None

    say = None
    if not is_open:
        say = _say_closed(next_opening(data, now, enabled, zone), now, zone)
    elif delivery_open and not config.delivery_enabled:
        say = "Lieferung ist gerade pausiert."
        if pickup_open:
            say += " Abholung ist möglich."

    return ServiceStatus(
        is_open=is_open,
        closes_at=closes_at,
        pickup_enabled=pickup_open,
        delivery_enabled=delivery_open and config.delivery_enabled,
        pickup_wait_minutes=config.pickup_wait_minutes,
        delivery_wait_minutes=config.delivery_wait_minutes,
        call_mode=config.call_mode,
        say=say,
    )


def _say_closed(opening: datetime | None, now: datetime, zone: ZoneInfo) -> str:
    if opening is None:
        return "Wir haben derzeit geschlossen."
    local_now = now.astimezone(zone)
    local_open = opening.astimezone(zone)
    days_ahead = (local_open.date() - local_now.date()).days
    if days_ahead == 0:
        when = "heute"
    elif days_ahead == 1:
        when = "morgen"
    else:
        when = f"am {WEEKDAYS[local_open.weekday()]}"
    return f"Wir haben gerade geschlossen. Wir öffnen wieder {when} um {_clock(local_open)}."


def _clock(dt: datetime) -> str:
    return f"{dt.hour} Uhr" if dt.minute == 0 else f"{dt.hour}:{dt.minute:02d} Uhr"


__all__ = ["HoursData", "get_service_status"]
