"""Weekly windows of the call mode and the team's last tap on the switch (T-8.1, docs/02 §4).

`call_mode_windows` holds weekly windows with a mode of their own, say the
agent answers on Friday evenings while the restaurant listens in by day. Only
`shadow`, `overflow` and `primary`: the emergency stop is a button, never a
plan. `service_config.call_mode_chosen_at` is when the team last chose a mode
on the tablet: a window that began after it holds, the tap holds against one
that began before (`domain/status/mode.py`). No window is written here, and no
tap is backfilled: one nobody made would beat every window.

On a database that holds a restaurant's data: backup first, with
`bash scripts/backup.sh --label before_migration_013` (CLAUDE.md §8, docs/13
§4). The downgrade drops the table and the column, and with them every window
the restaurant has set; `service_config.call_mode` stays, so the mode the team
chose last holds at every hour. That backup is the way back to the windows.

Revision: 013
Revises: 012
Created: 2026-10-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "013"
down_revision: str | None = "012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "service_config",
        sa.Column("call_mode_chosen_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "call_mode_windows",
        sa.Column("weekday", sa.SmallInteger(), nullable=False),
        sa.Column("starts_at", sa.Time(), nullable=False),
        sa.Column("ends_at", sa.Time(), nullable=False),
        sa.Column("call_mode", sa.Text(), nullable=False),
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
            "weekday BETWEEN 0 AND 6", name="ck_call_mode_windows_weekday"
        ),
        sa.CheckConstraint("starts_at < ends_at", name="ck_call_mode_windows_order"),
        sa.CheckConstraint(
            "call_mode IN ('shadow', 'overflow', 'primary')",
            name="ck_call_mode_windows_call_mode",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_call_mode_windows_tenant_id"),
        "call_mode_windows",
        ["tenant_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_call_mode_windows_tenant_id"), table_name="call_mode_windows"
    )
    op.drop_table("call_mode_windows")
    op.drop_column("service_config", "call_mode_chosen_at")
