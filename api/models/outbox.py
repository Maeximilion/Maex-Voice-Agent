"""Outbox: der kalte Pfad beginnt hier, in derselben Transaktion wie der Fachvorgang."""

from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from api.models.base import Base, TenantScoped, Timestamps, UUIDPrimaryKey
from api.models.tenants import _in

EVENT_TYPES = (
    "order.confirmed",
    "reservation.confirmed",
    "callback.created",
    "order.handover_failed",
    "daily.report",
)
OUTBOX_STATUSES = ("pending", "sent", "failed")


class OutboxEvent(UUIDPrimaryKey, TenantScoped, Timestamps, Base):
    __tablename__ = "outbox"
    __table_args__ = (
        _in("event_type", EVENT_TYPES, "ck_outbox_event_type"),
        _in("status", OUTBOX_STATUSES, "ck_outbox_status"),
        Index("ix_outbox_status_next_attempt", "status", "next_attempt_at"),
    )

    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_error: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
