"""One open callback per call: a partial unique index (#981).

`create_callback` keeps a call to one open card under the call's advisory
lock (`api/domain/callbacks/create.py` `_lock_call`), and a model's retry gets
that card back (docs/04). The lock binds only the writers that take it; this
index is the barrier behind it. `create_callback` answers a conflict on it as
the same replay, and reopening a done card of a call that has another open one
is refused (`api/domain/callbacks/board.py` `reopen`), the one path that could
make a second open card before.

Old data: two open cards of one call may exist (a card reopened next to a
newer one). The upgrade keeps the oldest open, the card `create_callback`
reads, and closes the others as done by `migration:047`, at the time of the
migration; they stay in the history sheet. Like a team's "Erledigt", that
mark starts their deletion period again (`jobs/retention.py` counts from
`done_at`): kept visible for the team beats a shorter period for a duplicate.
The downgrade opens exactly those again. The table is locked against other
writers for the few statements, so no new card slips in between the cleanup
and the index. On a database a restaurant
signs in to, take a backup first with
`bash scripts/backup.sh --label before_migration_047` (CLAUDE.md §8, docs/13
§4).

Revision: 047
Revises: 046
Created: 2026-11-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "047"
down_revision: str | None = "046"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX = "uq_callbacks_open_per_call"
CLOSED_BY = "migration:047"
OPEN = "status = 'open' AND deleted_at IS NULL"


def upgrade() -> None:
    # No insert or update by anyone else until the index stands; SHARE ROW
    # EXCLUSIVE also excludes a second run of this upgrade, which SHARE did not.
    op.execute("LOCK TABLE callbacks IN SHARE ROW EXCLUSIVE MODE")
    op.execute(
        sa.text(
            f"""
            UPDATE callbacks SET status = 'done', done_by = :by, done_at = now()
            WHERE id IN (
                SELECT id FROM (
                    SELECT id, row_number() OVER (
                        PARTITION BY tenant_id, call_id ORDER BY created_at, id
                    ) AS place
                    FROM callbacks WHERE {OPEN}
                ) ranked
                WHERE place > 1
            )
            """
        ).bindparams(by=CLOSED_BY)
    )
    op.create_index(
        INDEX,
        "callbacks",
        ["tenant_id", "call_id"],
        unique=True,
        postgresql_where=sa.text(OPEN),
    )


def downgrade() -> None:
    op.drop_index(INDEX, table_name="callbacks")
    op.execute(
        sa.text(
            "UPDATE callbacks SET status = 'open', done_by = NULL, done_at = NULL "
            "WHERE status = 'done' AND done_by = :by"
        ).bindparams(by=CLOSED_BY)
    )
