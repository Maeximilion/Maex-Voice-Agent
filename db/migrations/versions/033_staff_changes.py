"""Staff changes and their check cards (#570 part b, D34 point 4, docs/23 §6).

`service_config` gets a team number for today (`team_phone_today` with the
moment it ends, the end of the business day): the staff change the number
without the owner's PIN, and it returns to `team_phone` by itself. The new
table `staff_changes` holds the check cards: a staff change to the team
number or to a date's hours, with what held before and what was asked for,
until the owner keeps or undoes it with the admin PIN.

New columns and a new table; the upgrade changes no row. The downgrade drops
them: a team number set for today and every check card are gone, the hours a
staff change wrote stay in `special_days`. On a database a restaurant uses,
take a backup first with `bash scripts/backup.sh --label
before_migration_033` (CLAUDE.md §8, docs/13 §4).

Revision: 033
Revises: 032
Created: 2026-11-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "033"
down_revision: str | None = "032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.add_column("service_config", sa.Column("team_phone_today", sa.Text()))
    op.add_column("service_config", sa.Column("team_phone_today_until", TZ))
    op.create_check_constraint(
        "ck_service_config_team_phone_today",
        "service_config",
        "(team_phone_today IS NULL) = (team_phone_today_until IS NULL)",
    )
    op.create_table(
        "staff_changes",
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("before", postgresql.JSONB(), nullable=False),
        sa.Column("after", postgresql.JSONB(), nullable=False),
        sa.Column("held", sa.Boolean(), nullable=False),
        sa.Column("status", sa.Text(), server_default="open", nullable=False),
        sa.Column("decided_at", TZ),
        sa.Column("created_at", TZ, server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", TZ, server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "kind IN ('team_phone', 'day_hours')", name="ck_staff_changes_kind"
        ),
        sa.CheckConstraint(
            "status IN ('open', 'kept', 'undone')", name="ck_staff_changes_status"
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_staff_changes_tenant_id", "staff_changes", ["tenant_id"])
    op.create_index(
        "uq_staff_changes_open",
        "staff_changes",
        ["tenant_id", "kind", "day"],
        unique=True,
        postgresql_where=sa.text("status = 'open'"),
    )


def downgrade() -> None:
    op.drop_index("uq_staff_changes_open", table_name="staff_changes")
    op.drop_index("ix_staff_changes_tenant_id", table_name="staff_changes")
    op.drop_table("staff_changes")
    op.drop_constraint(
        "ck_service_config_team_phone_today", "service_config", type_="check"
    )
    op.drop_column("service_config", "team_phone_today_until")
    op.drop_column("service_config", "team_phone_today")
