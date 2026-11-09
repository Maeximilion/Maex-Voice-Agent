"""The restaurant's login to the operations view (#570, D34, docs/23 §6).

One account per restaurant (`gui_accounts`, unique per tenant) with the password
and the team PIN as scrypt hashes; one row per signed-in device
(`gui_sessions`, the token only as its SHA-256); the operator's one-time setup
links (`gui_setup_links`, hashed too); failed sign-ins per client address
(`gui_login_failures`). New tables only, nothing existing changes.

The downgrade drops the four tables: every device is signed out and the
password is gone, so the view needs a new setup link after the next upgrade.
No guest data lives here.

Revision: 030
Revises: 029
Created: 2026-11-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "030"
down_revision: str | None = "029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "gui_accounts",
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=True),
        sa.Column("team_pin_hash", sa.Text(), nullable=True),
        sa.Column("failed_logins", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_failed_at", TZ, nullable=True),
        sa.Column("blocked_until", TZ, nullable=True),
        sa.Column("created_at", TZ, server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", TZ, server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", name="uq_gui_accounts_tenant_id"),
    )
    op.create_table(
        "gui_sessions",
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("account_id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("device", sa.Text(), server_default="", nullable=False),
        sa.Column("created_at", TZ, nullable=False),
        sa.Column("last_seen_at", TZ, nullable=False),
        sa.Column("last_active_at", TZ, nullable=False),
        sa.Column("pin_failures", sa.Integer(), server_default="0", nullable=False),
        sa.ForeignKeyConstraint(
            ["account_id"], ["gui_accounts.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash", name="uq_gui_sessions_token_hash"),
    )
    op.create_index("ix_gui_sessions_account_id", "gui_sessions", ["account_id"])
    op.create_table(
        "gui_setup_links",
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("account_id", sa.UUID(), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("created_at", TZ, nullable=False),
        sa.Column("expires_at", TZ, nullable=False),
        sa.ForeignKeyConstraint(
            ["account_id"], ["gui_accounts.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash", name="uq_gui_setup_links_token_hash"),
    )
    op.create_index("ix_gui_setup_links_account_id", "gui_setup_links", ["account_id"])
    op.create_table(
        "gui_login_failures",
        sa.Column("address", sa.Text(), nullable=False),
        sa.Column("failures", sa.Integer(), nullable=False),
        sa.Column("last_failed_at", TZ, nullable=False),
        sa.Column("blocked_until", TZ, nullable=True),
        sa.PrimaryKeyConstraint("address"),
    )


def downgrade() -> None:
    op.drop_table("gui_login_failures")
    op.drop_index("ix_gui_setup_links_account_id", table_name="gui_setup_links")
    op.drop_table("gui_setup_links")
    op.drop_index("ix_gui_sessions_account_id", table_name="gui_sessions")
    op.drop_table("gui_sessions")
    op.drop_table("gui_accounts")
