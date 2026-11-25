"""One counting row per date in `special_days` (#441).

Two `set` rows for one tenant and date made the hours and the seats depend on
row order: `domain/status/hours.py` keeps the row read last, the seats in
`domain/reservations/capacity.py` the row read first. `uq_special_days_set`
allows one `set` row per tenant and date; suggestions and dismissals keep their
own key (`uq_special_days_suggestion`, migration 015).

On a database that holds a restaurant's data: backup first, with
`bash scripts/backup.sh --label before_migration_017` (CLAUDE.md §8, docs/13
§4). Where a date already has two `set` rows the upgrade stops before it
changes anything and names them: which one holds is the team's decision, not
the migration's. The downgrade drops the key.

Revision: 017
Revises: 016
Created: 2026-10-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "017"
down_revision: str | None = "016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    twice = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT tenant_id, date, count(*), "
                "string_agg(id::text || ' (' || coalesce(note, '') || ')', ', ' "
                "ORDER BY created_at) "
                "FROM special_days WHERE status = 'set' "
                "GROUP BY tenant_id, date HAVING count(*) > 1 ORDER BY tenant_id, date"
            )
        )
        .all()
    )
    if twice:
        found = "\n".join(
            f"  tenant {tenant} on {day}: {n} rows {rows}"
            for tenant, day, n, rows in twice
        )
        raise RuntimeError(
            "Migration 017 stopped, nothing changed: these dates have more than "
            "one special day with status 'set', and only one may count:\n"
            f"{found}\n"
            "Back up first (bash scripts/backup.sh --label before_migration_017), "
            "keep the row that should hold on each date, delete the others, then "
            "run the migration again."
        )
    op.create_index(
        "uq_special_days_set",
        "special_days",
        ["tenant_id", "date"],
        unique=True,
        postgresql_where=sa.text("status = 'set'"),
    )


def downgrade() -> None:
    op.drop_index("uq_special_days_set", table_name="special_days")
