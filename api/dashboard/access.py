"""Operator sign-in, sessions and the break-glass reset (#571, docs/23 §5).

A sign-in needs the name, the password and one second factor: a TOTP code, or
one of ten single-use recovery codes. Never the password alone. Passkeys come
next (docs/23 §5, the following pull request).

Every answer to a wrong name, password or factor is the same. Failures count
per account and per client address, or on the browser alone when it brings the
device cookie of an earlier sign-in, with the waits of a customer's login (every
third failure in a row, 1 to 5 minutes; `domain/access/sessions.py`). The
attempt is counted before the slow hash, in a transaction of its own, so a
parallel burst meets the wait.

A session lives 30 minutes from its last request and 12 hours at most. Each
sign-in opens a new one and ends the one the browser brought.
"""

import base64
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import NamedTuple, cast

from sqlalchemy import CursorResult, delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from api.dashboard import totp
from api.dashboard.models import (
    OperatorAccount,
    OperatorAudit,
    OperatorDevice,
    OperatorLoginFailure,
    OperatorRecoveryCode,
    OperatorSession,
)
from api.domain.access.hashing import (
    hash_secret,
    password_problem,
    token_hash,
    verify_secret,
)
from api.domain.access.sessions import FAILURES_FORGOTTEN, count_failure, minutes_left

SESSION_IDLE = timedelta(minutes=30)
SESSION_MAX = timedelta(hours=12)
# last_seen_at moves at most once a minute, not on every request.
TOUCH_EVERY = timedelta(minutes=1)
TOKEN_BYTES = 32
RECOVERY_CODES = 10
# 80 random bits, sixteen base32 letters, shown in four groups.
RECOVERY_BYTES = 10
RECOVERY = re.compile(r"[a-z2-7]{16}")
NAME = re.compile(r"[a-z0-9._-]{2,40}")


class SignInRefused(Exception):
    """Any wrong input, or a wait that still runs (`wait_minutes`)."""

    def __init__(self, wait_minutes: int | None = None):
        super().__init__("sign-in refused")
        self.wait_minutes = wait_minutes


class Signed(NamedTuple):
    """The session and the device cookie of a signed-in browser."""

    token: str
    device_token: str


@dataclass(frozen=True)
class Operator:
    session_id: uuid.UUID
    account_id: uuid.UUID
    name: str


@dataclass(frozen=True)
class Factors:
    """Shown once, on the operator host, never stored readable apart from the
    TOTP secret."""

    totp_secret: str
    recovery_codes: list[str]


def sign_in(
    db: Session,
    name: str,
    password: str,
    code: str,
    address: str,
    now: datetime,
    device_token: str = "",
    previous_token: str = "",
) -> Signed:
    """Check name, password and second factor and open a session. Commits."""
    name = name.strip().lower()
    stored, account_id, trusted = _count_attempt(db, name, address, device_token, now)
    if not verify_secret(password, stored):
        _note_failure(db, name if account_id else None, address, "password")
        raise SignInRefused()
    account = db.get(
        OperatorAccount, account_id, with_for_update=True, populate_existing=True
    )
    # A reset while the password was checked: the old one opens nothing.
    if account is None or account.password_hash != stored:
        db.rollback()
        raise SignInRefused()
    factor = _second_factor(db, account, code, now)
    if factor is None:
        db.rollback()
        _note_failure(db, name, address, "factor")
        raise SignInRefused()
    if not trusted:
        account.failures, account.blocked_until, account.last_failed_at = 0, None, None
        db.execute(
            delete(OperatorLoginFailure).where(OperatorLoginFailure.address == address)
        )
    device_token = _trust_device(db, account, device_token, now)
    if previous_token:
        db.execute(
            delete(OperatorSession).where(
                OperatorSession.token_hash == token_hash(previous_token)
            )
        )
    token = secrets.token_urlsafe(TOKEN_BYTES)
    db.add(
        OperatorSession(
            account_id=account.id,
            token_hash=token_hash(token),
            created_at=now,
            last_seen_at=now,
        )
    )
    if factor == "recovery":
        left = recovery_left(db, account.id)
        audit(db, account.name, "operator.recovery_code_used", {"left": left})
    audit(
        db, account.name, "operator.signed_in", {"factor": factor, "address": address}
    )
    db.commit()
    return Signed(token, device_token)


def resolve(db: Session, token: str, now: datetime) -> Operator | None:
    """The operator behind a session cookie, or None once it ended. Commits
    when it moves last_seen_at or drops an ended session."""
    if not token:
        return None
    found = db.execute(
        select(OperatorSession, OperatorAccount.name)
        .join(OperatorAccount, OperatorAccount.id == OperatorSession.account_id)
        .where(OperatorSession.token_hash == token_hash(token))
    ).first()
    if found is None:
        return None
    row, name = found
    if now - row.last_seen_at >= SESSION_IDLE or now - row.created_at >= SESSION_MAX:
        db.delete(row)
        db.commit()
        return None
    if now - row.last_seen_at >= TOUCH_EVERY:
        row.last_seen_at = now
        db.commit()
    return Operator(row.id, row.account_id, name)


def sign_out(db: Session, operator: Operator) -> None:
    db.execute(delete(OperatorSession).where(OperatorSession.id == operator.session_id))
    audit(db, operator.name, "operator.signed_out", {})
    db.commit()


def recovery_left(db: Session, account_id: uuid.UUID) -> int:
    return (
        db.scalar(
            select(func.count())
            .select_from(OperatorRecoveryCode)
            .where(OperatorRecoveryCode.account_id == account_id)
        )
        or 0
    )


def create_account(db: Session, name: str, password: str) -> Factors:
    """A new operator with a TOTP secret and ten recovery codes. Commits."""
    name = _checked(name, password)
    if db.scalar(select(OperatorAccount.id).where(OperatorAccount.name == name)):
        raise ValueError(f"An account named {name} exists already.")
    account = OperatorAccount(
        name=name, password_hash=hash_secret(password), totp_secret=totp.new_secret()
    )
    db.add(account)
    db.flush()
    codes = _new_recovery_codes(db, account.id)
    audit(db, "cli", "operator.account_created", {"account": name})
    db.commit()
    return Factors(account.totp_secret, codes)


def reset_account(db: Session, name: str, password: str) -> Factors:
    """Break glass, every factor lost (docs/23 §5): a new password, TOTP secret
    and recovery codes, every session and device ended, the counts cleared.
    Run only on the operator host. Commits."""
    name = _checked(name, password)
    account = db.scalars(
        select(OperatorAccount).where(OperatorAccount.name == name).with_for_update()
    ).first()
    if account is None:
        raise LookupError(f"No account named {name}.")
    account.password_hash = hash_secret(password)
    account.totp_secret, account.totp_last_step = totp.new_secret(), None
    account.failures, account.blocked_until, account.last_failed_at = 0, None, None
    for table in (OperatorRecoveryCode, OperatorSession, OperatorDevice):
        db.execute(delete(table).where(table.account_id == account.id))
    codes = _new_recovery_codes(db, account.id)
    audit(db, "cli", "operator.factors_reset", {"account": name})
    db.commit()
    return Factors(account.totp_secret, codes)


def audit(
    db: Session, actor: str, action: str, detail: dict, target: str | None = None
) -> None:
    """One row of the operator's own audit; the caller commits."""
    db.add(OperatorAudit(actor=actor, action=action, target=target, detail=detail))


def _checked(name: str, password: str) -> str:
    name = name.strip().lower()
    if not NAME.fullmatch(name):
        raise ValueError(
            "The name has 2 to 40 characters: a to z, 0 to 9, dot, dash, underscore."
        )
    problem = password_problem(password)
    if problem:
        raise ValueError(problem)
    return name


def _count_attempt(
    db: Session, name: str, address: str, device_token: str, now: datetime
) -> tuple[str | None, uuid.UUID | None, bool]:
    """Refuse while a wait runs, otherwise count one more failure. Returns the
    stored hash, the account and whether a known device was counted. Commits."""
    # Addresses are forgotten a day after their last failure, as their count is.
    db.execute(
        delete(OperatorLoginFailure).where(
            OperatorLoginFailure.last_failed_at < now - FAILURES_FORGOTTEN
        )
    )
    account = db.scalars(
        select(OperatorAccount)
        .where(OperatorAccount.name == name)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()
    device = _device(db, account.id, device_token) if account else None
    counters: list[OperatorAccount | OperatorDevice | OperatorLoginFailure]
    if device is not None:
        counters = [device]
    else:
        # Two failures from a new address at once must not collide on the insert.
        db.execute(
            insert(OperatorLoginFailure)
            .values(address=address, failures=0, last_failed_at=now)
            .on_conflict_do_nothing(index_elements=["address"])
        )
        known = db.get(
            OperatorLoginFailure, address, with_for_update=True, populate_existing=True
        )
        counters = [row for row in (account, known) if row is not None]
    waits = [row.blocked_until for row in counters if row.blocked_until is not None]
    if waits and max(waits) > now:
        db.rollback()
        raise SignInRefused(minutes_left(max(waits) - now))
    for row in counters:
        previous = row.last_failed_at if row.failures else None
        row.failures, row.blocked_until = count_failure(row.failures, previous, now)
        row.last_failed_at = now
    stored = account.password_hash if account else None
    account_id = account.id if account else None
    db.commit()
    return stored, account_id, device is not None


def _note_failure(db: Session, name: str | None, address: str, reason: str) -> None:
    """A failure on a real account goes into the audit; one on an unknown name
    does not, so a spray of names cannot fill it. A right password with a wrong
    factor says the password is known."""
    if name is None:
        return
    audit(
        db,
        "anonymous",
        "operator.sign_in_failed",
        {"account": name, "reason": reason, "address": address},
    )
    db.commit()


def _second_factor(
    db: Session, account: OperatorAccount, code: str, now: datetime
) -> str | None:
    step = totp.matching_step(
        account.totp_secret, code, now.timestamp(), account.totp_last_step
    )
    if step is not None:
        account.totp_last_step = step
        return "totp"
    recovery = re.sub(r"[\s-]", "", code).lower()
    if not RECOVERY.fullmatch(recovery):
        return None
    used = cast(
        CursorResult,
        db.execute(
            delete(OperatorRecoveryCode).where(
                OperatorRecoveryCode.account_id == account.id,
                OperatorRecoveryCode.code_hash == token_hash(recovery),
            )
        ),
    )
    return "recovery" if used.rowcount else None


def _device(
    db: Session, account_id: uuid.UUID, device_token: str
) -> OperatorDevice | None:
    if not device_token:
        return None
    return db.scalars(
        select(OperatorDevice)
        .where(
            OperatorDevice.account_id == account_id,
            OperatorDevice.token_hash == token_hash(device_token),
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()


def _trust_device(
    db: Session, account: OperatorAccount, device_token: str, now: datetime
) -> str:
    """The device cookie of a browser that just signed in: the one it brought
    when it belongs to this account, else a new one. Clears its count."""
    row = _device(db, account.id, device_token)
    if row is None:
        device_token = secrets.token_urlsafe(TOKEN_BYTES)
        row = OperatorDevice(
            account_id=account.id,
            token_hash=token_hash(device_token),
            created_at=now,
            last_used_at=now,
        )
        db.add(row)
    row.failures, row.blocked_until, row.last_failed_at = 0, None, None
    row.last_used_at = now
    return device_token


def _new_recovery_codes(db: Session, account_id: uuid.UUID) -> list[str]:
    codes = []
    for _ in range(RECOVERY_CODES):
        raw = base64.b32encode(secrets.token_bytes(RECOVERY_BYTES)).decode().lower()
        db.add(OperatorRecoveryCode(account_id=account_id, code_hash=token_hash(raw)))
        codes.append("-".join(raw[i : i + 4] for i in range(0, len(raw), 4)))
    return codes
