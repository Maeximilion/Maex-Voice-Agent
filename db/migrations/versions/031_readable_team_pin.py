"""The team PIN readable for the operator (Maxi 09.11.2026, #570, D34).

The operator sees and changes the restaurant's team PIN (`python -m
api.jobs.gui_access pin`, later the dashboard of #571), so `gui_accounts` keeps
it as text instead of a scrypt hash. A hash of 4 to 6 digits held nobody with
the database back anyway: a million guesses are an afternoon offline.

A stored hash cannot become the PIN: the upgrade empties every team PIN, and
the operator sets it again with `gui_access pin --set <digits>` or a new setup
link. Until then a locked device signs in again with the password. The
downgrade drops the readable PINs the same way. Both directions delete data,
so on a database a restaurant signs in to take a backup first with
`bash scripts/backup.sh --label before_migration_031` (CLAUDE.md §8, docs/13
§4); restoring it brings the hashes back, setting a new PIN does not.

Revision: 031
Revises: 030
Created: 2026-11-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "031"
down_revision: str | None = "030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_column("gui_accounts", "team_pin_hash")
    op.add_column("gui_accounts", sa.Column("team_pin", sa.Text(), nullable=True))
    op.create_check_constraint(
        "ck_gui_accounts_team_pin", "gui_accounts", "team_pin ~ '^[0-9]{4,6}$'"
    )


def downgrade() -> None:
    op.drop_constraint("ck_gui_accounts_team_pin", "gui_accounts", type_="check")
    op.drop_column("gui_accounts", "team_pin")
    op.add_column("gui_accounts", sa.Column("team_pin_hash", sa.Text(), nullable=True))
