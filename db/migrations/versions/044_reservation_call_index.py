"""An index on the reservations by call and creation time (#908).

The seat count (`api/domain/reservations/capacity.py` `booked_guests`) lets
a call's draft count only while no newer row of the same call exists. With
only `ix_reservations_tenant_reserved_for` every live draft walked the
tenant's whole history for that check, so the many corrections #908 is about
made the count slow inside the day's lock (701 ms for 80 drafts at 144,000
rows); this index answers it from the call's own few rows (3.6 ms). The
upgrade and the downgrade change no row.

Revision: 044
Revises: 043
Created: 2026-11-22
"""

from collections.abc import Sequence

from alembic import op

revision: str = "044"
down_revision: str | None = "043"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX = "ix_reservations_call_created_at"


def upgrade() -> None:
    op.create_index(INDEX, "reservations", ["call_id", "created_at"])


def downgrade() -> None:
    op.drop_index(INDEX, table_name="reservations")
