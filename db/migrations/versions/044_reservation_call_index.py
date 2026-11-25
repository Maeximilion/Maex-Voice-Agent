"""Reservations by call: an index and the time a guest went back to a draft (#908).

The seat count (`api/domain/reservations/capacity.py` `booked_guests`) lets
a call's draft count only while no newer row of the same call exists. With
only `ix_reservations_tenant_reserved_for` every live draft walked the
tenant's whole history for that check, so the many corrections #908 is about
made the count slow inside the day's lock (701 ms for 80 drafts at 144,000
rows); `ix_reservations_call_id` answers it from the call's own few rows.

`replayed_at` is set when a guest goes back to an earlier draft and its key
replays it: from then on that draft is the call's newest and holds the
seats, not the correction the guest left. `created_at` stays as it was, it
anchors the read-back's day words. Empty for every existing row, which then
counts by `created_at` as before.

The upgrade changes no row; the downgrade drops the column with what it
holds, so a backup comes first:
`bash scripts/backup.sh --label before_migration_044` (CLAUDE.md §8, docs/13 §4).

Revision: 044
Revises: 043
Created: 2026-11-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "044"
down_revision: str | None = "043"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX = "ix_reservations_call_id"


def upgrade() -> None:
    op.add_column(
        "reservations",
        sa.Column("replayed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(INDEX, "reservations", ["call_id"])


def downgrade() -> None:
    op.drop_index(INDEX, table_name="reservations")
    op.drop_column("reservations", "replayed_at")
