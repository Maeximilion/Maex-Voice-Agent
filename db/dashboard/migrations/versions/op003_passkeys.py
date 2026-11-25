"""Passkeys, their challenges and the step-up of a session (#571, docs/23 §5).

`operator_passkeys` holds the public half of each WebAuthn credential;
`operator_challenges` the random value one ceremony signs, for one holder and
one purpose. A session remembers when it last showed a second factor
(`stepped_up_at`) and a TOTP secret that is shown but not confirmed yet
(`pending_totp`). `operator_accounts.totp_secret` may now be empty: an
operator with two passkeys may remove TOTP.

The downgrade drops every passkey. An account that had removed TOTP then has
no second factor the old code knows, so it gets a random secret nobody has
seen: its recovery codes still open it, and `python -m api.dashboard.cli reset
<name>` on the operator host gives it new factors. No customer's stack is
touched.

Revision: op003
Revises: op002
Created: 2026-11-16
"""

import base64
import secrets
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "op003"
down_revision: str | None = "op002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "operator_passkeys",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "account_id",
            UUID(as_uuid=True),
            sa.ForeignKey("operator_accounts.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("credential_id", sa.LargeBinary(), nullable=False, unique=True),
        sa.Column("public_key", sa.LargeBinary(), nullable=False),
        sa.Column("sign_count", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "operator_challenges",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("bound_to", sa.Text(), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("challenge", sa.LargeBinary(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.UniqueConstraint("bound_to", "purpose", name="uq_operator_challenge_holder"),
    )
    op.add_column(
        "operator_sessions", sa.Column("stepped_up_at", sa.DateTime(timezone=True))
    )
    op.add_column("operator_sessions", sa.Column("pending_totp", sa.Text()))
    op.alter_column("operator_accounts", "totp_secret", nullable=True)


def downgrade() -> None:
    accounts = sa.table(
        "operator_accounts", sa.column("id"), sa.column("totp_secret", sa.Text())
    )
    bind = op.get_bind()
    for (account_id,) in bind.execute(
        sa.select(accounts.c.id).where(accounts.c.totp_secret.is_(None))
    ).all():
        # As api/dashboard/totp.py new_secret: 20 random bytes in base32.
        unknown = base64.b32encode(secrets.token_bytes(20)).decode()
        bind.execute(
            accounts.update()
            .where(accounts.c.id == account_id)
            .values(totp_secret=unknown)
        )
    op.alter_column("operator_accounts", "totp_secret", nullable=False)
    op.drop_column("operator_sessions", "pending_totp")
    op.drop_column("operator_sessions", "stepped_up_at")
    op.drop_table("operator_challenges")
    op.drop_table("operator_passkeys")
