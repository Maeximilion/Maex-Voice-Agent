"""check_slot: Ist der Wunschtermin frei, und wenn nicht, welche zwei Termine liegen am nächsten?"""

import uuid
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from api.core.errors import InvalidInput, NotFound
from api.core.time import utcnow
from api.domain.reservations.capacity import (
    CapacityWindow,
    booked_guests,
    capacity_windows,
)
from api.domain.reservations.spoken import spoken_time
from api.domain.status.hours import load_hours, windows_for_day
from api.models import Tenant
from api.schemas.reservations import SlotCheck

MAX_ALTERNATIVES = 2
DINEIN = "dinein"


def check_slot(
    session: Session,
    tenant_id: uuid.UUID,
    reserved_for: datetime,
    party_size: int,
    now: datetime | None = None,
) -> SlotCheck:
    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        raise NotFound("Mandant unbekannt")
    now = now or utcnow()
    if reserved_for <= now:
        raise InvalidInput(
            "Zeitpunkt liegt in der Vergangenheit",
            say="Dieser Zeitpunkt ist schon vorbei.",
        )

    zone = ZoneInfo(tenant.timezone)
    local = reserved_for.astimezone(zone)
    # Fenster des Vortags reichen über Mitternacht (18:00 bis 01:00), deshalb beide Tage.
    days = (local.date() - timedelta(days=1), local.date())

    hours = load_hours(session, tenant_id, days[0], days=2)
    open_windows = [w for d in days for w in windows_for_day(hours, d, DINEIN, zone)]
    windows = [w for d in days for w in capacity_windows(session, tenant_id, d, zone)]
    booked = booked_guests(session, tenant_id, windows)

    def fits(at: datetime) -> bool:
        if not any(opens <= at < closes for opens, closes in open_windows):
            return False
        window = _window_for(windows, at)
        if window is None:
            return False
        return booked[window] + party_size <= window.max_guests

    if fits(local):
        return SlotCheck(available=True)

    candidates = [
        slot
        for window in windows
        for slot in window.grid()
        if slot != local and slot > now and fits(slot)
    ]
    candidates.sort(key=lambda slot: (abs(slot - local), slot))
    alternatives = [slot.astimezone(UTC) for slot in candidates[:MAX_ALTERNATIVES]]
    return SlotCheck(
        available=False,
        alternatives=alternatives,
        say=_say(local, candidates[:MAX_ALTERNATIVES]),
    )


def _window_for(windows: list[CapacityWindow], at: datetime) -> CapacityWindow | None:
    return next((w for w in windows if w.contains(at)), None)


def _say(wish: datetime, alternatives: list[datetime]) -> str:
    if not alternatives:
        return f"Um {spoken_time(wish)} ist leider nichts frei, und an dem Tag auch sonst nicht."
    options = " oder ".join(spoken_time(a) for a in alternatives)
    return f"Um {spoken_time(wish)} ist leider voll. {options[0].upper() + options[1:]} ginge."
