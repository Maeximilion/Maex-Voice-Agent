"""Daily report to the team as an outbox event `daily.report` (docs/11 §jobs).

Its one report so far is L18 (docs/09 §1, T-11.6): how many orders of earlier
business days nobody has marked "In Kasse eingegeben". The register is the
only booking system with a TSE, so an order the agent took and nobody typed in
is a sale missing from the books. The tablet shows each of them as a red card
(domain/ordering/board.py); this push reaches the team when nobody looks.

The dispatcher process (`api/jobs/cold_path.py`) runs a pass every
INTERVAL_SECONDS. A pass sends one event per tenant and business day, from
REGISTER_REPORT_HOUR local time on, and none when every order is marked. A
day the process was down is not sent late: the next day's report counts every
older unmarked order anyway. A report the dispatcher gave up on (`failed`, n8n
down through all its attempts, api/events/outbox.py) is queued again after
RETRY_FAILED_AFTER, so an outage around the hour does not cost the day's push.
The payload holds counts and dates, no guest details.
"""

import logging
from datetime import datetime, time, timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from api.config import settings
from api.core.logging import error_text, get_logger, log
from api.core.time import business_day, business_day_bounds_utc, to_local
from api.domain.ordering.board import unmarked_before
from api.events import enqueue
from api.events.types import DAILY_REPORT
from api.models import OutboxEvent, Tenant

logger = get_logger("api.jobs.daily_report")

REGISTER_UNMARKED = "register_unmarked"
# How often the dispatcher process asks. A report goes out at most this late.
INTERVAL_SECONDS = 300.0
# A failed report is queued again this long after the last one: a long n8n
# outage adds one row an hour, not one every pass.
RETRY_FAILED_AFTER = timedelta(hours=1)


def report(session: Session, now: datetime) -> int:
    """One pass over every tenant. Commits; returns how many events it queued.

    Each tenant runs in a savepoint of its own: one that fails (a broken time
    zone, say) is logged and rolled back, the others still get their report.
    """
    queued = 0
    for tenant in session.scalars(select(Tenant).order_by(Tenant.id)).all():
        try:
            with session.begin_nested():
                count = _register_report(session, tenant, now)
            # Counted once the savepoint holds: its flush can still fail.
            queued += count
        except Exception as exc:  # noqa: BLE001 - one tenant must not stop the rest
            log(
                logger,
                logging.ERROR,
                "Daily report failed for a tenant",
                tenant_id=str(tenant.id),
                error=error_text(exc),
            )
    session.commit()
    return queued


def _register_report(session: Session, tenant: Tenant, now: datetime) -> int:
    if to_local(now, tenant.timezone).time() < time(settings.register_report_hour):
        return 0
    day = business_day(now, tenant.timezone)
    if _sent(session, tenant, day.isoformat(), now):
        return 0
    start, _ = business_day_bounds_utc(day, tenant.timezone)
    orders = unmarked_before(session, tenant.id, start)
    if not orders:
        return 0
    enqueue(
        session,
        tenant_id=tenant.id,
        event_type=DAILY_REPORT,
        payload={
            "report": REGISTER_UNMARKED,
            "business_day": day.isoformat(),
            "count": len(orders),
            "oldest_day": business_day(
                orders[0].created_at, tenant.timezone
            ).isoformat(),
        },
    )
    log(
        logger,
        logging.INFO,
        "Register report queued",
        tenant_id=str(tenant.id),
        count=len(orders),
    )
    return 1


def _sent(session: Session, tenant: Tenant, day: str, now: datetime) -> bool:
    """Whether this business day's report is in the outbox: pending or sent,
    or failed less than RETRY_FAILED_AFTER ago.

    The deletion job thins a sent payload only after RETENTION_OUTBOX_SENT_DAYS
    (at least one day), so today's row still names its report and day.
    """
    return (
        session.scalar(
            select(OutboxEvent.id)
            .where(
                OutboxEvent.tenant_id == tenant.id,
                OutboxEvent.event_type == DAILY_REPORT,
                OutboxEvent.payload["report"].astext == REGISTER_UNMARKED,
                OutboxEvent.payload["business_day"].astext == day,
                or_(
                    OutboxEvent.status != "failed",
                    OutboxEvent.created_at > now - RETRY_FAILED_AFTER,
                ),
            )
            .limit(1)
        )
        is not None
    )
