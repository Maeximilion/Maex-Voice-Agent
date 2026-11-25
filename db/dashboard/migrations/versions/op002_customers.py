"""The customers the dashboard looks after: where each stack's ops API answers
and the token it takes (#571, docs/23 §7 and §8).

One row per customer stack, written by `python -m api.dashboard.cli customer-add`
on the operator host. The token is stored readable, because the dashboard sends
it; the operator database and its encrypted backups stay on the operator host
(T7). The downgrade drops the table: the dashboard then shows no customer until
each is added again, and no customer's stack is touched.

Revision: op002
Revises: op001
Created: 2026-11-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "op002"
down_revision: str | None = "op001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "operator_customers",
        sa.Column("key", sa.Text(), primary_key=True),
        sa.Column("api_url", sa.Text(), nullable=False, unique=True),
        sa.Column("ops_token", sa.Text(), nullable=False, unique=True),
        sa.Column(
            "added_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        # api/models/tenants.py CUSTOMER_KEY.
        sa.CheckConstraint(
            "key ~ '^[a-z][a-z0-9]*(-[a-z0-9]+)*$'", name="ck_operator_customer_key"
        ),
    )


def downgrade() -> None:
    op.drop_table("operator_customers")
