"""Holidays of the restaurant's state as suggestions in `special_days` (T-9.5).

A statutory holiday says nothing about whether the restaurant opens: many do.
So the holiday job only suggests a date, and the team answers on the tablet
(docs/06 §3). Until then the date keeps its weekday's hours (CLAUDE.md §2
rule 1: hours come from what the restaurant set, never from the code).

- `special_days.status`: `set` (counts, every existing row), `suggested`
  (written by the job, counts for nothing) or `dismissed` (the team keeps the
  weekday's hours; the job does not suggest that date again).
- `special_days.like_weekday`: the hours and seats of that weekday hold on the
  date ("wie Sonntag"), Monday is 0. Never together with `closed` or hours of
  its own.
- `uq_special_days_suggestion`: one suggestion or dismissal per date, so two
  passes of the job at once write one row.
- `service_config.holiday_region`: the state, as ISO 3166-2 code without
  "DE-"; none means no suggestions.

On a database that holds a restaurant's data: backup first, with
`bash scripts/backup.sh --label before_migration_015` (CLAUDE.md §8, docs/13
§4). The upgrade marks every existing special day `set`, so nothing changes.
The downgrade deletes the rows that are not `set` (a suggestion or dismissal
would count as a special day without the column), turns a `set` day "like a
weekday" into an ordinary row without hours, which keeps that date's own
weekday, and drops the region.

Revision: 015
Revises: 014
Created: 2026-10-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "015"
down_revision: str | None = "014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

REGIONS = (
    *("BB", "BE", "BW", "BY", "HB", "HE", "HH", "MV"),
    *("NI", "NW", "RP", "SH", "SL", "SN", "ST", "TH"),
)


def upgrade() -> None:
    op.add_column(
        "special_days",
        sa.Column("status", sa.Text(), nullable=False, server_default="set"),
    )
    op.add_column("special_days", sa.Column("like_weekday", sa.SmallInteger()))
    op.create_check_constraint(
        "ck_special_days_status",
        "special_days",
        "status IN ('set', 'suggested', 'dismissed')",
    )
    op.create_check_constraint(
        "ck_special_days_like_weekday", "special_days", "like_weekday BETWEEN 0 AND 6"
    )
    op.create_check_constraint(
        "ck_special_days_like_weekday_alone",
        "special_days",
        "like_weekday IS NULL OR "
        "(NOT closed AND opens_at IS NULL AND closes_at IS NULL)",
    )
    op.create_index(
        "uq_special_days_suggestion",
        "special_days",
        ["tenant_id", "date"],
        unique=True,
        postgresql_where=sa.text("status <> 'set'"),
    )
    op.add_column("service_config", sa.Column("holiday_region", sa.Text()))
    quoted = ", ".join(f"'{r}'" for r in REGIONS)
    op.create_check_constraint(
        "ck_service_config_holiday_region",
        "service_config",
        f"holiday_region IN ({quoted})",
    )


def downgrade() -> None:
    op.drop_column("service_config", "holiday_region")
    op.execute("DELETE FROM special_days WHERE status <> 'set'")
    op.drop_index("uq_special_days_suggestion", table_name="special_days")
    op.drop_column("special_days", "like_weekday")
    op.drop_column("special_days", "status")
