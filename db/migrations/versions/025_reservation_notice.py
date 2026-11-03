"""How long before its time a table can be reserved, per weekday (T-3.13, #499).

A column on `capacity`, where the seats of a weekday are: a day "wie Sonntag"
takes Sunday's notice with Sunday's seats. Zero is no notice, so nothing changes
until a restaurant sets one. Not `service_config.reservation_lead_minutes`: that
is the gap after opening before the first table.

The downgrade drops the column and with it the notices a restaurant set: take a
backup first with `bash scripts/backup.sh --label before_migration_025`
(CLAUDE.md §8, docs/13 §4).

Revision: 025
Revises: 024
Created: 2026-11-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "025"
down_revision: str | None = "024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "capacity",
        sa.Column("notice_minutes", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_check_constraint(
        "ck_capacity_notice_minutes", "capacity", "notice_minutes >= 0"
    )


def downgrade() -> None:
    op.drop_constraint("ck_capacity_notice_minutes", "capacity", type_="check")
    op.drop_column("capacity", "notice_minutes")
