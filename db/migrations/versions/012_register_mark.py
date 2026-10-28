"""The mark that the team typed an order into the register (T-11.6, docs/09 §1 L18).

`orders.register_entered_at` is when somebody tapped "In Kasse eingegeben" on
the tablet. "Passt, Zettel an die Kasse" only approves the order and releases
the input slip (docs/02 §2a); the typing comes after it, so the register's
books (TSE) can only be checked against a mark of its own. A correction clears
the mark: the register holds the old state, the new one needs typing again.

No backfill: an order from before this migration was never marked, and calling
it typed in would be a claim nobody made. Before the first real order (L18)
there are only test orders; on the workbench they stand on the tablet as red
cards until somebody marks them or the database is seeded again.

On a database that holds a restaurant's data: backup first, with
`bash scripts/backup.sh --label before_migration_012` (CLAUDE.md §8, docs/13
§4). The downgrade drops the column and with it every mark; that backup is the
way back to them.

Revision: 012
Revises: 011
Created: 2026-10-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "012"
down_revision: str | None = "011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "orders",
        sa.Column("register_entered_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("orders", "register_entered_at")
