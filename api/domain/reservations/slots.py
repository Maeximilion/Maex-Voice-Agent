"""check_slot: Ist der Wunschtermin frei, und wenn nicht, welche zwei Termine im selben Service?

Ein Service ist ein Öffnungsfenster für `dinein` (Mittag, Abend). Buchbar ist ein Beginn
frühestens `reservation_lead_minutes` nach Öffnung und spätestens
`reservation_last_start_minutes` vor Schluss (T-1.14, D12). Alternativen kommen nur aus
dem Service des Wunsches; außerhalb der Öffnung heißt es „geschlossen", nur morgens vor
der ersten Öffnung darf ein späterer Service desselben Tages einspringen.
"""

import uuid
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from api.core.errors import InvalidInput, NotFound
from api.core.time import utcnow
from api.domain.reservations.capacity import (
    CapacityWindow,
    booked_guests,
    capacity_windows,
)
from api.domain.reservations.spoken import WEEKDAYS, spoken_daytime, spoken_time
from api.domain.status.hours import Window, load_hours, windows_for_day
from api.models import ServiceConfig, Tenant
from api.schemas.reservations import SlotCheck

MAX_ALTERNATIVES = 2
# Eine Alternative wird ohne Tag angesagt ("halb zwei"), der Gast hoert die Deutung,
# die dem Wunsch am naechsten liegt. Eindeutig ist sie nur unter sechs Stunden
# Abstand: die andere Haelfte der Uhr (+-12 h) liegt dann weiter weg, jeder andere
# Tag (+-24 h) sowieso. Sonst kaeme am Ruhetag Sonntag 21:30 als "halb zehn"
# (Befund T-5.2, reservierung_0027) und nachts um halb eins der Mittag als "halb
# zwei" (Review PR #152). Keine Oeffnungszeit, sondern eine Regel der Ansage.
# Weiter weg (morgens auf den Abend) nennt der Satz die Tageszeit.
UNAMBIGUOUS = timedelta(hours=6)
DINEIN = "dinein"
ASK_OTHER_TIME = "Zu welcher anderen Uhrzeit passt es Ihnen?"


def check_slot(
    session: Session,
    tenant_id: uuid.UUID,
    reserved_for: datetime,
    party_size: int,
    now: datetime | None = None,
) -> SlotCheck:
    tenant = session.get(Tenant, tenant_id)
    config = session.get(ServiceConfig, tenant_id)
    if tenant is None or config is None:
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
    yesterday, today = local.date() - timedelta(days=1), local.date()

    hours = load_hours(session, tenant_id, yesterday, days=2)
    today_services = windows_for_day(hours, today, DINEIN, zone)
    services = windows_for_day(hours, yesterday, DINEIN, zone) + today_services
    windows = [
        w
        for d in (yesterday, today)
        for w in capacity_windows(session, tenant_id, d, zone)
    ]
    booked = booked_guests(session, tenant_id, windows)
    lead = timedelta(minutes=config.reservation_lead_minutes)
    last = timedelta(minutes=config.reservation_last_start_minutes)

    def startable(at: datetime, service: Window) -> bool:
        opens, closes = service
        return opens + lead <= at <= closes - last

    def fits(at: datetime, service: Window) -> bool:
        if not startable(at, service):
            return False
        window = _window_for(windows, at)
        if window is None:
            return False
        return booked[window] + party_size <= window.max_guests

    def free_slots(service: Window) -> list[datetime]:
        return [
            slot
            for window in windows
            for slot in window.grid()
            if slot != local and slot > now and fits(slot, service)
        ]

    service = next((s for s in services if s[0] <= local < s[1]), None)
    if service is not None and fits(local, service):
        return SlotCheck(available=True)

    # In UTC gemessen: zwei Zeiten derselben ZoneInfo zieht Python als Wandzeit ab,
    # an der Zeitumstellung laege die Grenze sonst eine Stunde daneben.
    wish_utc = local.astimezone(UTC)

    def distance(slot: datetime) -> timedelta:
        return abs(slot.astimezone(UTC) - wish_utc)

    if service is not None:
        candidates = [s for s in free_slots(service) if distance(s) < UNAMBIGUOUS]
        candidates.sort(key=lambda slot: (distance(slot), slot))
        chosen = candidates[:MAX_ALTERNATIVES]
        if startable(local, service):
            say = _say_full(local, chosen)
        else:
            say = _say_with_options(
                f"Um {spoken_time(local)} können wir leider keinen Tisch reservieren.",
                local,
                chosen,
            )
        return _unavailable(chosen, say)

    # Ruhetag oder geschlossener Sondertag: der Gast soll das hoeren, statt es mit
    # anderen Uhrzeiten am selben Tag zu versuchen (Review PR #152). Nicht, wenn der
    # Wunsch noch im Fenster des Vorabends liegt (Montag 00:30 nach Sonntag).
    if not today_services:
        return _unavailable(
            [], f"Am {WEEKDAYS[local.weekday()]} haben wir leider geschlossen."
        )

    if _is_morning(local, services, today_services):
        for later in today_services:
            chosen = free_slots(later)[:MAX_ALTERNATIVES]
            if chosen:
                return _unavailable(
                    chosen,
                    _say_with_options(
                        f"Um {spoken_time(local)} haben wir noch geschlossen.",
                        local,
                        chosen,
                    ),
                )

    # D12: zwischen zwei Services und nach dem letzten keine Alternative aus einem
    # anderen Service, der Gast nennt selbst eine Uhrzeit.
    return _unavailable(
        [], f"Um {spoken_time(local)} haben wir leider geschlossen. {ASK_OTHER_TIME}"
    )


def _is_morning(local: datetime, services: list[Window], today: list[Window]) -> bool:
    """Vor der ersten Öffnung des Tages und näher an ihr als am letzten Schluss davor.

    Dienstag 01:00 nach einem Montagabend bis 01:00 ist Nacht, kein Morgen (D12).
    Ohne Fenster am Vortag (Ruhetag) zählt Mitternacht als letzter Schluss, sonst
    wäre 01:00 nach dem Ruhetag ein Morgen (Review PR #158). Gerechnet in UTC wie
    `distance`, sonst läge die Grenze an der Zeitumstellung eine Stunde daneben.
    """
    wish = local.astimezone(UTC)
    first_open = today[0][0].astimezone(UTC)
    if wish >= first_open:
        return False
    midnight = datetime.combine(local.date(), time(0), tzinfo=local.tzinfo)
    last_close = max(
        [c.astimezone(UTC) for _, c in services if c.astimezone(UTC) <= wish]
        + [midnight.astimezone(UTC)]
    )
    return first_open - wish < wish - last_close


def _window_for(windows: list[CapacityWindow], at: datetime) -> CapacityWindow | None:
    return next((w for w in windows if w.contains(at)), None)


def _unavailable(chosen: list[datetime], say: str) -> SlotCheck:
    return SlotCheck(
        available=False,
        alternatives=[slot.astimezone(UTC) for slot in chosen],
        say=say,
    )


def _say_full(wish: datetime, alternatives: list[datetime]) -> str:
    if not alternatives:
        # Nicht "an dem Tag": gesucht wird nur im Abstand, in dem die Ansage
        # eindeutig ist; ein Termin weiter weg kann noch frei sein (Review PR #152).
        return f"Um {spoken_time(wish)} ist leider nichts frei, auch nicht kurz davor oder danach."
    return _say_with_options(
        f"Um {spoken_time(wish)} ist leider voll.", wish, alternatives
    )


def _say_with_options(
    lead_in: str, wish: datetime, alternatives: list[datetime]
) -> str:
    if not alternatives:
        return f"{lead_in} {ASK_OTHER_TIME}"
    options = " oder ".join(_spoken_options(wish, alternatives))
    return f"{lead_in} {options[0].upper() + options[1:]} ginge."


def _spoken_options(wish: datetime, alternatives: list[datetime]) -> list[str]:
    """Ab sechs Stunden Abstand mit Tageszeit, gleiche Tageszeit nur einmal."""
    parts, previous = [], None
    for slot in alternatives:
        if abs(slot.astimezone(UTC) - wish.astimezone(UTC)) < UNAMBIGUOUS:
            parts.append(spoken_time(slot))
            previous = None
            continue
        daytime, clock = spoken_daytime(slot).split(" ", 1)
        parts.append(clock if daytime == previous else f"{daytime} {clock}")
        previous = daytime
    return parts
