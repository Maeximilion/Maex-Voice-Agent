"""The client address of a sign-in challenge (#827 F1, docs/23 §5).

Anyone may ask for a sign-in challenge, and each one is a row for five
minutes. `client_address` lets the dashboard count the live ones of one
address and refuse more than a few, so one machine cannot fill the table;
nobody's challenge is removed for that. Challenges of a session (a
registration, a step-up) leave it empty.

The downgrade drops the column; the challenges themselves stay valid.

Revision: op004
Revises: op003
Created: 2026-11-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "op004"
down_revision: str | None = "op003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "operator_challenges", sa.Column("client_address", sa.Text(), nullable=True)
    )
    op.create_index(
        "ix_operator_challenges_client_address",
        "operator_challenges",
        ["client_address"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_operator_challenges_client_address", table_name="operator_challenges"
    )
    op.drop_column("operator_challenges", "client_address")
