"""A table booked outside the opening hours on purpose carries a mark (#819, Maxi 17.11.2026).

`reservations.off_hours` is set by `confirm_tablet` when the team books a time
that no dine-in window of the opening hours holds, after it was asked
("Trotzdem eintragen"). The list "Reservierte Tische außerhalb der
Öffnungszeiten" leaves such a table out, and the board's card names it. Only
the team's tables carry it: the agent books inside the hours only.

New column only, false for every existing row: whether an older table was
entered outside the hours on purpose cannot be read back, so it stays on the
list for the team to check. The downgrade drops the column, and with it the
marks: marked tables come back on the list, the side that calls a guest too
many rather than none. On a database a restaurant signs in to, take a backup
first with `bash scripts/backup.sh --label before_migration_041` (CLAUDE.md §8,
docs/13 §4).

Revision: 041
Revises: 040
Created: 2026-11-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "041"
down_revision: str | None = "040"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "reservations",
        sa.Column("off_hours", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_check_constraint(
        "ck_reservations_off_hours_tablet",
        "reservations",
        "NOT off_hours OR source = 'tablet'",
    )


def downgrade() -> None:
    op.drop_constraint("ck_reservations_off_hours_tablet", "reservations")
    op.drop_column("reservations", "off_hours")
