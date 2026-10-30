"""Holiday suggestions (T-9.5): every tenant that named its state
(`service_config.holiday_region`) gets the statutory holidays of the next weeks
as suggestions on the tablet (`domain/status/holidays.py`).

`cold_path.py` runs a pass every INTERVAL_SECONDS; a pass that finds every date
written writes nothing. `python -m api.jobs.holidays` runs one pass by hand.
"""

import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.config import settings
from api.core.logging import configure_logging, error_text, get_logger, log
from api.core.time import to_local, utcnow
from api.db import SessionLocal
from api.domain.status.holidays import suggest
from api.models import ServiceConfig, Tenant

logger = get_logger("api.jobs.holidays")

# A holiday is suggested weeks ahead; a pass every six hours is plenty.
INTERVAL_SECONDS = 6 * 3600.0


def suggest_all(session: Session, now: datetime) -> int:
    """One pass over every tenant with a region. Commits; returns how many
    suggestions it wrote. Each tenant runs in a savepoint of its own: one that
    fails is logged and rolled back, the others still get theirs."""
    written = 0
    tenants = session.execute(
        select(Tenant, ServiceConfig.holiday_region)
        .join(ServiceConfig, ServiceConfig.tenant_id == Tenant.id)
        .order_by(Tenant.id)
    ).all()
    for tenant, region in tenants:
        if region is None:
            continue
        try:
            with session.begin_nested():
                today = to_local(now, tenant.timezone).date()
                count = suggest(session, tenant.id, region, today)
            written += count
        except Exception as exc:  # noqa: BLE001 - one tenant must not stop the rest
            log(
                logger,
                logging.ERROR,
                "Holiday suggestions failed for a tenant",
                tenant_id=str(tenant.id),
                error=error_text(exc),
            )
    session.commit()
    if written:
        log(logger, logging.INFO, "Holidays suggested", count=written)
    return written


if __name__ == "__main__":
    configure_logging(settings.log_level)
    with SessionLocal() as session:
        suggest_all(session, utcnow())
