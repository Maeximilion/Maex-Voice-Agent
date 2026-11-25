"""The recordings of calls: one row per recorded call (T-11.3, legal check L2).

A call is recorded only after the caller's explicit "ja" (`consent_log`), and the
audio lies in a folder outside the database (`domain/calls/recordings.py`). The
row names the file, so the daily job finds it: 30 days after `started_at`, or at
once after a withdrawal (`withdrawn_at`), file and row go for good. Nothing is
recorded while `RECORDING_ENABLED` is off, so the table stays empty until then.

A new table, nothing existing changes. The downgrade drops it with its rows, and
the files they name stay in the folder without a way to find them: on a database
that holds a restaurant's data, delete the files first and take a backup with
`bash scripts/backup.sh --label before_migration_022` (CLAUDE.md §8, docs/13 §4).

Revision: 022
Revises: 020
Created: 2026-11-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "022"
down_revision: str | None = "020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "recordings",
        sa.Column("call_id", sa.UUID(), nullable=False),
        sa.Column("file_name", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("withdrawn_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.ForeignKeyConstraint(["call_id"], ["calls.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("call_id", name="uq_recordings_call_id"),
    )
    op.create_index(
        op.f("ix_recordings_tenant_id"), "recordings", ["tenant_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_recordings_tenant_id"), table_name="recordings")
    op.drop_table("recordings")
