"""Bounds on capacity and party size (#252).

`capacity.slot_minutes` must be positive: the grid of a window adds the step
until it passes the end (`domain/reservations/capacity.py`), so zero or less
never ends, and it runs inside the day lock of a reservation. `max_guests` may
be zero (a window closed for reservations), not negative. A reservation is for
at least one guest; the tools already refuse less, the table did not.

An existing row outside the bounds stops the upgrade. Nothing is changed
silently: what the value should have been is the restaurant's decision.

Revision: 006
Revises: 005
Created: 2026-10-12
"""

from collections.abc import Sequence

from alembic import op

revision: str = "006"
down_revision: str | None = "005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (name, table, condition)
CHECKS = (
    ("ck_capacity_slot_minutes", "capacity", "slot_minutes > 0"),
    ("ck_capacity_max_guests", "capacity", "max_guests >= 0"),
    ("ck_reservations_party_size", "reservations", "party_size > 0"),
)


def upgrade() -> None:
    for name, table, condition in CHECKS:
        op.create_check_constraint(name, table, condition)


def downgrade() -> None:
    for name, table, _ in reversed(CHECKS):
        op.drop_constraint(name, table)
