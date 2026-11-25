"""An index on when an address or name last failed to sign in (#888 S11).

Every sign-in attempt forgets the counters whose last failure is a day old.
Without an index that delete reads the whole table, which a spray of made-up
names fills with one row per name.

The downgrade drops the index; the code before this migration prunes the same
way, only slower.

Revision: op007
Revises: op006
Created: 2026-11-23
"""

from collections.abc import Sequence

from alembic import op

revision: str = "op007"
down_revision: str | None = "op006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX = "ix_operator_login_failures_last_failed_at"


def upgrade() -> None:
    op.create_index(INDEX, "operator_login_failures", ["last_failed_at"])


def downgrade() -> None:
    op.drop_index(INDEX, table_name="operator_login_failures")
