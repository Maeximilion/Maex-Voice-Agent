"""A new customer's tenant inside its own stack (#573, D32, docs/13 §8).

`create_tenant` writes the restaurant from its customer file
(`schemas/provision.py`) in one transaction: the tenant with its key, the
service config with the call mode `shadow`, the weekly hours, the seats and the
first setup link of its login. All of it or nothing. A tenant with the same
key is left as it is, so a rerun changes nothing and keeps what the team set
since. Any other tenant in the database refuses: one customer per stack, and
the operations view shows only the oldest tenant (`stack_tenant_id`).

`missing` says what a tenant still lacks before it takes calls.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import ColumnExpressionArgument, exists, select, text
from sqlalchemy.orm import Session

from api.config import TEAM_PHONE_PLACEHOLDER, settings
from api.core.errors import InvalidInput
from api.domain.access.setup import issue_link
from api.domain.customers.phone import normalize_phone
from api.domain.status.staff_changes import dialable_numbers
from api.models import (
    AuditLog,
    BridgeHeartbeat,
    Capacity,
    DeliveryZone,
    GuiAccount,
    MenuItem,
    OpeningHours,
    ServiceConfig,
    Tenant,
)
from api.schemas.provision import DAYS, CustomerSpec


@dataclass
class Provisioned:
    """What `create_tenant` did. The rows are the ones written now: none on a
    rerun or a dry run's rollback. The setup token is shown once."""

    tenant_id: uuid.UUID | None
    created: bool
    opening_hours: int = 0
    seats: int = 0
    setup_token: str | None = None
    link_expires_at: datetime | None = None


def create_tenant(
    session: Session,
    spec: CustomerSpec,
    actor: str,
    now: datetime,
    dry_run: bool = False,
) -> Provisioned:
    """Create the customer's tenant, or find it by its key. Raises InvalidInput.
    Commits; a dry run checks and counts everything, then rolls back and
    issues no link."""
    team_phone = _team_phone(spec.team_phone)
    # Two runs at once: the second waits here, then sees the first one's tenant.
    session.execute(text("LOCK TABLE tenants IN SHARE ROW EXCLUSIVE MODE"))
    tenants = session.scalars(select(Tenant).order_by(Tenant.created_at)).all()
    for tenant in tenants:
        if tenant.customer_key == spec.key:
            session.rollback()
            return Provisioned(tenant.id, created=False)
    if tenants:
        other = tenants[0]
        session.rollback()
        raise InvalidInput(
            f"this database already holds the tenant {other.name!r} "
            f"(key {other.customer_key or 'none'}); one customer per stack (D32)"
        )

    tenant = Tenant(name=spec.name, timezone=spec.timezone, customer_key=spec.key)
    session.add(tenant)
    session.flush()
    session.add(
        ServiceConfig(
            tenant_id=tenant.id,
            call_mode="shadow",
            team_phone=team_phone,
            max_call_seconds=settings.max_call_seconds,
            privacy_notice_address=spec.privacy_notice_address,
            holiday_region=spec.holiday_region,
        )
    )
    hours = [
        OpeningHours(
            tenant_id=tenant.id,
            weekday=DAYS.index(day),
            opens_at=window.opens,
            closes_at=window.closes,
            service=service,
        )
        for window in spec.hours
        for day in window.days
        for service in window.services
    ]
    seats = [
        Capacity(
            tenant_id=tenant.id,
            weekday=DAYS.index(day),
            slot_start=window.start,
            slot_end=window.end,
            max_guests=window.guests,
            slot_minutes=window.slot_minutes,
        )
        for window in spec.seats
        for day in window.days
    ]
    session.add_all([*hours, *seats])
    session.add(
        AuditLog(
            tenant_id=tenant.id,
            actor=actor,
            action="tenant.provisioned",
            entity="tenant",
            entity_id=tenant.id,
            payload={
                "customer_key": spec.key,
                "opening_hours": len(hours),
                "seats": len(seats),
            },
        )
    )
    session.flush()
    if dry_run:
        session.rollback()
        return Provisioned(None, True, len(hours), len(seats))
    # The one commit: the link is the last step, so a failure anywhere before
    # it, or in it, leaves no tenant behind.
    token, expires_at = issue_link(session, tenant.id, actor, now)
    return Provisioned(tenant.id, True, len(hours), len(seats), token, expires_at)


def _team_phone(given: str | None) -> str:
    """The team number of the new tenant: one the dialplan dials, since a
    transfer elsewhere would never ring (`dialable_numbers`, T-10.13)."""
    number = normalize_phone(given) if given else settings.team_phone.strip()
    if number not in dialable_numbers():
        raise InvalidInput(
            f"team phone {number or '(empty)'} is not TEAM_PHONE or on "
            "TRANSFER_TARGETS of this stack"
        )
    return number


def missing(session: Session, tenant_id: uuid.UUID) -> list[str]:
    """What the tenant still lacks before it takes calls, in the operator's
    words, with the command that fills it; empty when nothing is missing."""

    def some(model: Any, *where: ColumnExpressionArgument[bool]) -> bool:
        return bool(
            session.scalar(select(exists().where(model.tenant_id == tenant_id, *where)))
        )

    services = set(
        session.scalars(
            select(OpeningHours.service).where(OpeningHours.tenant_id == tenant_id)
        )
    )
    config = session.get(ServiceConfig, tenant_id)
    gaps = []
    if not services:
        gaps.append("opening hours")
    if "dinein" in services and not some(Capacity, Capacity.max_guests > 0):
        gaps.append("seats for reservations")
    if "delivery" in services and not some(DeliveryZone, DeliveryZone.active):
        gaps.append("delivery zones (python -m scripts.seed_zones)")
    if not some(MenuItem, MenuItem.active):
        gaps.append("menu (python -m scripts.import_menu)")
    if session.get(BridgeHeartbeat, tenant_id) is None:
        gaps.append("print bridge, never polled")
    if not some(GuiAccount, GuiAccount.password_hash.is_not(None)):
        gaps.append("login, setup link not used (python -m api.jobs.gui_access link)")
    if config is None or config.privacy_notice_address is None:
        gaps.append("privacy notice address (docs/09 L4)")
    if config is None or config.team_phone in ("", TEAM_PHONE_PLACEHOLDER):
        # Every transfer and fallback would ring a number nobody answers (#687).
        gaps.append("team number, still the placeholder (TEAM_PHONE in .env)")
    return gaps
