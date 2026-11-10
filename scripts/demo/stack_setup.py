"""Start state of the README demo stack, made inside the api container.

    python -m scripts.demo.stack_setup

Run by `scripts/readme_demo.py` through `docker compose exec`. It seeds a restaurant,
opens it around the clock (hours and capacity still come from the database) and loads
the repository's evaluation menu from `evals/menu/`. No order, no reservation. Invented
data only.
"""

import uuid
from datetime import time
from pathlib import Path

from sqlalchemy import update

from api.db import SessionLocal
from api.domain.menu.importer import apply, parse
from api.models import Capacity, OpeningHours, ServiceConfig
from api.models.tenants import SERVICES
from scripts.import_menu import read_files
from scripts.seed import seed

TENANT = "Demo"
MENU_FOLDER = Path("evals/menu")


def main() -> None:
    with SessionLocal() as session:
        result = seed(session, tenant_name=TENANT, timezone="Europe/Berlin")
        tenant_id = uuid.UUID(result.tenant_id)
        for model in (OpeningHours, Capacity):
            session.query(model).filter(model.tenant_id == tenant_id).delete()
        session.add_all(
            [
                OpeningHours(
                    tenant_id=tenant_id,
                    weekday=d,
                    opens_at=time(0, 0),
                    closes_at=time(23, 59),
                    service=s,
                )
                for d in range(7)
                for s in SERVICES
            ]
            + [
                Capacity(
                    tenant_id=tenant_id,
                    weekday=d,
                    slot_start=time(0, 0),
                    slot_end=time(23, 59),
                    max_guests=200,
                    slot_minutes=30,
                )
                for d in range(7)
            ]
        )
        session.execute(
            update(ServiceConfig)
            .where(ServiceConfig.tenant_id == tenant_id)
            .values(call_mode="primary", delivery_enabled=True)
        )
        session.commit()
        plan = parse(read_files(MENU_FOLDER))
        if not plan.ok:
            raise SystemExit(f"menu does not parse: {plan.errors}")
        apply(session, tenant_id, plan)


if __name__ == "__main__":
    main()
