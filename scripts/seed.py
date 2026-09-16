"""Testkonfiguration für einen Mandanten anlegen. Aufruf: python -m scripts.seed [--tenant-name N]

Idempotent: zweimal ausführen ergibt denselben Zustand. Mandant und service_config
werden nur angelegt, wenn sie fehlen (der Live-Schalter gehört dem Team, nicht dem
Skript); Öffnungszeiten und Kapazität werden deterministisch ersetzt.

Die Werte sind Platzhalter, bis die Ist-Aufnahme (C1) die echten liefert. Im Betrieb
kommen Öffnungszeiten und Kapazität ausschließlich aus der Datenbank (CLAUDE.md §2).
"""

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from datetime import time

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from api.config import settings
from api.db import SessionLocal
from api.models import Capacity, OpeningHours, ServiceConfig, Tenant
from api.models.tenants import SERVICES

CLOSED_WEEKDAYS = {0}  # Montag Ruhetag
OPENING_WINDOWS = ((time(11, 30), time(14, 0)), (time(17, 0), time(22, 0)))
CAPACITY_WINDOWS = ((time(11, 30), time(14, 0), 30), (time(17, 0), time(22, 0), 40))
SLOT_MINUTES = 30


@dataclass
class SeedResult:
    tenant_id: str
    tenant_created: bool
    service_config_created: bool
    opening_hours: int
    capacity: int


def seed(session: Session, tenant_name: str, timezone: str) -> SeedResult:
    tenant = session.scalar(select(Tenant).where(Tenant.name == tenant_name))
    tenant_created = tenant is None
    if tenant is None:
        tenant = Tenant(name=tenant_name, timezone=timezone)
        session.add(tenant)
        session.flush()

    config_created = session.get(ServiceConfig, tenant.id) is None
    if config_created:
        session.add(
            ServiceConfig(
                tenant_id=tenant.id,
                call_mode="shadow",
                team_phone=settings.team_phone,
                max_call_seconds=settings.max_call_seconds,
            )
        )

    session.execute(delete(OpeningHours).where(OpeningHours.tenant_id == tenant.id))
    session.execute(delete(Capacity).where(Capacity.tenant_id == tenant.id))
    open_days = [d for d in range(7) if d not in CLOSED_WEEKDAYS]
    hours = [
        OpeningHours(tenant_id=tenant.id, weekday=d, opens_at=o, closes_at=c, service=s)
        for d in open_days
        for s in SERVICES
        for o, c in OPENING_WINDOWS
    ]
    capacity = [
        Capacity(
            tenant_id=tenant.id,
            weekday=d,
            slot_start=start,
            slot_end=end,
            max_guests=guests,
            slot_minutes=SLOT_MINUTES,
        )
        for d in open_days
        for start, end, guests in CAPACITY_WINDOWS
    ]
    session.add_all(hours + capacity)
    session.commit()

    return SeedResult(
        tenant_id=str(tenant.id),
        tenant_created=tenant_created,
        service_config_created=config_created,
        opening_hours=len(hours),
        capacity=len(capacity),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Testkonfiguration für einen Mandanten anlegen"
    )
    parser.add_argument("--tenant-name", default=settings.tenant_name)
    parser.add_argument("--timezone", default=settings.tenant_timezone)
    args = parser.parse_args(argv)

    with SessionLocal() as session:
        result = seed(session, tenant_name=args.tenant_name, timezone=args.timezone)
    print(json.dumps(asdict(result)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
