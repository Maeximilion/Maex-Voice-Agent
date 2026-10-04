"""Lunch menus: `menu_items.lunch_only` and the lunch window `lunch_hours` (T-4.13).

A lunch menu (register groups 100 to 102, numbers like `M4A`, `VM5C`) is only
sold inside the lunch window. Which dishes are lunch menus is a flag on the
dish, set by the import; when lunch is, is one row per weekday in local time.
A weekday without a row has no lunch menus, so after this migration no lunch
menu is sold by phone until someone enters the window (CLAUDE.md §2 rule 1:
never a time from the code).

Revision: 006
Predecessor: 005
Created: 2026-10-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "006"
down_revision: str | None = "005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "menu_items",
        sa.Column(
            "lunch_only", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
    )
    op.create_table(
        "lunch_hours",
        sa.Column("weekday", sa.SmallInteger(), nullable=False),
        sa.Column("starts_at", sa.Time(), nullable=False),
        sa.Column("ends_at", sa.Time(), nullable=False),
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
        sa.CheckConstraint("weekday BETWEEN 0 AND 6", name="ck_lunch_hours_weekday"),
        sa.CheckConstraint("starts_at < ends_at", name="ck_lunch_hours_window"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "weekday", name="uq_lunch_hours_tenant_weekday"
        ),
    )
    op.create_index(
        op.f("ix_lunch_hours_tenant_id"), "lunch_hours", ["tenant_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_lunch_hours_tenant_id"), table_name="lunch_hours")
    op.drop_table("lunch_hours")
    op.drop_column("menu_items", "lunch_only")
