"""Tables of the operator database (docs/23 §5), apart from every customer's.

One named account per person. The password is a scrypt hash; the TOTP secret is
stored readable, because the code is an HMAC of it (the database and its backups
stay on the operator host, backups encrypted, T7). Recovery codes, session and
device tokens are random, so only their SHA-256 is kept. Of a passkey only the
public key is here; its private key never leaves the operator's device. Failed sign-ins count
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
    Index,
    Integer,
    LargeBinary,
    Text,
    UniqueConstraint,
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
    # None once the operator removed TOTP, which takes two passkeys.
    totp_secret: Mapped[str | None] = mapped_column(Text)
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
    # The last second factor this session showed: the sign-in's, then each
    # step-up. Dangerous actions need one of the last five minutes.
    stepped_up_at: Mapped[datetime | None] = _at(nullable=True)
    # The last passkey on the step-up, never the sign-in's: once the account
    # has a passkey, a change of its factors needs one of the last five
    # minutes (D37).
    passkey_stepped_up_at: Mapped[datetime | None] = _at(nullable=True)
    # A new TOTP secret, shown to the operator and not in force until a code
    # of it came back.
    pending_totp: Mapped[str | None] = mapped_column(Text)


class OperatorPasskey(Base):
    """A WebAuthn credential of an account. The counter is the authenticator's
    own count of signatures: one that does not grow means a copy."""

    __tablename__ = "operator_passkeys"

    id: Mapped[uuid.UUID] = _id()
    account_id: Mapped[uuid.UUID] = _account()
    credential_id: Mapped[bytes] = mapped_column(
        LargeBinary, nullable=False, unique=True
    )
    public_key: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    sign_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _at()
    last_used_at: Mapped[datetime | None] = _at(nullable=True)


class OperatorChallenge(Base):
    """The random value one WebAuthn ceremony signs. One per purpose and
    holder (`session:<id>`, or `attempt:<hash of the sign-in's cookie>`), taken
    out when it is used, worthless after `expires_at`."""

    __tablename__ = "operator_challenges"
    __table_args__ = (
        UniqueConstraint("bound_to", "purpose", name="uq_operator_challenge_holder"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    bound_to: Mapped[str] = mapped_column(Text, nullable=False)
    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    challenge: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    # Who asked for a sign-in challenge; the live ones of one address are
    # capped (`passkeys.SIGN_IN_PER_ADDRESS`). Empty for a session's.
    client_address: Mapped[str | None] = mapped_column(Text, index=True)
    # Indexed: every new challenge sweeps the expired ones out first.
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )


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
    # Every attempt prunes by it (#888 S11, op007).
    __table_args__ = (
        Index("ix_operator_login_failures_last_failed_at", "last_failed_at"),
    )

    address: Mapped[str] = mapped_column(Text, primary_key=True)


class Customer(Base):
    """A customer's stack as the dashboard reaches it (docs/23 §8): the address
    of its ops API on that stack's `ops` network and the stack's `OPS_TOKEN`.
    The token is readable, because the dashboard sends it. No two customers
    share an address or a token, so one row can never read another's stack."""

    __tablename__ = "operator_customers"
    __table_args__ = (
        CheckConstraint(
            "key ~ '^[a-z][a-z0-9]*(-[a-z0-9]+)*$'", name="ck_operator_customer_key"
        ),
    )

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    api_url: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    ops_token: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


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


class OperatorPush(Base):
    """A push to the operator's `ops` topic, held until the push server took it
    (#827 F3, docs/23 §5): written in the request that changed a factor and
    sent by the dashboard's sender (`dashboard/push.py`), as a customer's
    outbox is by its dispatcher. An unsent row is never dropped: a push server
    that is down only makes it wait. A sent one goes with the next push after
    `push.SENT_KEEP` (#861)."""

    __tablename__ = "operator_pushes"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
