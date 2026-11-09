"""The restaurant's login to the operations view (#570, D34, docs/23 §6).

One login per restaurant: `gui_accounts` holds its password and the team PIN,
both as scrypt hashes. Each signed-in device is a row of `gui_sessions`; the
browser keeps only a random token, the row only its SHA-256. A setup link made
by the operator sets the password; it is stored hashed too. Failed sign-ins are
counted per account (on its row) and per client address (`gui_login_failures`).
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from api.models.base import Base, Timestamps, UUIDPrimaryKey


class GuiAccount(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "gui_accounts"

    # One login per restaurant (D34): unique, not just indexed.
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    # Empty until the first setup link is used.
    password_hash: Mapped[str | None] = mapped_column(Text)
    team_pin_hash: Mapped[str | None] = mapped_column(Text)
    failed_logins: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    last_failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    blocked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class GuiSession(UUIDPrimaryKey, Base):
    __tablename__ = "gui_sessions"

    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("gui_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    # The browser's own words, cut short: names the device in the list (#570 b).
    device: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    # Any request: the 30-day window slides from here.
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    # A sign-in, a PIN or a change: 15 minutes after it the next change asks the PIN.
    last_active_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    pin_failures: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )


class GuiSetupLink(UUIDPrimaryKey, Base):
    __tablename__ = "gui_setup_links"

    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("gui_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class GuiLoginFailure(Base):
    __tablename__ = "gui_login_failures"

    # The client address as Caddy saw it; deleted a day after its last failure.
    address: Mapped[str] = mapped_column(Text, primary_key=True)
    failures: Mapped[int] = mapped_column(Integer, nullable=False)
    last_failed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    blocked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
