"""The owner's admin PIN (#570 part b, D34 points 3 and 8, docs/23 §6).

`gui_accounts` gets the admin PIN, readable for the operator like the team PIN
(migration 031), and a failure count of its own with the waits of a wrong
password. It must differ from the team PIN, or the staff would know it.
`gui_sessions` gets `admin_until`: the owner's rights on that device end there,
60 minutes after the admin PIN, or earlier with the idle lock.

New columns only; the upgrade changes no row. The downgrade drops them: the
admin PINs are gone and every device has staff rights only, so the operator
sets the PIN again after the next upgrade. On a database a restaurant signs in
to, take a backup first with `bash scripts/backup.sh --label
before_migration_032` (CLAUDE.md §8, docs/13 §4).

Revision: 032
Revises: 031
Created: 2026-11-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "032"
down_revision: str | None = "031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.add_column("gui_accounts", sa.Column("admin_pin", sa.Text(), nullable=True))
    op.add_column(
        "gui_accounts",
        sa.Column("admin_failures", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column("gui_accounts", sa.Column("admin_last_failed_at", TZ, nullable=True))
    op.add_column("gui_accounts", sa.Column("admin_blocked_until", TZ, nullable=True))
    op.create_check_constraint(
        "ck_gui_accounts_admin_pin", "gui_accounts", "admin_pin ~ '^[0-9]{4,6}$'"
    )
    # NULL on either side passes: an account without one of the PINs is fine.
    op.create_check_constraint(
        "ck_gui_accounts_admin_pin_not_team_pin",
        "gui_accounts",
        "admin_pin <> team_pin",
    )
    op.add_column("gui_sessions", sa.Column("admin_until", TZ, nullable=True))


def downgrade() -> None:
    op.drop_column("gui_sessions", "admin_until")
    op.drop_constraint(
        "ck_gui_accounts_admin_pin_not_team_pin", "gui_accounts", type_="check"
    )
    op.drop_constraint("ck_gui_accounts_admin_pin", "gui_accounts", type_="check")
    op.drop_column("gui_accounts", "admin_blocked_until")
    op.drop_column("gui_accounts", "admin_last_failed_at")
    op.drop_column("gui_accounts", "admin_failures")
    op.drop_column("gui_accounts", "admin_pin")
