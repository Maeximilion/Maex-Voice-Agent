"""Orders and reservations note whether they were booked on their own line (#523).

A data subject request opens a booking only when its number is the line its call
came from (security audit 02.11.2026). The caller ID of the call goes after 90
days, with an objection and with an erasure, the booking keeps its name and number
longer: read live, the guest could no longer be verified by it. So the draft
notes it once, in `booked_on_own_line`.

The upgrade fills the column from the caller IDs that are still there, by the
rule of the draft: the booking's number is the caller ID in E.164
(`number_or_none`, as `customers.lookup.line_of`); a row whose caller ID is
already gone stays false and opens nothing, as before. The downgrade drops the column,
and a second upgrade can no longer read back what a cleared caller ID said: on a
database that holds a restaurant's data, take a backup first with
`bash scripts/backup.sh --label before_migration_027` (CLAUDE.md §8, docs/13 §4).

Revision: 027
Revises: 026
Created: 2026-11-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# The same rule as the draft: a copy could drift from it.
from api.domain.customers.phone import number_or_none

revision: str = "027"
down_revision: str | None = "026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("orders", "reservations")


def upgrade() -> None:
    bind = op.get_bind()
    for table in TABLES:
        op.add_column(
            table,
            sa.Column(
                "booked_on_own_line",
                sa.Boolean(),
                nullable=False,
                server_default="false",
            ),
        )
        rows = bind.execute(
            sa.text(
                f"SELECT b.id, b.phone, c.caller_id FROM {table} b "
                "JOIN calls c ON c.id = b.call_id AND c.tenant_id = b.tenant_id "
                "WHERE c.caller_id IS NOT NULL"
            )
        ).all()
        own = [
            {"id": row.id} for row in rows if row.phone == number_or_none(row.caller_id)
        ]
        if own:
            bind.execute(
                sa.text(f"UPDATE {table} SET booked_on_own_line = true WHERE id = :id"),
                own,
            )


def downgrade() -> None:
    for table in reversed(TABLES):
        op.drop_column(table, "booked_on_own_line")
