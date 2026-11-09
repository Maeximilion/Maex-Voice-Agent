"""A stack's state at a glance, for the operator dashboard (#571, docs/23 §8).

Counts, times and codes only, never guest data: the ops API hands this out
without a reason recorded, so nothing here may name a guest, a number or an
order's content. Read by `GET /v1/ops/status` (api/ops/router.py).
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session

from api.config import settings
from api.core.time import business_day
from api.domain.calls.figures import Figures, span_figures
from api.domain.menu.price_check import open_differences
from api.domain.status import bridge
from api.domain.status.mode import mode_at
from api.models import (
    AuditLog,
    BridgeHeartbeat,
    Call,
    ItemAllergen,
    MenuItem,
    ServiceConfig,
    Tenant,
)

# Written by the monitor for each push to the operator (jobs/monitor.py).
ALARM_ACTION = "monitor.alarm"
# The monitor logs its daily report under the same action; it is no alarm.
DAY_REPORT = "day_report"
# Written by scripts/backup.sh after a good run.
BACKUP_DONE = "backup.done"
# An alarm pushed within this span counts as open: the monitor repeats one that
# still holds every clock hour or every day.
ALARMS_OPEN_FOR = timedelta(hours=24)


@dataclass(frozen=True)
class MenuStatus:
    items: int
    # Register prices the team has not answered (T-4.9).
    price_differences: int
    # Active dishes without one confirmed allergen: the agent says "no
    # information" for them (CLAUDE.md §9).
    without_allergen_information: int


@dataclass(frozen=True)
class BridgeStatus:
    configured: bool
    last_poll_at: datetime | None
    silent: bool


@dataclass(frozen=True)
class Alarm:
    kind: str
    last_at: datetime
    count: int


@dataclass(frozen=True)
class Status:
    tenant_key: str | None
    tenant_name: str
    # None while the tenant has no service_config (not provisioned).
    call_mode: str | None
    last_call_at: datetime | None
    business_day: date
    today: Figures
    print_bridge: BridgeStatus
    backup_at: datetime | None
    alarms: list[Alarm]
    menu: MenuStatus


def status(session: Session, tenant: Tenant, now: datetime) -> Status:
    config = session.get(ServiceConfig, tenant.id)
    day = business_day(now, tenant.timezone)
    return Status(
        tenant_key=tenant.customer_key,
        tenant_name=tenant.name,
        call_mode=mode_at(session, tenant, config, now).mode if config else None,
        last_call_at=session.scalar(
            select(func.max(Call.started_at)).where(Call.tenant_id == tenant.id)
        ),
        business_day=day,
        today=span_figures(session, tenant, day, day),
        print_bridge=_bridge(session, tenant, now),
        backup_at=session.scalar(
            select(func.max(AuditLog.at)).where(
                AuditLog.tenant_id == tenant.id, AuditLog.action == BACKUP_DONE
            )
        ),
        alarms=_alarms(session, tenant, now),
        menu=_menu(session, tenant),
    )


def _bridge(session: Session, tenant: Tenant, now: datetime) -> BridgeStatus:
    return BridgeStatus(
        configured=str(tenant.id) == settings.kitchen_bridge_tenant_id,
        last_poll_at=session.scalar(
            select(BridgeHeartbeat.seen_at).where(
                BridgeHeartbeat.tenant_id == tenant.id
            )
        ),
        silent=bridge.silence(session, tenant, now) is not None,
    )


def _alarms(session: Session, tenant: Tenant, now: datetime) -> list[Alarm]:
    kind = AuditLog.payload["kind"].astext
    rows = session.execute(
        select(kind, func.max(AuditLog.at), func.count())
        .where(
            AuditLog.tenant_id == tenant.id,
            AuditLog.action == ALARM_ACTION,
            AuditLog.at > now - ALARMS_OPEN_FOR,
            kind != DAY_REPORT,
        )
        .group_by(kind)
        .order_by(func.max(AuditLog.at).desc())
    ).all()
    return [Alarm(kind, last_at, count) for kind, last_at, count in rows]


def _menu(session: Session, tenant: Tenant) -> MenuStatus:
    active = (MenuItem.tenant_id == tenant.id, MenuItem.active.is_(True))
    items = session.scalar(select(func.count()).select_from(MenuItem).where(*active))
    unknown = session.scalar(
        select(func.count())
        .select_from(MenuItem)
        .where(
            *active,
            ~exists().where(ItemAllergen.menu_item_id == MenuItem.id),
        )
    )
    return MenuStatus(
        items=items or 0,
        price_differences=len(open_differences(session, tenant.id)),
        without_allergen_information=unknown or 0,
    )
