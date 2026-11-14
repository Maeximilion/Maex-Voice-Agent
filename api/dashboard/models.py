"""Tables of the operator database (docs/23 §5), apart from every customer's.

One named account per person. The password is a scrypt hash; the TOTP secret is
stored readable, because the code is an HMAC of it (the database and its backups
stay on the operator host, backups encrypted, T7). Recovery codes, session and
device tokens are random, so only their SHA-256 is kept. Failed sign-ins count
per account (on its row), per client address and per known browser (a device
cookie), as on a customer's login (docs/23 §6).
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Integer,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _id() -> Mapped[uuid.UUID]:
    return mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )


def _account() -> Mapped[uuid.UUID]:
    return mapped_column(
        ForeignKey("operator_accounts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )


def _at(nullable: bool = False) -> Mapped[Any]:
    return mapped_column(DateTime(timezone=True), nullable=nullable)


class Counted:
    """A failure count and the wait it brought (`domain/access/sessions.py`)."""

    failures: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    last_failed_at: Mapped[datetime | None] = _at(nullable=True)
    blocked_until: Mapped[datetime | None] = _at(nullable=True)


class OperatorAccount(Counted, Base):
    __tablename__ = "operator_accounts"
    __table_args__ = (
        CheckConstraint("name ~ '^[a-z0-9._-]{2,40}$'", name="ck_operator_name"),
    )

    id: Mapped[uuid.UUID] = _id()
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    totp_secret: Mapped[str] = mapped_column(Text, nullable=False)
    # The step of the last code taken: that one and older ones open nothing.
    totp_last_step: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class OperatorRecoveryCode(Base):
    __tablename__ = "operator_recovery_codes"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    account_id: Mapped[uuid.UUID] = _account()
    code_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)


class OperatorSession(Base):
    __tablename__ = "operator_sessions"

    id: Mapped[uuid.UUID] = _id()
    account_id: Mapped[uuid.UUID] = _account()
    token_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    # Twelve hours from here at most, thirty minutes from last_seen_at.
    created_at: Mapped[datetime] = _at()
    last_seen_at: Mapped[datetime] = _at()


class OperatorDevice(Counted, Base):
    """A browser that signed in once: its own count of failures, so a guesser
    who keeps the account's wait running does not keep the operator out."""

    __tablename__ = "operator_devices"

    id: Mapped[uuid.UUID] = _id()
    account_id: Mapped[uuid.UUID] = _account()
    token_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    created_at: Mapped[datetime] = _at()
    last_used_at: Mapped[datetime] = _at()


class OperatorLoginFailure(Counted, Base):
    __tablename__ = "operator_login_failures"

    address: Mapped[str] = mapped_column(Text, primary_key=True)


class OperatorAudit(Base):
    """Sign-ins, failures, factor changes and every action of the dashboard:
    who, what, on which customer, when. Never guest data."""

    __tablename__ = "operator_audit"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    # The customer's key once actions on customers exist (#571 part 3).
    target: Mapped[str | None] = mapped_column(Text)
    detail: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
