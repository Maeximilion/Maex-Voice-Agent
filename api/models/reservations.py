"""Reservierungen (docs/03 Stufe 1). confirm ist der einzige Weg von draft nach confirmed."""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from api.models.base import Base, SoftDelete, TenantScoped, Timestamps, UUIDPrimaryKey
from api.models.tenants import _in

RESERVATION_STATUSES = ("draft", "confirmed", "cancelled")
# Who took the reservation: the agent on a call, or the team on the tablet (T-3.12).
RESERVATION_SOURCES = ("agent", "tablet")


class Reservation(UUIDPrimaryKey, TenantScoped, Timestamps, SoftDelete, Base):
    __tablename__ = "reservations"
    __table_args__ = (
        _in("status", RESERVATION_STATUSES, "ck_reservations_status"),
        _in("source", RESERVATION_SOURCES, "ck_reservations_source"),
        # An agent's table hangs on its call; the team's has none (D30).
        CheckConstraint(
            "(source = 'agent') = (call_id IS NOT NULL)",
            name="ck_reservations_call_source",
        ),
        CheckConstraint("party_size > 0", name="ck_reservations_party_size"),
        # Only the team books outside the hours (#819): the agent never does.
        CheckConstraint(
            "NOT off_hours OR source = 'tablet'",
            name="ck_reservations_off_hours_tablet",
        ),
        Index("ix_reservations_tenant_reserved_for", "tenant_id", "reserved_for"),
        # The seat count asks per draft for a newer row of its call (#908).
        Index("ix_reservations_call_created_at", "call_id", "created_at"),
    )

    call_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("calls.id", ondelete="RESTRICT")
    )
    source: Mapped[str] = mapped_column(Text, nullable=False, server_default="agent")
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="draft")
    guest_name: Mapped[str] = mapped_column(Text, nullable=False)
    phone: Mapped[str] = mapped_column(Text, nullable=False)
    # `phone` is the line the call came from, read once by the draft: the
    # caller ID of the call goes before the table's name does (#523).
    booked_on_own_line: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    party_size: Mapped[int] = mapped_column(Integer, nullable=False)
    reserved_for: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    note: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    # A self-test's card (#574): shown on the tablet as TEST, never sent out.
    is_test: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    # The team booked it outside the opening hours on purpose (#819, migration
    # 042): set by `confirm_tablet` from the hours at the tap, never afterwards.
    off_hours: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )


class ReservationTable(TenantScoped, Base):
    """The table, or the two tables of a combination, a reservation sits on (T-3.12)."""

    __tablename__ = "reservation_tables"

    reservation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("reservations.id", ondelete="CASCADE"), primary_key=True
    )
    table_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("dining_tables.id", ondelete="RESTRICT"),
        primary_key=True,
        index=True,
    )
