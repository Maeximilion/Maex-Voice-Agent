"""What the price check read from the register, on the dish (T-4.9).

The tablet cannot read the copy of the register files, so `scripts/price_check.py`
leaves what it found here and the team answers on the tablet (docs/06 §3). Neither
column is a price the agent says: `price_cents` stays the only one until somebody
takes the register's.

- `menu_items.register_price_cents`: the register's price where the last check
  found a different one, none otherwise.
- `menu_items.register_price_rejected`: the team rejected exactly that price. It
  stays out of sight until the register says another one.

On a database that holds a restaurant's data: backup first, with
`bash scripts/backup.sh --label before_migration_020` (CLAUDE.md §8, docs/13 §4).
The upgrade leaves every dish without a difference, so nothing changes. The
downgrade drops both columns with the open differences and the rejections; the
next check finds the differences again, a rejection has to be repeated, and
`audit_log` keeps it.

Revision: 020
Revises: 021 (021 reached main first, so 020 follows it)
Created: 2026-11-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "020"
down_revision: str | None = "021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("menu_items", sa.Column("register_price_cents", sa.Integer()))
    op.add_column(
        "menu_items",
        sa.Column(
            "register_price_rejected",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "ck_menu_items_register_price_cents",
        "menu_items",
        "register_price_cents >= 0",
    )
    op.create_check_constraint(
        "ck_menu_items_register_price_rejected",
        "menu_items",
        "NOT register_price_rejected OR register_price_cents IS NOT NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_menu_items_register_price_rejected", "menu_items", type_="check"
    )
    op.drop_constraint(
        "ck_menu_items_register_price_cents", "menu_items", type_="check"
    )
    op.drop_column("menu_items", "register_price_rejected")
    op.drop_column("menu_items", "register_price_cents")
