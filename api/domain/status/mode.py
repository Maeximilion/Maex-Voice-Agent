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
from datetime import datetime

from sqlalchemy import select
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
    windows = session.scalars(
        select(CallModeWindow).where(
            CallModeWindow.tenant_id == tenant.id,
            CallModeWindow.weekday == local.weekday(),
            CallModeWindow.starts_at <= local.time(),
            CallModeWindow.ends_at > local.time(),
        )
    ).all()
    chosen = config.call_mode_chosen_at
    planned = [
        w
        for w in windows
        if chosen is None or datetime.combine(local.date(), w.starts_at, zone) > chosen
    ]
    if not planned:
        return ModeNow(config.call_mode, None)
    window = min(planned, key=lambda w: CAUTION.index(w.call_mode))
    return ModeNow(
        window.call_mode, datetime.combine(local.date(), window.ends_at, zone)
    )
