"""Reservations the team enters on the tablet, seated on the floor plan (T-3.12, #497).

A friend asks an employee on WhatsApp and the team enters the table by hand:
no call, so `reservations.call_id` may be empty, and `source` says who took the
reservation (`agent` on a call, `tablet` by the team). A CHECK keeps the two
together: an agent's reservation has its call, a tablet's has none (D30).
Existing rows are the agent's and keep their call.

`reservation_tables` seats a reservation on one table of the plan or on the two
tables of a combination. A table is soft deleted (`dining_tables.deleted_at`),
so a reservation keeps its name; a reservation that goes takes its seating with
it.

The downgrade deletes every reservation the team entered on the tablet, with its
seating, because `call_id` becomes required again: on a database that holds a
restaurant's data, take a backup first with
`bash scripts/backup.sh --label before_migration_029` (CLAUDE.md §8, docs/13 §4).

Revision: 029
Revises: 028
Created: 2026-11-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "029"
down_revision: str | None = "028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "reservations",
        sa.Column("source", sa.Text(), server_default="agent", nullable=False),
    )
    op.alter_column("reservations", "call_id", existing_type=sa.UUID(), nullable=True)
    op.create_check_constraint(
        "ck_reservations_source", "reservations", "source IN ('agent', 'tablet')"
    )
    op.create_check_constraint(
        "ck_reservations_call_source",
        "reservations",
        "(source = 'agent') = (call_id IS NOT NULL)",
    )
    op.create_table(
        "reservation_tables",
        sa.Column("reservation_id", sa.UUID(), nullable=False),
        sa.Column("table_id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.ForeignKeyConstraint(
            ["reservation_id"], ["reservations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["table_id"], ["dining_tables.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("reservation_id", "table_id"),
    )
    op.create_index(
        op.f("ix_reservation_tables_table_id"),
        "reservation_tables",
        ["table_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_reservation_tables_tenant_id"),
        "reservation_tables",
        ["tenant_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_table("reservation_tables")
    # Without a call these rows cannot stand once call_id is required again.
    op.execute("DELETE FROM reservations WHERE call_id IS NULL")
    op.drop_constraint("ck_reservations_call_source", "reservations", type_="check")
    op.drop_constraint("ck_reservations_source", "reservations", type_="check")
    op.alter_column("reservations", "call_id", existing_type=sa.UUID(), nullable=False)
    op.drop_column("reservations", "source")
