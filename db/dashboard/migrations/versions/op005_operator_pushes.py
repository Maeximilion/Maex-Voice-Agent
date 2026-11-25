"""The operator's pushes wait in an outbox of their own (#827 F3, docs/23 §5).

A changed factor used to be pushed inside the request, and a push server that
was down or not set only left a log line. `operator_pushes` holds each push
until the push server took it: the request writes the row, the dashboard's
sender posts it and retries with growing waits, never giving up, as a
customer's outbox is emptied by its dispatcher.

The downgrade drops the table with the pushes still waiting in it; the code
before this migration sends in the request again.

Revision: op005
Revises: op004
Created: 2026-11-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "op005"
down_revision: str | None = "op004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "operator_pushes",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_operator_pushes_next_attempt_at", "operator_pushes", ["next_attempt_at"]
    )


def downgrade() -> None:
    op.drop_table("operator_pushes")
