"""Anruf-Log und Rückrufe (docs/03 Stufe 1)."""

import uuid
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
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


class ComplaintRule(UUIDPrimaryKey, TenantScoped, Timestamps, Base):
    """One fixed answer to a complaint (D19, docs/05 §3): the words that select
    it, the remedy in cents and the two sentences the guest hears. The core
    reads it, the model never does (hard rule 1). No personal data. A new row
    stays off until someone switches it on: a remedy is money."""

    __tablename__ = "complaint_rules"
    __table_args__ = (
        CheckConstraint("cardinality(phrases) > 0", name="ck_complaint_rules_phrases"),
        CheckConstraint("remedy_cents > 0", name="ck_complaint_rules_remedy_cents"),
        CheckConstraint(
            "btrim(name) <> '' AND btrim(offer_say) <> '' AND btrim(granted_say) <> ''",
            name="ck_complaint_rules_texts",
        ),
        # With the amount and the guest's sentence it fits a callback's summary.
        CheckConstraint("char_length(name) <= 100", name="ck_complaint_rules_name"),
    )

    # What the team reads on the card.
    name: Mapped[str] = mapped_column(Text, nullable=False)
    phrases: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    remedy_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    offer_say: Mapped[str] = mapped_column(Text, nullable=False)
    granted_say: Mapped[str] = mapped_column(Text, nullable=False)
    active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )


CONSENT_ANSWERS = ("yes", "no", "unclear")


class ConsentLog(UUIDPrimaryKey, TenantScoped, Base):
    """One answer to a consent question of the agent (legal check L13): the
    call, when it was answered, the question word for word as it was asked
    (its version), and the answer. Proof of the consent a stored allergy note
    rests on; the allergy itself is not in it."""

    __tablename__ = "consent_log"
    __table_args__ = (_in("answer", CONSENT_ANSWERS, "ck_consent_log_answer"),)

    call_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("calls.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    question: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[str] = mapped_column(Text, nullable=False)
    answered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
