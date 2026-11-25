"""The limit of concurrent AI calls (D16, T-10.9, docs/20 §8, docs/18 §7).

`service_config.max_concurrent_calls` is how many calls the agent takes at
once (default 1: the test phase runs with one, the router path allows two and
the team needs its lines). `call_limit_windows` holds weekly windows with a
limit of their own, for D16's "Friday to Sunday evenings up to 3" once the SIP
trunk allows it (T-10.12); no window is written here. A call above the limit
rings the team (`domain/calls/routing.py`, `telephony/handler.py`).

On a database that holds a restaurant's data: backup first, with
`bash scripts/backup.sh --label before_migration_011` (CLAUDE.md §8, docs/13
§4). The upgrade writes the default into every existing `service_config` row;
the downgrade drops the column and the table, and with them every limit the
restaurant has set since. That backup is the way back to them.

Revision: 011
Revises: 010
Created: 2026-10-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "011"
down_revision: str | None = "010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "service_config",
        sa.Column(
            "max_concurrent_calls", sa.Integer(), nullable=False, server_default="1"
        ),
    )
    op.create_check_constraint(
        "ck_service_config_max_concurrent_calls",
        "service_config",
        "max_concurrent_calls >= 1",
    )
    op.create_table(
        "call_limit_windows",
        sa.Column("weekday", sa.SmallInteger(), nullable=False),
        sa.Column("starts_at", sa.Time(), nullable=False),
        sa.Column("ends_at", sa.Time(), nullable=False),
        sa.Column("max_calls", sa.Integer(), nullable=False),
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
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
            "weekday BETWEEN 0 AND 6", name="ck_call_limit_windows_weekday"
        ),
        sa.CheckConstraint("starts_at < ends_at", name="ck_call_limit_windows_order"),
        sa.CheckConstraint("max_calls >= 1", name="ck_call_limit_windows_max_calls"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_call_limit_windows_tenant_id"),
        "call_limit_windows",
        ["tenant_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_call_limit_windows_tenant_id"), table_name="call_limit_windows"
    )
    op.drop_table("call_limit_windows")
    op.drop_constraint("ck_service_config_max_concurrent_calls", "service_config")
    op.drop_column("service_config", "max_concurrent_calls")
