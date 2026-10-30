"""The print bridge's sign of life (T-8.3, docs/02 §5, docs/13 §0a).

The server cannot reach the bridge, the bridge fetches its slips. So its poll
(`handover.claim_tickets`, every 2 s) is its heartbeat, and a bridge or a line
in the restaurant that fails while no order waits shows here, not with the
first slip that waits 60 s.

A bridge counts as silent after ALARM_BRIDGE_SILENT_MINUTES without a poll, and
only while pickup or delivery has been open that long: the register PC is off
at night, and only orders print. A tenant other than KITCHEN_BRIDGE_TENANT_ID has no bridge.
A bridge set up that never polled is silent too. Read by the operator's alarm
(jobs/monitor.py) and the tablet's header.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from api.config import settings
from api.core import time as clock
from api.domain.status.hours import load_hours, open_window_at
from api.models import BridgeHeartbeat, Tenant

# Far below the shortest silence that counts (1 minute): a poll every 2 s writes
# at most this often.
WRITE_EVERY_SECONDS = 30
ORDER_SERVICES = ("pickup", "delivery")


@dataclass(frozen=True)
class Silence:
    # The last poll; None for a bridge that never polled.
    since: datetime | None


def note_poll(session: Session, tenant_id: uuid.UUID, now: datetime) -> None:
    """Part of the caller's transaction."""
    statement = insert(BridgeHeartbeat).values(tenant_id=tenant_id, seen_at=now)
    session.execute(
        statement.on_conflict_do_update(
            index_elements=[BridgeHeartbeat.tenant_id],
            set_={"seen_at": statement.excluded.seen_at},
            where=BridgeHeartbeat.seen_at
            < now - timedelta(seconds=WRITE_EVERY_SECONDS),
        )
    )


def silence(
    session: Session, tenant: Tenant, now: datetime | None = None
) -> Silence | None:
    """The silence of this tenant's bridge, or None: it polls, or none is due."""
    if str(tenant.id) != settings.kitchen_bridge_tenant_id:
        return None
    now = now or clock.utcnow()
    seen = session.scalar(
        select(BridgeHeartbeat.seen_at).where(BridgeHeartbeat.tenant_id == tenant.id)
    )
    limit = timedelta(minutes=settings.alarm_bridge_silent_minutes)
    if seen is not None and now - seen < limit:
        return None
    zone = clock.tz(tenant.timezone)
    data = load_hours(session, tenant.id, now.astimezone(zone).date(), days=1)
    windows = [w for s in ORDER_SERVICES if (w := open_window_at(data, now, s, zone))]
    # Opening starts the clock too: last night's poll says nothing at 11:02, and
    # the register PC may come on a few minutes late without an alarm each day.
    if not windows or now - min(opens for opens, _ in windows) < limit:
        return None
    return Silence(seen)
