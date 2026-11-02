"""get_service_status: Geht gerade etwas, und was? Alles aus der Datenbank, nichts geschätzt."""

import uuid
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from api.core.errors import NotFound
from api.core.time import utcnow
from api.domain.reservations.spoken import WEEKDAYS
from api.domain.status.hours import load_hours, next_opening, open_window_at
from api.domain.status.mode import mode_at
from api.domain.status.waits import current_wait
from api.models import ServiceConfig, Tenant
from api.models.tenants import SERVICES
from api.schemas.status import ServiceStatus

DELIVERY = "delivery"
PICKUP = "pickup"
SAY_NO_RESERVATIONS = "Reservierungen nehmen wir gerade nicht an."


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
    switched_on = {PICKUP: config.pickup_enabled, DELIVERY: config.delivery_enabled}
    enabled = [s for s in SERVICES if switched_on.get(s, True)]
    windows = {s: open_window_at(data, now, s, zone) for s in SERVICES}
    active = [w for w in (windows[s] for s in enabled) if w is not None]
    is_open = bool(active)
    closes_at = max(closes for _, closes in active).astimezone(UTC) if is_open else None
    pickup_open = windows[PICKUP] is not None
    delivery_open = windows[DELIVERY] is not None

    say = None
    if not is_open:
        say = _say_closed(next_opening(data, now, enabled, zone), now, zone)
    else:
        say = _say_paused(
            pickup_open and not config.pickup_enabled,
            delivery_open and not config.delivery_enabled,
            pickup_open and config.pickup_enabled,
            delivery_open and config.delivery_enabled,
        )
    if not config.reservations_enabled:
        say = f"{say} {SAY_NO_RESERVATIONS}" if say else SAY_NO_RESERVATIONS

    return ServiceStatus(
        is_open=is_open,
        closes_at=closes_at,
        pickup_enabled=pickup_open and config.pickup_enabled,
        delivery_enabled=delivery_open and config.delivery_enabled,
        reservations_enabled=config.reservations_enabled,
        pickup_wait_minutes=current_wait(config, PICKUP, now),
        delivery_wait_minutes=current_wait(config, DELIVERY, now),
        call_mode=mode_at(session, tenant, config, now).mode,
        say=say,
    )


def _say_paused(
    pickup_paused: bool,
    delivery_paused: bool,
    pickup_possible: bool,
    delivery_possible: bool,
) -> str | None:
    """What the team switched off while its hours run, and what is left of it."""
    if pickup_paused and delivery_paused:
        return "Abholung und Lieferung sind gerade pausiert."
    if pickup_paused:
        say = "Abholung ist gerade pausiert."
        return f"{say} Lieferung ist möglich." if delivery_possible else say
    if delivery_paused:
        say = "Lieferung ist gerade pausiert."
        return f"{say} Abholung ist möglich." if pickup_possible else say
    return None


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
