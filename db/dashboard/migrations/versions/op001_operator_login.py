"""The operator login: accounts, recovery codes, sessions, devices, failures
and the operator's own audit (#571, docs/23 §5).

The first migration of the operator database, which holds no customer data. The
downgrade drops every table and with them every operator account: the
dashboard then needs `python -m api.dashboard.cli add` again.

Revision: op001
Revises:
Created: 2026-11-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "op001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _id() -> sa.Column:
    return sa.Column(
        "id",
        UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
    )


def _account() -> sa.Column:
    return sa.Column(
        "account_id",
        UUID(as_uuid=True),
        sa.ForeignKey("operator_accounts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )


def _at(name: str, nullable: bool = False) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def _counted() -> list[sa.Column]:
    return [
        sa.Column("failures", sa.Integer(), nullable=False, server_default="0"),
        _at("last_failed_at", nullable=True),
        _at("blocked_until", nullable=True),
    ]


def upgrade() -> None:
    op.create_table(
        "operator_accounts",
        _id(),
        sa.Column("name", sa.Text(), nullable=False, unique=True),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("totp_secret", sa.Text(), nullable=False),
        sa.Column("totp_last_step", sa.BigInteger()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        *_counted(),
        sa.CheckConstraint("name ~ '^[a-z0-9._-]{2,40}$'", name="ck_operator_name"),
    )
    op.create_table(
        "operator_recovery_codes",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        _account(),
        sa.Column("code_hash", sa.Text(), nullable=False, unique=True),
    )
    op.create_table(
        "operator_sessions",
        _id(),
        _account(),
        sa.Column("token_hash", sa.Text(), nullable=False, unique=True),
        _at("created_at"),
        _at("last_seen_at"),
    )
    op.create_table(
        "operator_devices",
        _id(),
        _account(),
        sa.Column("token_hash", sa.Text(), nullable=False, unique=True),
        _at("created_at"),
        _at("last_used_at"),
        *_counted(),
    )
    op.create_table(
        "operator_login_failures",
        sa.Column("address", sa.Text(), primary_key=True),
        *_counted(),
    )
    op.create_table(
        "operator_audit",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("target", sa.Text()),
        sa.Column(
            "detail", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
    )


def downgrade() -> None:
    for table in (
        "operator_audit",
        "operator_login_failures",
        "operator_devices",
        "operator_sessions",
        "operator_recovery_codes",
        "operator_accounts",
    ):
        op.drop_table(table)
