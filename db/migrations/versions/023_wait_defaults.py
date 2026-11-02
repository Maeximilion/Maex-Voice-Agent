"""The default waits are 30 minutes for pickup and 60 for delivery (Maxi, 02.11.2026).

`service_config` holds the wait the agent names, and a restart keeps it. So the
default only decides the start of a new tenant, and a tenant still on the old
pair moves with it. A wait somebody changed on the tablet stays as it is.

The downgrade puts the old defaults back and moves only rows on the new pair: a
row that really stood on 30 and 60 before the upgrade cannot be told from one
that moved. No personal data is touched.

Revision: 023
Revises: 022
Created: 2026-11-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "023"
down_revision: str | None = "022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _set_defaults(pickup: int, delivery: int) -> None:
    op.alter_column("service_config", "pickup_wait_minutes", server_default=str(pickup))
    op.alter_column(
        "service_config", "delivery_wait_minutes", server_default=str(delivery)
    )


def _move(column: str, before: int, after: int) -> None:
    op.execute(
        sa.text(
            f"UPDATE service_config SET {column} = :after WHERE {column} = :before"
        ).bindparams(after=after, before=before)
    )


def upgrade() -> None:
    _set_defaults(30, 60)
    _move("pickup_wait_minutes", 20, 30)
    _move("delivery_wait_minutes", 45, 60)


def downgrade() -> None:
    _set_defaults(20, 45)
    _move("pickup_wait_minutes", 30, 20)
    _move("delivery_wait_minutes", 60, 45)
