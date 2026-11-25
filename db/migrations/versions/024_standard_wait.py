"""The standard wait, and a wait for today that ends by itself (T-3.14, #505).

Until now one value per service held both: the wait of a rush stayed, and the
guests of the next day heard it. The two columns become the standard and keep
their value, so what a restaurant set stays what it gets. A wait for today gets
columns of its own with the moment it ends, the end of the business day: reading
it past that moment gives the standard. Nothing has to run at 05:00, so nothing
can miss it, the same way "Gericht aus" ends by itself.

The downgrade puts the old names back and drops the waits for today: a rush value
is lost and the standard holds, which is what the old code would then read. Both
directions change what the agent tells guests, so on a database that holds a
restaurant's data, take a backup first with
`bash scripts/backup.sh --label before_migration_024` (CLAUDE.md §8, docs/13 §4).

Revision: 024
Revises: 023
Created: 2026-11-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "024"
down_revision: str | None = "023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SERVICES = ("pickup", "delivery")


def upgrade() -> None:
    for service in SERVICES:
        op.alter_column(
            "service_config",
            f"{service}_wait_minutes",
            new_column_name=f"{service}_wait_standard_minutes",
        )
        op.add_column(
            "service_config",
            sa.Column(f"{service}_wait_today_minutes", sa.Integer(), nullable=True),
        )
        op.add_column(
            "service_config",
            sa.Column(
                f"{service}_wait_today_until", sa.DateTime(timezone=True), nullable=True
            ),
        )
        op.create_check_constraint(
            f"ck_service_config_{service}_wait_today",
            "service_config",
            f"({service}_wait_today_minutes IS NULL) = "
            f"({service}_wait_today_until IS NULL)",
        )


def downgrade() -> None:
    for service in SERVICES:
        op.drop_constraint(
            f"ck_service_config_{service}_wait_today", "service_config", type_="check"
        )
        op.drop_column("service_config", f"{service}_wait_today_until")
        op.drop_column("service_config", f"{service}_wait_today_minutes")
        op.alter_column(
            "service_config",
            f"{service}_wait_standard_minutes",
            new_column_name=f"{service}_wait_minutes",
        )
