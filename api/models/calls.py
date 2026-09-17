"""Anruf-Log und Rückrufe (docs/03 Stufe 1)."""

import uuid
from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, Index, Integer, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from api.models.base import Base, SoftDelete, TenantScoped, Timestamps, UUIDPrimaryKey
from api.models.tenants import _in

INTENTS = ("reservation", "pickup", "delivery", "info", "complaint", "unknown")
OUTCOMES = ("completed", "transferred", "callback", "abandoned", "error")
CALLBACK_REASONS = ("complaint", "not_understood", "human_requested", "out_of_scope")
CALLBACK_STATUSES = ("open", "done")


class Call(UUIDPrimaryKey, TenantScoped, Timestamps, Base):
    __tablename__ = "calls"
    __table_args__ = (
        _in("intent", INTENTS, "ck_calls_intent"),
        _in("outcome", OUTCOMES, "ck_calls_outcome"),
        Index("ix_calls_tenant_started", "tenant_id", "started_at"),
    )

    external_session_id: Mapped[str] = mapped_column(Text, nullable=False)
    caller_id: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_seconds: Mapped[int | None] = mapped_column(Integer)
    intent: Mapped[str | None] = mapped_column(Text)
    outcome: Mapped[str | None] = mapped_column(Text)
    transfer_reason: Mapped[str | None] = mapped_column(Text)
    cost_cents: Mapped[int | None] = mapped_column(Integer)
    model: Mapped[str | None] = mapped_column(Text)
    tool_calls: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    delete_after: Mapped[date] = mapped_column(Date, nullable=False)


class Callback(UUIDPrimaryKey, TenantScoped, Timestamps, SoftDelete, Base):
    __tablename__ = "callbacks"
    __table_args__ = (
        _in("reason", CALLBACK_REASONS, "ck_callbacks_reason"),
        _in("status", CALLBACK_STATUSES, "ck_callbacks_status"),
        Index("ix_callbacks_tenant_status", "tenant_id", "status"),
    )

    call_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("calls.id", ondelete="RESTRICT"), nullable=False
    )
    phone: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="open")
    done_by: Mapped[str | None] = mapped_column(Text)
    done_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
