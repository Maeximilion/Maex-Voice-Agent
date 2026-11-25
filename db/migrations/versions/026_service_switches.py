"""Switches for pickup and reservations, next to the one for delivery (T-3.10, #495).

Both start on, so nothing changes until the team taps. The downgrade drops them:
a service that was switched off is on again, which changes what the agent offers,
so on a database that holds a restaurant's data, take a backup first with
`bash scripts/backup.sh --label before_migration_026` (CLAUDE.md §8, docs/13 §4).

Revision: 026
Revises: 025
Created: 2026-11-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "026"
down_revision: str | None = "025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for column in ("pickup_enabled", "reservations_enabled"):
        op.add_column(
            "service_config",
            sa.Column(column, sa.Boolean(), nullable=False, server_default="true"),
        )


def downgrade() -> None:
    for column in ("reservations_enabled", "pickup_enabled"):
        op.drop_column("service_config", column)
