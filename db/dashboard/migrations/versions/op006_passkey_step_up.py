"""When a session last showed a passkey on the step-up (D37, #884, docs/23 §5).

Once an account has a passkey, a change of its factors needs a passkey
step-up of the last five minutes; the sign-in and a TOTP step-up no longer
count for it. `stepped_up_at` keeps saying when the session last showed any
second factor, for every other dangerous action.

The downgrade drops the column; the code before this migration takes any
fresh factor for a factor change again.

Revision: op006
Revises: op005
Created: 2026-11-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "op006"
down_revision: str | None = "op005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "operator_sessions",
        sa.Column("passkey_stepped_up_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("operator_sessions", "passkey_stepped_up_at")
