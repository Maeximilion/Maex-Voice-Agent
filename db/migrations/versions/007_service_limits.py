"""The restaurant's limits from the security audit (#252, Maxi 12.10.2026).

One migration for all five fields, so that the behaviours behind them can be
built side by side without a migration each (order in docs/01_STATUS.md). The
restaurant's rules, so they are fields of `service_config` and no constants in
the code (CLAUDE.md §2 rule 1). The defaults are Maxi's values.

Read from this migration on, by `check_slot`: how far ahead a table can be
booked (90 days), and up to which party size the agent books it itself. Above
`reservation_max_party_size` (8), and for a table on the same day above
`reservation_max_party_size_same_day` (5), the agent takes the wish and the
team calls back.

Read by nothing yet, each with a pull request of its own to come:
`order_approval_above_cents` (15000: an order above 150 euros waits for the
team's approval before a slip prints, whatever the mode) and
`turn_deadline_seconds` (10: how long a guest waits in silence for the model
before the call goes to the team).

Zero is a setting, not an error: no day ahead means today only, no guest means
the team confirms every table, an amount of zero that every order waits. Only
the deadline must be positive: with zero no turn could ever be answered.

On a database that holds a restaurant's data: backup first, with
`bash scripts/backup.sh --label before_migration_007` (CLAUDE.md §8, docs/13
§4). The upgrade writes the defaults into every existing `service_config` row;
the downgrade drops the five columns, and with them every value the restaurant
has set there since. That backup is the way back to those values.

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

# (name, default, lowest value)
COLUMNS = (
    ("reservation_max_days_ahead", "90", 0),
    ("reservation_max_party_size", "8", 0),
    ("reservation_max_party_size_same_day", "5", 0),
    ("order_approval_above_cents", "15000", 0),
    ("turn_deadline_seconds", "10", 1),
)


def upgrade() -> None:
    for name, default, lowest in COLUMNS:
        op.add_column(
            "service_config",
            sa.Column(name, sa.Integer(), nullable=False, server_default=default),
        )
        op.create_check_constraint(
            f"ck_service_config_{name}", "service_config", f"{name} >= {lowest}"
        )


def downgrade() -> None:
    for name, _, _ in reversed(COLUMNS):
        op.drop_constraint(f"ck_service_config_{name}", "service_config")
        op.drop_column("service_config", name)
