"""The self-test's call and card (#574, Maxi 15.11.2026).

`calls.is_test` and `reservations.is_test` mark the rows of a self-test call
(`python -m sim.selftest`): the tablet shows its table as a card marked TEST,
and nothing reaches outside. No outbox event, no customer contact, no count in
the figures, the call log or the day report. The self-test removes its rows
again; the flag only keeps them apart while they live.

New columns only, false for every existing row. The downgrade drops them, and
refuses while a test call is left: it would then look like a real one. On a database a
restaurant signs in to, take a backup first with `bash scripts/backup.sh
--label before_migration_039` (CLAUDE.md §8, docs/13 §4).

Revision: 039
Revises: 038
Created: 2026-11-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "039"
down_revision: str | None = "038"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("calls", "reservations")


def upgrade() -> None:
    for table in TABLES:
        op.add_column(
            table,
            sa.Column(
                "is_test", sa.Boolean(), nullable=False, server_default=sa.false()
            ),
        )


def downgrade() -> None:
    # Without the flag a left test call and its card would pass for real ones.
    left = sum(
        op.get_bind().scalar(sa.text(f"SELECT count(*) FROM {table} WHERE is_test"))
        for table in TABLES
    )
    if left:
        raise RuntimeError(
            f"{left} self-test row(s) left in calls or reservations: remove them first "
            "(python -m sim.selftest --card-seconds 0 deletes them)"
        )
    for table in TABLES:
        op.drop_column(table, "is_test")
