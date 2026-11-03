"""The floor plan: rooms and terraces, tables, which tables combine (T-3.11, #496).

Three new tables, nothing existing changes, and nothing reads them for a booking:
the agent keeps counting seats in `capacity` (D28). The downgrade drops the three
with every room, table and pair the team drew; on a database that holds a
restaurant's data, take a backup first with
`bash scripts/backup.sh --label before_migration_028` (CLAUDE.md §8, docs/13 §4).

Revision: 028
Revises: 027
Created: 2026-11-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "028"
down_revision: str | None = "027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _base() -> list[sa.Column]:
    return [
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    ]


def upgrade() -> None:
    op.create_table(
        "floor_areas",
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), server_default="inside", nullable=False),
        sa.Column("shape", sa.Text(), server_default="rectangle", nullable=False),
        sa.Column("width_cm", sa.Integer(), nullable=False),
        sa.Column("depth_cm", sa.Integer(), nullable=False),
        sa.Column("wall_cm", sa.Integer(), server_default="20", nullable=False),
        sa.Column(
            "fixtures",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("revision", sa.Integer(), server_default="0", nullable=False),
        *_base(),
        sa.CheckConstraint("kind IN ('inside', 'terrace')", name="ck_floor_areas_kind"),
        sa.CheckConstraint(
            "shape IN ('rectangle', 'square')", name="ck_floor_areas_shape"
        ),
        sa.CheckConstraint(
            "char_length(btrim(name)) BETWEEN 1 AND 30", name="ck_floor_areas_name"
        ),
        sa.CheckConstraint(
            "width_cm BETWEEN 200 AND 5000 AND depth_cm BETWEEN 200 AND 5000",
            name="ck_floor_areas_size",
        ),
        sa.CheckConstraint(
            "shape <> 'square' OR width_cm = depth_cm", name="ck_floor_areas_square"
        ),
        sa.CheckConstraint("wall_cm BETWEEN 5 AND 60", name="ck_floor_areas_wall"),
        sa.CheckConstraint("revision >= 0", name="ck_floor_areas_revision"),
        sa.CheckConstraint(
            "jsonb_typeof(fixtures) = 'array'", name="ck_floor_areas_fixtures"
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_floor_areas_tenant_id"), "floor_areas", ["tenant_id"], unique=False
    )
    op.create_index(
        "uq_floor_areas_name",
        "floor_areas",
        ["tenant_id", sa.text("lower(name)")],
        unique=True,
    )

    op.create_table(
        "dining_tables",
        sa.Column("area_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("shape", sa.Text(), server_default="rect", nullable=False),
        sa.Column("seats", sa.SmallInteger(), nullable=False),
        sa.Column("width_cm", sa.Integer(), nullable=False),
        sa.Column("depth_cm", sa.Integer(), nullable=False),
        sa.Column("x_cm", sa.Integer(), nullable=False),
        sa.Column("y_cm", sa.Integer(), nullable=False),
        sa.Column("rotation", sa.SmallInteger(), server_default="0", nullable=False),
        sa.Column("end_seat", sa.Text(), server_default="left", nullable=False),
        *_base(),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("shape IN ('rect', 'round')", name="ck_dining_tables_shape"),
        sa.CheckConstraint(
            "end_seat IN ('left', 'right')", name="ck_dining_tables_end_seat"
        ),
        sa.CheckConstraint(
            "char_length(btrim(name)) BETWEEN 1 AND 25", name="ck_dining_tables_name"
        ),
        sa.CheckConstraint("seats BETWEEN 1 AND 40", name="ck_dining_tables_seats"),
        sa.CheckConstraint(
            "width_cm BETWEEN 30 AND 600 AND depth_cm BETWEEN 30 AND 600",
            name="ck_dining_tables_size",
        ),
        sa.CheckConstraint(
            "shape <> 'round' OR width_cm = depth_cm", name="ck_dining_tables_round"
        ),
        sa.CheckConstraint("x_cm >= 0 AND y_cm >= 0", name="ck_dining_tables_position"),
        sa.CheckConstraint(
            "rotation IN (0, 90, 180, 270)", name="ck_dining_tables_rotation"
        ),
        sa.ForeignKeyConstraint(["area_id"], ["floor_areas.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_dining_tables_tenant_id"), "dining_tables", ["tenant_id"], unique=False
    )
    op.create_index(
        op.f("ix_dining_tables_area_id"), "dining_tables", ["area_id"], unique=False
    )
    op.create_index(
        "uq_dining_tables_name",
        "dining_tables",
        ["tenant_id", sa.text("lower(name)")],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )

    op.create_table(
        "table_combinations",
        sa.Column("table_a_id", sa.UUID(), nullable=False),
        sa.Column("table_b_id", sa.UUID(), nullable=False),
        *_base(),
        sa.CheckConstraint(
            "table_a_id < table_b_id", name="ck_table_combinations_order"
        ),
        sa.ForeignKeyConstraint(
            ["table_a_id"], ["dining_tables.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["table_b_id"], ["dining_tables.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_table_combinations_tenant_id"),
        "table_combinations",
        ["tenant_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_table_combinations_table_b_id"),
        "table_combinations",
        ["table_b_id"],
        unique=False,
    )
    op.create_index(
        "uq_table_combinations_pair",
        "table_combinations",
        ["table_a_id", "table_b_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_table("table_combinations")
    op.drop_table("dining_tables")
    op.drop_table("floor_areas")
