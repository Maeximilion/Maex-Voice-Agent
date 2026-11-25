"""The request log of data subjects (T-11.8, legal check U2, docs/09 §2).

One row per request: access, erasure or objection, its deadline, the verification
details as the guest gave them, the answer. It proves the one-month deadline
(Art. 12(3)), so its `reference`, dates and `outcome` are not free text: only
`verification_note` holds personal data, and the deletion job strips it three
months after the answer and deletes the row after `delete_after`.

Nothing is backfilled. The lines of the interim file outside the repository are
moved in by hand with `python -m api.jobs.data_requests log` (docs/09 §2).

On a database that holds a restaurant's data: backup first, with
`bash scripts/backup.sh --label before_migration_019` (CLAUDE.md §8, docs/13
§4). The downgrade drops the table with every logged request.

Revision: 019
Revises: 018
Created: 2026-11-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "019"
down_revision: str | None = "018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "data_subject_requests",
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("reference", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deadline", sa.Date(), nullable=False),
        sa.Column("verification_note", sa.Text(), nullable=True),
        sa.Column("answered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("outcome", sa.Text(), nullable=True),
        sa.Column("delete_after", sa.Date(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "reference ~ '^dsr_[0-9]{4}_[0-9]{3,}$'", name="ck_dsr_reference"
        ),
        sa.CheckConstraint(
            "kind IN ('access', 'erasure', 'objection')", name="ck_dsr_kind"
        ),
        sa.CheckConstraint(
            "channel IN ('phone', 'mail', 'in_person')", name="ck_dsr_channel"
        ),
        sa.CheckConstraint(
            "outcome IN ('done', 'not_verified', 'refused', 'withdrawn')",
            name="ck_dsr_outcome",
        ),
        sa.CheckConstraint(
            "(answered_at IS NULL) = (outcome IS NULL)"
            " AND (answered_at IS NULL) = (delete_after IS NULL)",
            name="ck_dsr_answered",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "reference", name="uq_dsr_reference"),
    )
    op.create_index(
        "ix_data_subject_requests_tenant_id",
        "data_subject_requests",
        ["tenant_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_table("data_subject_requests")
