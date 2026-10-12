"""Limits for a reservation by phone (#252, Maxi 12.10.2026).

How far ahead a table can be booked, and up to which party size the agent
books it itself: above `reservation_max_party_size`, and for a table on the
same day above `reservation_max_party_size_same_day`, the agent takes the wish
and the team calls back. The restaurant's rules, so they are fields of
`service_config` and no constants in the code (CLAUDE.md §2 rule 1). The
defaults are Maxi's values: 90 days, 8 guests, 5 guests on the same day.

Zero is a setting, not an error: no day ahead means today only, no guest means
the team confirms every table.

Revision: 007
Revises: 006
Created: 2026-10-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "007"
down_revision: str | None = "006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COLUMNS = (
    ("reservation_max_days_ahead", "90"),
    ("reservation_max_party_size", "8"),
    ("reservation_max_party_size_same_day", "5"),
)


def upgrade() -> None:
    for name, default in COLUMNS:
        op.add_column(
            "service_config",
            sa.Column(name, sa.Integer(), nullable=False, server_default=default),
        )
        op.create_check_constraint(
            f"ck_service_config_{name}", "service_config", f"{name} >= 0"
        )


def downgrade() -> None:
    for name, _ in reversed(COLUMNS):
        op.drop_constraint(f"ck_service_config_{name}", "service_config")
        op.drop_column("service_config", name)
