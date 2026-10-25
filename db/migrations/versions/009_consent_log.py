"""The consent log: one row per answer to a consent question of the agent (T-11.1).

A caller's own allergy is health data (Art. 9 GDPR). The legal check (L13,
decided by Maxi on 20.10.2026) lets the kitchen note keep its reason "Grund:
Allergie" only with the caller's explicit "ja" to the question "Darf ich die
Allergie als Hinweis für die Küche notieren?", and that consent is logged per
call: the call, when it was answered, the question word for word (its
version), and the answer, `yes`, `no` or `unclear`. The allergy itself is not
in the row. Written by the conversation core (`api/agent/loop.py`) through
`api/domain/calls/consent.py`.

A new table, nothing existing changes. The downgrade drops it with every logged
answer: on a database that holds a restaurant's data, backup first with
`bash scripts/backup.sh --label before_migration_009` (CLAUDE.md §8, docs/13
§4).

Revision: 009
Revises: 008
Created: 2026-10-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "009"
down_revision: str | None = "008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "consent_log",
        sa.Column("call_id", sa.UUID(), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("answer", sa.Text(), nullable=False),
        sa.Column("answered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.CheckConstraint(
            "answer IN ('yes', 'no', 'unclear')", name="ck_consent_log_answer"
        ),
        sa.ForeignKeyConstraint(["call_id"], ["calls.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_consent_log_tenant_id"), "consent_log", ["tenant_id"], unique=False
    )
    op.create_index(
        op.f("ix_consent_log_call_id"), "consent_log", ["call_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_consent_log_call_id"), table_name="consent_log")
    op.drop_index(op.f("ix_consent_log_tenant_id"), table_name="consent_log")
    op.drop_table("consent_log")
