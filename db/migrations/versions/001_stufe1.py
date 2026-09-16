"""Stufe 1: tenants, service_config, opening_hours, special_days, capacity, reservations, calls, callbacks, outbox, audit_log.

Quelle: docs/03_DATENMODELL.md §Migrationsreihenfolge. Per autogenerate erzeugt und durchgesehen.

Revision: 001
Vorgänger:
Erstellt: 2026-09-16 19:12:17.569237
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column(
            "at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("entity", sa.Text(), nullable=False),
        sa.Column("entity_id", sa.UUID(), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
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
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_audit_log_tenant_at", "audit_log", ["tenant_id", "at"], unique=False
    )
    op.create_table(
        "tenants",
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column(
            "timezone", sa.Text(), server_default="Europe/Berlin", nullable=False
        ),
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
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
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "calls",
        sa.Column("external_session_id", sa.Text(), nullable=False),
        sa.Column("caller_id", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_seconds", sa.Integer(), nullable=True),
        sa.Column("intent", sa.Text(), nullable=True),
        sa.Column("outcome", sa.Text(), nullable=True),
        sa.Column("transfer_reason", sa.Text(), nullable=True),
        sa.Column("cost_cents", sa.Integer(), nullable=True),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column(
            "tool_calls",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("delete_after", sa.Date(), nullable=False),
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
        sa.CheckConstraint(
            "intent IN ('reservation', 'pickup', 'delivery', 'info', 'complaint', 'unknown')",
            name="ck_calls_intent",
        ),
        sa.CheckConstraint(
            "outcome IN ('completed', 'transferred', 'callback', 'abandoned', 'error')",
            name="ck_calls_outcome",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_calls_tenant_id"), "calls", ["tenant_id"], unique=False)
    op.create_index(
        "ix_calls_tenant_started", "calls", ["tenant_id", "started_at"], unique=False
    )
    op.create_table(
        "capacity",
        sa.Column("weekday", sa.SmallInteger(), nullable=False),
        sa.Column("slot_start", sa.Time(), nullable=False),
        sa.Column("slot_end", sa.Time(), nullable=False),
        sa.Column("max_guests", sa.Integer(), nullable=False),
        sa.Column("slot_minutes", sa.Integer(), server_default="30", nullable=False),
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
        sa.CheckConstraint("weekday BETWEEN 0 AND 6", name="ck_capacity_weekday"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_capacity_tenant_id"), "capacity", ["tenant_id"], unique=False
    )
    op.create_table(
        "opening_hours",
        sa.Column("weekday", sa.SmallInteger(), nullable=False),
        sa.Column("opens_at", sa.Time(), nullable=False),
        sa.Column("closes_at", sa.Time(), nullable=False),
        sa.Column("service", sa.Text(), nullable=False),
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
        sa.CheckConstraint(
            "service IN ('dinein', 'pickup', 'delivery')",
            name="ck_opening_hours_service",
        ),
        sa.CheckConstraint("weekday BETWEEN 0 AND 6", name="ck_opening_hours_weekday"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_opening_hours_tenant_id"), "opening_hours", ["tenant_id"], unique=False
    )
    op.create_table(
        "outbox",
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.Text(), server_default="pending", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.CheckConstraint(
            "event_type IN ('order.confirmed', 'reservation.confirmed', 'callback.created', 'order.handover_failed', 'daily.report')",
            name="ck_outbox_event_type",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'sent', 'failed')", name="ck_outbox_status"
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_outbox_status_next_attempt",
        "outbox",
        ["status", "next_attempt_at"],
        unique=False,
    )
    op.create_index(op.f("ix_outbox_tenant_id"), "outbox", ["tenant_id"], unique=False)
    op.create_table(
        "service_config",
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("call_mode", sa.Text(), server_default="shadow", nullable=False),
        sa.Column(
            "delivery_enabled", sa.Boolean(), server_default="true", nullable=False
        ),
        sa.Column(
            "pickup_wait_minutes", sa.Integer(), server_default="20", nullable=False
        ),
        sa.Column(
            "delivery_wait_minutes", sa.Integer(), server_default="45", nullable=False
        ),
        sa.Column("team_phone", sa.Text(), nullable=False),
        sa.Column(
            "max_call_seconds", sa.Integer(), server_default="420", nullable=False
        ),
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
        sa.CheckConstraint(
            "call_mode IN ('shadow', 'overflow', 'primary', 'paused')",
            name="ck_service_config_call_mode",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("tenant_id"),
    )
    op.create_table(
        "special_days",
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("closed", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("opens_at", sa.Time(), nullable=True),
        sa.Column("closes_at", sa.Time(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
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
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_special_days_tenant_id"), "special_days", ["tenant_id"], unique=False
    )
    op.create_table(
        "callbacks",
        sa.Column("call_id", sa.UUID(), nullable=False),
        sa.Column("phone", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default="open", nullable=False),
        sa.Column("done_by", sa.Text(), nullable=True),
        sa.Column("done_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "reason IN ('complaint', 'not_understood', 'human_requested', 'out_of_scope')",
            name="ck_callbacks_reason",
        ),
        sa.CheckConstraint("status IN ('open', 'done')", name="ck_callbacks_status"),
        sa.ForeignKeyConstraint(["call_id"], ["calls.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_callbacks_tenant_id"), "callbacks", ["tenant_id"], unique=False
    )
    op.create_index(
        "ix_callbacks_tenant_status", "callbacks", ["tenant_id", "status"], unique=False
    )
    op.create_table(
        "reservations",
        sa.Column("call_id", sa.UUID(), nullable=False),
        sa.Column("status", sa.Text(), server_default="draft", nullable=False),
        sa.Column("guest_name", sa.Text(), nullable=False),
        sa.Column("phone", sa.Text(), nullable=False),
        sa.Column("party_size", sa.Integer(), nullable=False),
        sa.Column("reserved_for", sa.DateTime(timezone=True), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
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
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('draft', 'confirmed', 'cancelled')",
            name="ck_reservations_status",
        ),
        sa.ForeignKeyConstraint(["call_id"], ["calls.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
    )
    op.create_index(
        op.f("ix_reservations_tenant_id"), "reservations", ["tenant_id"], unique=False
    )
    op.create_index(
        "ix_reservations_tenant_reserved_for",
        "reservations",
        ["tenant_id", "reserved_for"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_reservations_tenant_reserved_for", table_name="reservations")
    op.drop_index(op.f("ix_reservations_tenant_id"), table_name="reservations")
    op.drop_table("reservations")
    op.drop_index("ix_callbacks_tenant_status", table_name="callbacks")
    op.drop_index(op.f("ix_callbacks_tenant_id"), table_name="callbacks")
    op.drop_table("callbacks")
    op.drop_index(op.f("ix_special_days_tenant_id"), table_name="special_days")
    op.drop_table("special_days")
    op.drop_table("service_config")
    op.drop_index(op.f("ix_outbox_tenant_id"), table_name="outbox")
    op.drop_index("ix_outbox_status_next_attempt", table_name="outbox")
    op.drop_table("outbox")
    op.drop_index(op.f("ix_opening_hours_tenant_id"), table_name="opening_hours")
    op.drop_table("opening_hours")
    op.drop_index(op.f("ix_capacity_tenant_id"), table_name="capacity")
    op.drop_table("capacity")
    op.drop_index("ix_calls_tenant_started", table_name="calls")
    op.drop_index(op.f("ix_calls_tenant_id"), table_name="calls")
    op.drop_table("calls")
    op.drop_table("tenants")
    op.drop_index("ix_audit_log_tenant_at", table_name="audit_log")
    op.drop_table("audit_log")
