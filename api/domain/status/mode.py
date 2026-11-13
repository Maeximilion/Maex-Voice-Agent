"""Which call mode holds now (T-8.1, docs/02 §4).

The team chooses a mode on the tablet (`config.set_mode`), and weekly windows
of `call_mode_windows` hold one of their own, say the agent answers on Friday
evenings and only listens in by day. A window that began after the team's last
tap holds; the tap holds against one that began before, also after that window
ends, until the next window begins. The emergency stop beats every window.
Where windows overlap, the most cautious mode counts, as the lowest limit of
overlapping `call_limit_windows` (T-10.9).

Read wherever the mode decides something: whether the agent answers a call
(`domain/calls/routing.py`), whether an order waits for "Passt"
(`domain/ordering/confirm.py`), the status tool and the tablet's header.
"""

from dataclasses import dataclass
from datetime import datetime, time, timedelta, tzinfo

from sqlalchemy import Text, cast, select
from sqlalchemy.orm import Session

from api.core import time as clock
from api.models import CallModeWindow, ServiceConfig, Tenant
from api.models.tenants import WINDOW_MODES

PAUSED = "paused"
# The modes of the switch and of a window, from the one in which the agent does
# the least to the one in which it does the most: one list, so a mode a window
# may hold always has a place in this order.
CAUTION = WINDOW_MODES


@dataclass(frozen=True)
class ModeNow:
    mode: str
    # When the window that holds the mode ends; None where the team's choice
    # or the emergency stop holds it.
    planned_until: datetime | None


def mode_at(
    session: Session,
    tenant: Tenant,
    config: ServiceConfig,
    now: datetime | None = None,
) -> ModeNow:
    if config.call_mode == PAUSED:
        return ModeNow(PAUSED, None)
    zone = clock.tz(tenant.timezone)
    local = (now or clock.utcnow()).astimezone(zone)
    # The columns, not the rows: a window to midnight ends at 24:00, which
    # Postgres stores and Python's time cannot hold. Its end is read as text
    # and becomes the next midnight (#682).
    windows = session.execute(
        select(
            CallModeWindow.starts_at,
            cast(CallModeWindow.ends_at, Text),
            CallModeWindow.call_mode,
        ).where(
            CallModeWindow.tenant_id == tenant.id,
            CallModeWindow.weekday == local.weekday(),
            CallModeWindow.starts_at <= local.time(),
            CallModeWindow.ends_at > local.time(),
        )
    ).all()
    # shortcut: a window across midnight is two rows, and the 00:00 row ends a tap
    # made in the first; join the rows when the tablet gets a window editor.
    chosen = config.call_mode_chosen_at
    planned = [
        (mode, _end(local, ends, zone))
        for starts, ends, mode in windows
        if chosen is None or datetime.combine(local.date(), starts, zone) > chosen
    ]
    if not planned:
        return ModeNow(config.call_mode, None)
    mode = min((m for m, _ in planned), key=CAUTION.index)
    # Of overlapping windows of that mode the latest end, whatever the row order.
    return ModeNow(mode, max(until for m, until in planned if m == mode))


def _end(local: datetime, ends: str, zone: tzinfo) -> datetime:
    """A window's end on the day of `local`; "24:00:00" is the next midnight."""
    if ends.startswith("24:"):
        return datetime.combine(local.date() + timedelta(days=1), time(0), zone)
    return datetime.combine(local.date(), time.fromisoformat(ends), zone)
