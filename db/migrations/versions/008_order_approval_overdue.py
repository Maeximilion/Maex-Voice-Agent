"""How long an order may wait for the team's approval before its card turns red (#252).

Since the order limit of migration 007 an order can wait for "Passt" on the
tablet in every mode, and an order nobody approves is cooked by nobody. Maxi,
13.10.2026: after 5 minutes without "Passt" the card turns red. The number is
the restaurant's rule, so it is a field of `service_config` and no constant in
the code (CLAUDE.md §2 rule 1): `order_approval_overdue_minutes`, at least 1.

Read by the order board of the tablet (`domain/ordering/board.py`). Nothing
else changes: the order stays confirmed and waiting, only the card says so.

On a database that holds a restaurant's data: backup first, with
`bash scripts/backup.sh --label before_migration_008` (CLAUDE.md §8, docs/13
§4). The upgrade writes the default into every existing `service_config` row;
the downgrade drops the column, and with it the value the restaurant has set
there since. That backup is the way back to that value.

Revision: 008
Revises: 007
Created: 2026-10-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "008"
down_revision: str | None = "007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COLUMN = "order_approval_overdue_minutes"
CHECK = f"ck_service_config_{COLUMN}"


def upgrade() -> None:
    op.add_column(
        "service_config",
        sa.Column(COLUMN, sa.Integer(), nullable=False, server_default="5"),
    )
    op.create_check_constraint(CHECK, "service_config", f"{COLUMN} >= 1")


def downgrade() -> None:
    op.drop_constraint(CHECK, "service_config")
    op.drop_column("service_config", COLUMN)
