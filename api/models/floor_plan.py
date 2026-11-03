"""The floor plan: rooms and terraces, their tables and which tables combine (T-3.11, docs/03).

A table is drawn and named here; it books nothing. The agent still counts seats
in `capacity` (D28) until the manual seating of T-3.12 is in use. Walls, doors,
counters and labels are only drawn, nothing refers to them, so they live as one
JSON list on their area instead of a table each.
"""

import uuid

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, SmallInteger, Text
from sqlalchemy import text as sql
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from api.models.base import Base, SoftDelete, TenantScoped, Timestamps, UUIDPrimaryKey
from api.models.tenants import _in

AREA_KINDS = ("inside", "terrace")
AREA_SHAPES = ("rectangle", "square")
TABLE_SHAPES = ("rect", "round")
TABLE_ROTATIONS = (0, 90, 180, 270)
END_SEATS = ("left", "right")


class FloorArea(UUIDPrimaryKey, TenantScoped, Timestamps, Base):
    __tablename__ = "floor_areas"
    __table_args__ = (
        _in("kind", AREA_KINDS, "ck_floor_areas_kind"),
        _in("shape", AREA_SHAPES, "ck_floor_areas_shape"),
        CheckConstraint(
            "char_length(btrim(name)) BETWEEN 1 AND 30", name="ck_floor_areas_name"
        ),
        CheckConstraint(
            "width_cm BETWEEN 200 AND 5000 AND depth_cm BETWEEN 200 AND 5000",
            name="ck_floor_areas_size",
        ),
        CheckConstraint(
            "shape <> 'square' OR width_cm = depth_cm", name="ck_floor_areas_square"
        ),
        CheckConstraint("wall_cm BETWEEN 5 AND 60", name="ck_floor_areas_wall"),
        CheckConstraint("revision >= 0", name="ck_floor_areas_revision"),
        CheckConstraint(
            "jsonb_typeof(fixtures) = 'array'", name="ck_floor_areas_fixtures"
        ),
        # Two rooms of one name could not be told apart on the tablet.
        Index("uq_floor_areas_name", "tenant_id", sql("lower(name)"), unique=True),
    )

    name: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False, server_default="inside")
    shape: Mapped[str] = mapped_column(Text, nullable=False, server_default="rectangle")
    width_cm: Mapped[int] = mapped_column(Integer, nullable=False)
    depth_cm: Mapped[int] = mapped_column(Integer, nullable=False)
    wall_cm: Mapped[int] = mapped_column(Integer, nullable=False, server_default="20")
    # Walls, doors, counters and labels, checked by domain/reservations/floor_plan.py.
    fixtures: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default=sql("'[]'::jsonb")
    )
    # Every save names the revision it started from; another tablet's save in
    # between moves it, and the older save is refused instead of overwriting.
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")


class DiningTable(UUIDPrimaryKey, TenantScoped, Timestamps, SoftDelete, Base):
    __tablename__ = "dining_tables"
    __table_args__ = (
        _in("shape", TABLE_SHAPES, "ck_dining_tables_shape"),
        _in("end_seat", END_SEATS, "ck_dining_tables_end_seat"),
        CheckConstraint(
            "char_length(btrim(name)) BETWEEN 1 AND 25", name="ck_dining_tables_name"
        ),
        CheckConstraint("seats BETWEEN 1 AND 40", name="ck_dining_tables_seats"),
        CheckConstraint(
            "width_cm BETWEEN 30 AND 600 AND depth_cm BETWEEN 30 AND 600",
            name="ck_dining_tables_size",
        ),
        CheckConstraint(
            "shape <> 'round' OR width_cm = depth_cm", name="ck_dining_tables_round"
        ),
        CheckConstraint("x_cm >= 0 AND y_cm >= 0", name="ck_dining_tables_position"),
        CheckConstraint(
            "rotation IN (0, 90, 180, 270)", name="ck_dining_tables_rotation"
        ),
        # The team will seat a reservation on "T5" (T-3.12): one live T5 per restaurant.
        Index(
            "uq_dining_tables_name",
            "tenant_id",
            sql("lower(name)"),
            unique=True,
            postgresql_where=sql("deleted_at IS NULL"),
        ),
    )

    area_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("floor_areas.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    shape: Mapped[str] = mapped_column(Text, nullable=False, server_default="rect")
    seats: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    width_cm: Mapped[int] = mapped_column(Integer, nullable=False)
    depth_cm: Mapped[int] = mapped_column(Integer, nullable=False)
    # The centre of the table, from the room's top left corner.
    x_cm: Mapped[int] = mapped_column(Integer, nullable=False)
    y_cm: Mapped[int] = mapped_column(Integer, nullable=False)
    rotation: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default="0"
    )
    # The end that takes the one end seat of a rectangle with 5 seats (or 1, 3).
    end_seat: Mapped[str] = mapped_column(Text, nullable=False, server_default="left")


class TableCombination(UUIDPrimaryKey, TenantScoped, Timestamps, Base):
    """Two tables the team may push together; one row per pair, the smaller id first."""

    __tablename__ = "table_combinations"
    __table_args__ = (
        CheckConstraint("table_a_id < table_b_id", name="ck_table_combinations_order"),
        Index("uq_table_combinations_pair", "table_a_id", "table_b_id", unique=True),
    )

    table_a_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("dining_tables.id", ondelete="RESTRICT"), nullable=False
    )
    table_b_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("dining_tables.id", ondelete="RESTRICT"), nullable=False, index=True
    )
