"""The print bridge's sign of life (T-8.3).

The bridge polls for slips, the server cannot reach it. A bridge or a line in
the restaurant that failed while no order waited was noticed by nobody until
the next order (open point of PR #182, docs/13 §0a). `bridge_heartbeats` keeps
the time of its last poll, one row per tenant, read by the operator's alarm and
the tablet's header (`domain/status/bridge.py`).

A table of its own, not a field of `service_config`: the poll comes every 2 s,
the header's token reads that row's `updated_at`, and `confirm` holds a lock on
it. No personal data. Nothing is backfilled: until its first poll a bridge that
is set up counts as never seen.

On a database that holds a restaurant's data: backup first, with
`bash scripts/backup.sh --label before_migration_015` (CLAUDE.md §8, docs/13
§4). The downgrade drops the table; the next poll writes the row again.

Revision: 015
Revises: 014
Created: 2026-10-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "015"
down_revision: str | None = "014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "bridge_heartbeats",
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("tenant_id"),
    )


def downgrade() -> None:
    op.drop_table("bridge_heartbeats")
