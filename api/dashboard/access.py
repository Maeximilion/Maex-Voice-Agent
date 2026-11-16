"""Operator sign-in, step-up, factors and the break-glass reset (#571,
docs/23 §5).

A sign-in needs the name, the password and one second factor: a passkey
(`passkeys.py`), a TOTP code, or one of ten single-use recovery codes. Never
the password alone.

Dangerous actions need a second factor of the last five minutes on the
session (`Operator.fresh`): the one of the sign-in, or a step-up with a passkey
or a TOTP code. Changing the factors is such an action. An account always
keeps TOTP or two passkeys, so one lost device never locks the operator out.

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
from typing import Any, NamedTuple, cast

from sqlalchemy import CursorResult, delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from api.dashboard import passkeys, totp
from api.dashboard.models import (
    OperatorAccount,
    OperatorAudit,
    OperatorDevice,
    OperatorLoginFailure,
    OperatorPasskey,
    OperatorRecoveryCode,
    OperatorSession,
)
from api.domain.access.hashing import (
    PASSWORD_MAX,
    PASSWORD_MIN,
    hash_secret,
    password_problem,
    token_hash,
    verify_secret,
)
from api.domain.access.sessions import FAILURES_FORGOTTEN, count_failure, minutes_left

SESSION_IDLE = timedelta(minutes=30)
SESSION_MAX = timedelta(hours=12)
# How long a second factor counts as fresh for a dangerous action.
STEP_UP_FRESH = timedelta(minutes=5)
# Without TOTP an account keeps at least this many passkeys.
PASSKEYS_WITHOUT_TOTP = 2
# last_seen_at moves at most once a minute, not on every request.
TOUCH_EVERY = timedelta(minutes=1)
# A sign-in this recent with the same code makes a refused second post a
# double tap, not a known password (#688).
REPEAT_WINDOW = timedelta(minutes=2)
TOKEN_BYTES = 32
RECOVERY_CODES = 10
# 80 random bits, sixteen base32 letters, shown in four groups.
RECOVERY_BYTES = 10
RECOVERY = re.compile(r"[a-z2-7]{16}")
NAME = re.compile(r"[a-z0-9._-]{2,40}")


class SignInRefused(Exception):
    """Any wrong input, or a wait that still runs (`wait_minutes`). `copy`
    says that a passkey of the account answered with a counter that did not
    grow: the browser hears nothing of it, the operator must."""

    def __init__(self, wait_minutes: int | None = None, copy: bool = False):
        super().__init__("sign-in refused")
        self.wait_minutes = wait_minutes
        self.copy = copy


class FactorNeeded(Exception):
    """The change would leave the account with neither TOTP nor two passkeys."""


class Signed(NamedTuple):
    """The session and the device cookie of a signed-in browser, and the
    second factor that opened it."""

    token: str
    device_token: str
    factor: str


@dataclass(frozen=True)
class Operator:
    session_id: uuid.UUID
    account_id: uuid.UUID
    name: str
    stepped_up_at: datetime | None = None

    def fresh(self, now: datetime) -> bool:
        """Did this session show a second factor in the last five minutes?"""
        return (
            self.stepped_up_at is not None
            and timedelta(0) <= now - self.stepped_up_at < STEP_UP_FRESH
        )


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
    passkey: str = "",
    attempt_token: str = "",
) -> Signed:
    """Check name, password and second factor and open a session. `passkey`
    is the answer of a WebAuthn ceremony to the challenge this browser asked
    for under `attempt_token`; with it, `code` is not looked at. Commits."""
    name = name.strip().lower()
    # Enter after the password: no factor at all. Refused before the password is
    # looked at, so it tells nothing, and neither counted nor audited (#816).
    if not passkey and not code.strip():
        raise SignInRefused()
    stored, account_id, trusted = _count_attempt(db, name, address, device_token, now)
    # Taken before the password is looked at: a challenge is spent by the first
    # post that brings it, whatever else that post got wrong.
    challenge = (
        passkeys.take(
            db,
            passkeys.for_attempt(attempt_token),
            passkeys.SIGN_IN,
            now,
            passkeys.signed_challenge(passkey),
        )
        if passkey and attempt_token
        else None
    )
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
    factor, why = _second_factor(db, account, code, now, passkey, challenge)
    if factor is None:
        repeat = not passkey and _just_signed_in(db, account, code, now)
        db.rollback()
        _note_failure(db, name, address, "repeat" if repeat else why)
        raise SignInRefused(copy=why == "counter")
    if not trusted:
        _forgive(db, account, address)
    device_token = _trust_device(db, account, device_token, now)
    # The browser's own session, and every ended one of the account that no
    # browser came back for: otherwise their rows would stay for good.
    db.execute(
        delete(OperatorSession).where(
            (OperatorSession.token_hash == token_hash(previous_token))
            | (
                (OperatorSession.account_id == account.id)
                & (
                    (OperatorSession.last_seen_at <= now - SESSION_IDLE)
                    | (OperatorSession.created_at <= now - SESSION_MAX)
                )
            )
        )
    )
    token = secrets.token_urlsafe(TOKEN_BYTES)
    db.add(
        OperatorSession(
            account_id=account.id,
            token_hash=token_hash(token),
            created_at=now,
            last_seen_at=now,
            # The sign-in is itself a fresh second factor.
            stepped_up_at=now,
        )
    )
    if factor == "recovery":
        left = recovery_left(db, account.id)
        audit(db, account.name, "operator.recovery_code_used", {"left": left})
    audit(
        db, account.name, "operator.signed_in", {"factor": factor, "address": address}
    )
    db.commit()
    return Signed(token, device_token, factor)


def resolve(
    db: Session, token: str, now: datetime, touch: bool = True
) -> Operator | None:
    """The operator behind a session cookie, or None once it ended. Commits
    when it moves last_seen_at or drops an ended session. `touch=False` is for
    a request the page sends by itself (the list's refresh): it must not count
    as the operator being there, or an open tab would never reach the idle
    limit."""
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
    if touch and now - row.last_seen_at >= TOUCH_EVERY:
        row.last_seen_at = now
        db.commit()
    return Operator(row.id, row.account_id, name, row.stepped_up_at)


def sign_out(db: Session, operator: Operator) -> None:
    db.execute(delete(OperatorSession).where(OperatorSession.id == operator.session_id))
    audit(db, operator.name, "operator.signed_out", {})
    db.commit()


def step_up(
    db: Session,
    operator: Operator,
    code: str,
    address: str,
    now: datetime,
    device_token: str = "",
    passkey: str = "",
) -> None:
    """A fresh second factor on a signed-in session (docs/23 §5): a passkey or
    a TOTP code. A recovery code is a way in, not a step-up. Wrong answers
    count and wait as at the sign-in, or a stolen session could guess codes.
    Commits."""
    # No factor at all says nothing: not counted, not audited (#816).
    if not passkey and not code.strip():
        raise SignInRefused()
    _, _, trusted = _count_attempt(db, operator.name, address, device_token, now)
    challenge = (
        passkeys.take(
            db,
            passkeys.for_session(operator.session_id),
            passkeys.STEP_UP,
            now,
            passkeys.signed_challenge(passkey),
        )
        if passkey
        else None
    )
    account = _locked(db, operator)
    session = db.get(
        OperatorSession,
        operator.session_id,
        with_for_update=True,
        populate_existing=True,
    )
    if session is None:
        db.rollback()
        raise SignInRefused()
    factor, why = _second_factor(
        db, account, code, now, passkey, challenge, recovery=False
    )
    if factor is None:
        db.rollback()
        audit(
            db,
            operator.name,
            "operator.step_up_failed",
            {"reason": why, "address": address},
        )
        db.commit()
        raise SignInRefused(copy=why == "counter")
    if trusted:
        device = _device(db, account.id, device_token)
        if device is not None:
            device.failures, device.blocked_until, device.last_failed_at = 0, None, None
    else:
        _forgive(db, account, address)
    session.stepped_up_at = now
    audit(db, operator.name, "operator.stepped_up", {"factor": factor})
    db.commit()


def add_passkey(
    db: Session, operator: Operator, answer: str, name: str, now: datetime
) -> str:
    """Register the passkey a ceremony on this session made. Returns its
    name; raises `passkeys.Refused`. Commits."""
    row = passkeys.register(
        db, operator.account_id, operator.session_id, answer, name, now
    )
    audit(db, operator.name, "operator.passkey_added", {"passkey": row.name})
    db.commit()
    return row.name


def remove_passkey(db: Session, operator: Operator, passkey_id: str) -> str:
    """Returns the removed passkey's name. Raises `LookupError` for one that
    is not this account's and `FactorNeeded` when it must stay. Commits."""
    account = _locked(db, operator)
    try:
        wanted = uuid.UUID(passkey_id)
    except ValueError:
        wanted = None
    row = db.get(OperatorPasskey, wanted) if wanted else None
    if row is None or row.account_id != account.id:
        db.rollback()
        raise LookupError("no such passkey")
    if (
        account.totp_secret is None
        and passkeys.count(db, account.id) <= PASSKEYS_WITHOUT_TOTP
    ):
        db.rollback()
        raise FactorNeeded()
    db.delete(row)
    audit(db, operator.name, "operator.passkey_removed", {"passkey": row.name})
    db.commit()
    return row.name


def propose_totp(db: Session, operator: Operator) -> None:
    """A new TOTP secret for the operator to scan, kept on the session and
    not in force until `confirm_totp`. Commits."""
    session = db.get(OperatorSession, operator.session_id, with_for_update=True)
    if session is not None:
        session.pending_totp = totp.new_secret()
    db.commit()


def pending_totp(db: Session, operator: Operator) -> str | None:
    return db.scalar(
        select(OperatorSession.pending_totp).where(
            OperatorSession.id == operator.session_id
        )
    )


def confirm_totp(db: Session, operator: Operator, code: str, now: datetime) -> bool:
    """Put the proposed secret in force once a code of it came back, so a
    secret that never reached the app replaces nothing. Commits."""
    account = _locked(db, operator)
    session = db.get(OperatorSession, operator.session_id, with_for_update=True)
    secret = session.pending_totp if session is not None else None
    step = totp.matching_step(secret, code, now.timestamp(), None) if secret else None
    if session is None or step is None:
        db.rollback()
        return False
    account.totp_secret, account.totp_last_step = secret, step
    session.pending_totp = None
    audit(db, operator.name, "operator.totp_set", {})
    db.commit()
    return True


def remove_totp(db: Session, operator: Operator) -> bool:
    """Raises `FactorNeeded` without two passkeys. False when TOTP was off
    already (a double post, an older tab): nothing to audit or push. Commits."""
    account = _locked(db, operator)
    if passkeys.count(db, account.id) < PASSKEYS_WITHOUT_TOTP:
        db.rollback()
        raise FactorNeeded()
    if account.totp_secret is None:
        db.rollback()
        return False
    account.totp_secret, account.totp_last_step = None, None
    audit(db, operator.name, "operator.totp_removed", {})
    db.commit()
    return True


def has_totp(db: Session, account_id: uuid.UUID) -> bool:
    return bool(
        db.scalar(
            select(OperatorAccount.totp_secret.is_not(None)).where(
                OperatorAccount.id == account_id
            )
        )
    )


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
    assert account.totp_secret is not None
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
    for table in (
        OperatorRecoveryCode,
        OperatorSession,
        OperatorDevice,
        OperatorPasskey,
    ):
        db.execute(delete(table).where(table.account_id == account.id))
    codes = _new_recovery_codes(db, account.id)
    audit(db, "cli", "operator.factors_reset", {"account": name})
    db.commit()
    return Factors(account.totp_secret, codes)


def audit(
    db: Session,
    actor: str,
    action: str,
    detail: dict[str, Any],
    target: str | None = None,
) -> None:
    """One row of the operator's own audit; the caller commits."""
    db.add(OperatorAudit(actor=actor, action=action, target=target, detail=detail))


def _locked(db: Session, operator: Operator) -> OperatorAccount:
    """The operator's account, held until the commit: two changes of its
    factors at once must not each see the other's factor as still there."""
    account = db.get(
        OperatorAccount,
        operator.account_id,
        with_for_update=True,
        populate_existing=True,
    )
    if account is None:
        db.rollback()
        raise SignInRefused()
    return account


def _forgive(db: Session, account: OperatorAccount, address: str) -> None:
    account.failures, account.blocked_until, account.last_failed_at = 0, None, None
    db.execute(
        delete(OperatorLoginFailure).where(OperatorLoginFailure.address == address)
    )


def _checked(name: str, password: str) -> str:
    name = name.strip().lower()
    # `operator:cli` is the command line's actor in a stack's audit log, and
    # `cli` in this one: an account of that name would write as either.
    if name == "cli":
        raise ValueError("The name cli is the command line's. Choose another one.")
    if not NAME.fullmatch(name):
        raise ValueError(
            "The name has 2 to 40 characters: a to z, 0 to 9, dot, dash, underscore."
        )
    # The restaurant's rule; its messages are German for the team, these are
    # the operator's.
    if not PASSWORD_MIN <= len(password) <= PASSWORD_MAX:
        raise ValueError(
            f"The password has {PASSWORD_MIN} to {PASSWORD_MAX} characters."
        )
    if password_problem(password):
        raise ValueError("This password is too easy to guess. Choose another one.")
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
        if account is None:
            # An unknown name waits like a known one, or the wait would tell
            # which names exist (#688, docs/23 §5). Its counter sits beside the
            # addresses under a key no address can have, forgotten the same way.
            counters.append(_name_counter(db, name, now))
    waits = [row.blocked_until for row in counters if row.blocked_until is not None]
    if waits and max(waits) > now:
        db.rollback()
        raise SignInRefused(minutes_left(max(waits) - now))
    for row in counters:
        row.failures, row.blocked_until, row.last_failed_at = count_failure(
            row.failures, row.last_failed_at, now
        )
    stored = account.password_hash if account else None
    account_id = account.id if account else None
    db.commit()
    return stored, account_id, device is not None


def _name_counter(db: Session, name: str, now: datetime) -> OperatorLoginFailure:
    # The hash, not the name: any length fits the index (Codex PR #731).
    key = f"name:{token_hash(name)}"
    db.execute(
        insert(OperatorLoginFailure)
        .values(address=key, failures=0, last_failed_at=now)
        .on_conflict_do_nothing(index_elements=["address"])
    )
    row = db.get(
        OperatorLoginFailure, key, with_for_update=True, populate_existing=True
    )
    assert row is not None
    return row


def _just_signed_in(
    db: Session, account: OperatorAccount, code: str, now: datetime
) -> bool:
    """Is `code` the one this account signed in with a moment ago? Then a
    second post of the same form (a double tap) is no sign of a known
    password (#688)."""
    if account.totp_secret is None:
        return False
    step = totp.matching_step(account.totp_secret, code, now.timestamp(), None)
    if step is None or step != account.totp_last_step:
        return False
    return bool(
        db.scalar(
            select(func.count())
            .select_from(OperatorSession)
            .where(
                OperatorSession.account_id == account.id,
                OperatorSession.created_at >= now - REPEAT_WINDOW,
            )
        )
    )


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
    db: Session,
    account: OperatorAccount,
    code: str,
    now: datetime,
    passkey: str = "",
    challenge: bytes | None = None,
    recovery: bool = True,
) -> tuple[str | None, str]:
    """The factor that held, or None and the audit's word for why not."""
    if passkey:
        try:
            passkeys.verify(db, account.id, challenge, passkey, now)
        except passkeys.Refused as refused:
            return None, refused.reason
        return "passkey", ""
    if account.totp_secret is not None:
        step = totp.matching_step(
            account.totp_secret, code, now.timestamp(), account.totp_last_step
        )
        if step is not None:
            account.totp_last_step = step
            return "totp", ""
    typed = re.sub(r"[\s-]", "", code).lower()
    if not recovery or not RECOVERY.fullmatch(typed):
        return None, "factor"
    used = cast(
        CursorResult[*tuple[Any, ...]],
        db.execute(
            delete(OperatorRecoveryCode).where(
                OperatorRecoveryCode.account_id == account.id,
                OperatorRecoveryCode.code_hash == token_hash(typed),
            )
        ),
    )
    return ("recovery", "") if used.rowcount else (None, "factor")


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
