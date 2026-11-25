"""Operator sign-in, step-up, factors and the break-glass reset (#571,
docs/23 §5).

A sign-in needs the name, the password and one second factor: a passkey
(`passkeys.py`), a TOTP code, or one of ten single-use recovery codes. Never
the password alone. The passkey is the way in; a code is the way back in when
no passkey is at hand, and each sign-in or step-up with one is pushed to the
operator's phone (#888 A1, Maxi 23.11.2026; the pushes are `app.py`'s).

Dangerous actions need a second factor of the last five minutes on the
session (`Operator.fresh`): the one of the sign-in, or a step-up with a passkey
or a TOTP code. Once the account has a passkey only the passkey counts: a sign-in
with a code opens nothing dangerous and the step-up takes no code (Maxi,
23.11.2026). Changing the factors is such an action, and once the account
has a passkey it needs a passkey step-up of the last five minutes
(`Operator.fresh_for_factors`, D37). An account always
keeps TOTP or two passkeys, so one lost device never locks the operator out.

Every answer to a wrong name, password or factor is the same. Failures count
per account and per client address, or on the browser alone when it brings the
device cookie of an earlier sign-in (at the step-up on the account too, #888
A3), with the waits of a customer's login (every
third failure in a row, 1 to 5 minutes; `domain/access/sessions.py`); an IPv6
address also counts in its /48, with a wait every 30th failure (#962). A
sign-in or step-up with a passkey that holds counts on that passkey alone and
skips the other waits, so a guesser who knows only the account name cannot
keep the operator out (#990); one behind the operator's own address can still
fill its cap of sign-in challenges (docs/23 §5, #827 F1). The
attempt is counted before the slow hash, in a transaction of its own, so a
parallel burst meets the wait.

A session lives 30 minutes from its last request and 12 hours at most. Each
sign-in opens a new one and ends the one the browser brought.
"""

import base64
import ipaddress
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
from api.domain.access.sessions import (
    FAILURES_FORGOTTEN,
    WAIT_EVERY,
    count_failure,
    counted_address,
    minutes_left,
)

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
# A right password with a wrong factor is pushed once in this time, so a thief
# with the password cannot flood the operator's phone (#888 A2).
KNOWN_PASSWORD_ALARM_EVERY = timedelta(hours=1)
# An account without a passkey uses its code every day: its code is pushed
# once in this time, not on each use (#990, Maxi 24.11.2026).
CODE_PUSH_EVERY = timedelta(days=1)
# Every this many wrong step-ups in a row on an account are pushed (#888 A2).
STEP_UP_ALARM_EVERY = 3
# An IPv6 /64 also counts in its /48, which a home line (/56) or a server
# (/48) holds whole, with a wait every this many failures in a row: a spray of
# made-up names reaches the hash 30 times, then at most 30 times per 5 minutes
# (#962). A known device counts alone, so it never meets this wait.
BLOCK_PREFIX = 48
BLOCK_WAIT_EVERY = 30
TOKEN_BYTES = 32
RECOVERY_CODES = 10
# 80 random bits, sixteen base32 letters, shown in four groups.
RECOVERY_BYTES = 10
RECOVERY = re.compile(r"[a-z2-7]{16}")
NAME = re.compile(r"[a-z0-9._-]{2,40}")


class SignInRefused(Exception):
    """Any wrong input, or a wait that still runs (`wait_minutes`). `copy`
    says that a passkey of the account answered with a counter that did not
    grow: the browser hears nothing of it, the operator must. `alarm` says
    that someone knows the password or holds a session and guesses codes: a
    right password with a wrong factor, the first in an hour, or every third
    wrong step-up in a row (#888 A2)."""

    def __init__(
        self, wait_minutes: int | None = None, copy: bool = False, alarm: bool = False
    ):
        super().__init__("sign-in refused")
        self.wait_minutes = wait_minutes
        self.copy = copy
        self.alarm = alarm


class PasskeyNeeded(Exception):
    """D37 under the account's row: the account has a passkey by now, and this
    session showed none on the step-up in the last five minutes."""


class FactorNeeded(Exception):
    """The change would leave the account with neither TOTP nor two passkeys."""


class Signed(NamedTuple):
    """The session and the device cookie of a signed-in browser, and the
    second factor that opened it."""

    token: str
    device_token: str
    factor: str
    # A browser without this account's device cookie (#888 A1).
    new_browser: bool
    # A TOTP code from a known browser whose push is due (`_code_push_due`).
    push_code: bool


@dataclass(frozen=True)
class Operator:
    session_id: uuid.UUID
    account_id: uuid.UUID
    name: str
    stepped_up_at: datetime | None = None
    passkey_stepped_up_at: datetime | None = None

    def fresh(self, now: datetime) -> bool:
        """Did this session show a second factor in the last five minutes?"""
        return _within(self.stepped_up_at, now)

    def fresh_for_factors(self, now: datetime, has_passkey: bool) -> bool:
        """May this session change the account's factors (D37)? Once the
        account has a passkey, only a passkey step-up of the last five minutes
        counts: not the sign-in, not a TOTP code. Without one, any fresh
        second factor does, as for every other dangerous action."""
        if has_passkey:
            return _within(self.passkey_stepped_up_at, now)
        return self.fresh(now)


def _within(at: datetime | None, now: datetime) -> bool:
    return at is not None and timedelta(0) <= now - at < STEP_UP_FRESH


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
    # The counters' key; the audit and the double tap keep the address as sent.
    counted = counted_address(address)
    # Taken before anything is counted or looked at: a challenge is spent by
    # the first post that brings it, whatever else that post got wrong.
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
    stored, account_id, trusted, held = _count_attempt(
        db, name, counted, device_token, now, passkey=passkey, challenge=challenge
    )
    if not verify_secret(password, stored):
        _note_failure(db, name if account_id else None, address, "password", now)
        raise SignInRefused()
    account = db.get(
        OperatorAccount, account_id, with_for_update=True, populate_existing=True
    )
    # A reset while the password was checked: the old one opens nothing.
    if account is None or account.password_hash != stored:
        db.rollback()
        raise SignInRefused()
    # Checked again under the account's row, and only now recorded: a passkey
    # removed meanwhile, or a counter another answer moved, opens nothing.
    factor, why = _second_factor(db, account, code, now, passkey, challenge)
    if factor is None:
        repeat = not passkey and _just_signed_in(db, account, code, address, now)
        db.rollback()
        reason = "repeat" if repeat else why
        _hold(db, account.id)
        alarm = reason == "factor" and _known_password_alarm_due(db, name, now)
        _note_failure(db, name, address, reason, now, alarm)
        raise SignInRefused(copy=why == "counter", alarm=alarm)
    if held is not None:
        # Counted alone, like a known device: the account's and the address's
        # failures may be a guesser's, so their waits stay as they are.
        _forget_passkey_count(db, held)
    elif not trusted:
        _forgive(db, account, counted)
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
            # The sign-in is itself a fresh second factor, but a code on an
            # account with a passkey is only the way back in (Maxi, 23.11.2026).
            stepped_up_at=(
                now
                if factor == "passkey" or not passkeys.count(db, account.id)
                else None
            ),
        )
    )
    if factor == "recovery":
        left = recovery_left(db, account.id)
        audit(db, account.name, "operator.recovery_code_used", {"left": left})
    # A new browser's push names the code itself.
    push_code = factor == "totp" and trusted and _code_push_due(db, account, now)
    _audit_code(db, account.name, "operator.signed_in", factor, address, push_code, now)
    db.commit()
    return Signed(token, device_token, factor, not trusted, push_code)


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
    return Operator(
        row.id, row.account_id, name, row.stepped_up_at, row.passkey_stepped_up_at
    )


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
) -> bool:
    """A fresh second factor on a signed-in session (docs/23 §5): a passkey or
    a TOTP code. A recovery code is a way in, not a step-up. Wrong answers
    count and wait as at the sign-in, or a stolen session could guess codes.
    Returns whether its code is to be pushed (`_code_push_due`); a passkey
    step-up is not. Commits."""
    # No factor at all says nothing: not counted, not audited (#816).
    if not passkey and not code.strip():
        raise SignInRefused()
    # Once the account has a passkey, only the passkey makes a session fresh
    # (Maxi, 23.11.2026): a code is not looked at, so not counted either.
    if not passkey and passkeys.count(db, operator.account_id):
        raise PasskeyNeeded()
    # With the device cookie the account counts too, or a holder of both
    # would get two wait ladders, one with the cookie and one without (#888 A3).
    counted = counted_address(address)
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
    # A passkey that holds counts alone here too, or a guesser's account wait
    # would refuse the step-up every factor change needs (#990).
    _, _, trusted, held = _count_attempt(
        db,
        operator.name,
        counted,
        device_token,
        now,
        account_too=True,
        passkey=passkey,
        challenge=challenge,
    )
    account = _locked(db, operator)
    # Counted again under the row a registration holds too: a first passkey
    # committed since the check above makes the code no fresh factor either.
    if not passkey and passkeys.count(db, account.id):
        db.rollback()
        raise PasskeyNeeded()
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
        _hold(db, account.id)
        alarm = (_failed_step_ups(db, operator.name) + 1) % STEP_UP_ALARM_EVERY == 0
        audit(
            db,
            operator.name,
            "operator.step_up_failed",
            {"reason": why, "address": address},
        )
        db.commit()
        raise SignInRefused(copy=why == "counter", alarm=alarm)
    if trusted:
        # The browser's own count; the account's stays to a guesser's waits
        # when a passkey held, as at the sign-in.
        device = _device(db, account.id, device_token)
        if device is not None:
            device.failures, device.blocked_until, device.last_failed_at = 0, None, None
    if held is not None:
        _forget_passkey_count(db, held)
    elif trusted:
        account.failures, account.blocked_until, account.last_failed_at = 0, None, None
    else:
        _forgive(db, account, counted)
    session.stepped_up_at = now
    if factor == "passkey":
        session.passkey_stepped_up_at = now
    push_code = factor != "passkey" and _code_push_due(db, account, now)
    _audit_code(db, operator.name, "operator.stepped_up", factor, None, push_code, now)
    db.commit()
    return push_code


def add_passkey(
    db: Session, operator: Operator, answer: str, name: str, now: datetime
) -> str:
    """Register the passkey a ceremony on this session made. Returns its
    name; raises `passkeys.Refused`. Commits."""
    row = passkeys.register(
        db,
        operator.account_id,
        operator.session_id,
        answer,
        name,
        now,
        passkey_step_up=operator.fresh_for_factors(now, has_passkey=True),
    )
    audit(db, operator.name, "operator.passkey_added", {"passkey": row.name})
    db.commit()
    return row.name


def remove_passkey(
    db: Session, operator: Operator, passkey_id: str, now: datetime, device_token: str
) -> tuple[str, int]:
    """Returns the removed passkey's name and how many other sessions ended
    (`_end_other_sessions`). Raises `LookupError` for one that is not this
    account's and `FactorNeeded` when it must stay. Commits."""
    account = _locked(db, operator, now)
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
    ended = _end_other_sessions(db, operator, device_token)
    db.commit()
    return row.name, ended


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


def confirm_totp(
    db: Session, operator: Operator, code: str, now: datetime, device_token: str
) -> int | None:
    """Put the proposed secret in force once a code of it came back, so a
    secret that never reached the app replaces nothing. Returns how many other
    sessions ended, None for a wrong code. Commits."""
    account = _locked(db, operator, now)
    session = db.get(OperatorSession, operator.session_id, with_for_update=True)
    secret = session.pending_totp if session is not None else None
    step = totp.matching_step(secret, code, now.timestamp(), None) if secret else None
    if session is None or step is None:
        db.rollback()
        return None
    account.totp_secret, account.totp_last_step = secret, step
    session.pending_totp = None
    audit(db, operator.name, "operator.totp_set", {})
    ended = _end_other_sessions(db, operator, device_token)
    db.commit()
    return ended


def remove_totp(
    db: Session, operator: Operator, now: datetime, device_token: str
) -> int | None:
    """Returns how many other sessions ended; None when TOTP was off already
    (a double post, an older tab): nothing to audit or push. Raises
    `FactorNeeded` without two passkeys. Commits."""
    account = _locked(db, operator, now)
    if passkeys.count(db, account.id) < PASSKEYS_WITHOUT_TOTP:
        db.rollback()
        raise FactorNeeded()
    if account.totp_secret is None:
        db.rollback()
        return None
    account.totp_secret, account.totp_last_step = None, None
    audit(db, operator.name, "operator.totp_removed", {})
    ended = _end_other_sessions(db, operator, device_token)
    db.commit()
    return ended


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


def _locked(
    db: Session, operator: Operator, now: datetime | None = None
) -> OperatorAccount:
    """The operator's account, held until the commit: two changes of its
    factors at once must not each see the other's factor as still there. A
    change that waited here while another one ended its session (D39, the
    break glass) is refused: it changes no factor and ends nobody's session.
    With `now`, a factor change: D37 is checked again under the row, so a
    first passkey another session registered after the route's check counts
    (`PasskeyNeeded`)."""
    account = db.get(
        OperatorAccount,
        operator.account_id,
        with_for_update=True,
        populate_existing=True,
    )
    alive = db.scalar(
        select(OperatorSession.id).where(OperatorSession.id == operator.session_id)
    )
    if account is None or alive is None:
        db.rollback()
        raise SignInRefused()
    if now is not None and not operator.fresh_for_factors(
        now, passkeys.count(db, account.id) > 0
    ):
        db.rollback()
        raise PasskeyNeeded()
    return account


def _end_other_sessions(db: Session, operator: Operator, device_token: str) -> int:
    """D39 (Maxi, 17.11.2026): a removed factor or a new TOTP secret is what
    one does after a loss, so every other session of the account ends with it;
    a thief's session must not outlive the change. The session making it stays.
    So do the device cookies: every other browser's is forgotten, or a thief's
    would go on guessing codes for the new secret on a wait ladder of its own
    (#888 B1, S12); the browser making the change keeps its own. Returns how
    many sessions ended; the caller holds the account's row and commits."""
    db.execute(
        delete(OperatorDevice).where(
            OperatorDevice.account_id == operator.account_id,
            OperatorDevice.token_hash != token_hash(device_token),
        )
    )
    ended = cast(
        CursorResult[*tuple[Any, ...]],
        db.execute(
            delete(OperatorSession).where(
                OperatorSession.account_id == operator.account_id,
                OperatorSession.id != operator.session_id,
            )
        ),
    ).rowcount
    if ended:
        audit(db, operator.name, "operator.other_sessions_ended", {"ended": ended})
    return ended


def _forgive(db: Session, account: OperatorAccount, address: str) -> None:
    account.failures, account.blocked_until, account.last_failed_at = 0, None, None
    db.execute(
        delete(OperatorLoginFailure).where(
            OperatorLoginFailure.address.in_(
                [key for key in (address, _block(address)) if key]
            )
        )
    )


def _block(address: str) -> str | None:
    """The /48 an IPv6 counted address (`x::/64`) lies in; None for anything
    else, an IPv4 address or a key that is no address."""
    try:
        network = ipaddress.ip_network(address)
    except ValueError:
        return None
    if network.version != 6 or network.prefixlen != 64:
        return None
    return str(network.supernet(new_prefix=BLOCK_PREFIX))


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
    db: Session,
    name: str,
    address: str,
    device_token: str,
    now: datetime,
    account_too: bool = False,
    passkey: str = "",
    challenge: bytes | None = None,
) -> tuple[str | None, uuid.UUID | None, bool, uuid.UUID | None]:
    """Refuse while a wait runs, otherwise count one more failure. Returns the
    stored hash, the account, whether a known device was counted and the
    passkey whose `passkey` answer to `challenge` holds, if any. A passkey
    that holds counts alone and skips every other wait (Maxi, #990): it cannot
    be guessed, so who knows only the account name can no longer keep the
    operator out with three wrong passwords every five minutes (#927). It is
    only checked here, not recorded: the caller checks it again once the
    password held. Otherwise a known device counts alone, or with the account
    when `account_too`, and a passkey that does not hold counts as if no
    passkey had come. Commits."""
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
    held = None
    if passkey and account:
        # Under the account's row, the order a passkey removal takes too.
        try:
            held = passkeys.verify(
                db, account.id, challenge, passkey, now, record=False
            ).id
        except passkeys.Refused:
            held = None
    counters: list[OperatorAccount | OperatorDevice | OperatorLoginFailure]
    block = None
    if held is not None:
        counters = [_counter(db, _passkey_key(held), now)]
    elif device is not None:
        counters = [device, account] if account_too and account else [device]
    else:
        counters = [row for row in (account, _counter(db, address, now)) if row]
        # Every attempt of the block counts, a known name too, or its wait
        # would tell which names exist.
        block_key = _block(address)
        if block_key:
            block = _counter(db, block_key, now)
            counters.append(block)
        if account is None:
            # An unknown name waits like a known one, or the wait would tell
            # which names exist (#688, docs/23 §5). Its counter sits beside the
            # addresses under a key no address can have, forgotten the same way.
            # The hash, not the name: any length fits the index (Codex PR #731).
            counters.append(_counter(db, f"name:{token_hash(name)}", now))
    waits = [row.blocked_until for row in counters if row.blocked_until is not None]
    if waits and max(waits) > now:
        db.rollback()
        raise SignInRefused(minutes_left(max(waits) - now))
    for row in counters:
        row.failures, row.blocked_until, row.last_failed_at = count_failure(
            row.failures,
            row.last_failed_at,
            now,
            BLOCK_WAIT_EVERY if row is block else WAIT_EVERY,
        )
    stored = account.password_hash if account else None
    account_id = account.id if account else None
    db.commit()
    return stored, account_id, device is not None, held


def _passkey_key(passkey_id: uuid.UUID) -> str:
    """The failure row of a passkey that held, beside the addresses under a key
    no address can have, forgotten the same way."""
    return f"passkey:{passkey_id}"


def _forget_passkey_count(db: Session, passkey_id: uuid.UUID) -> None:
    db.execute(
        delete(OperatorLoginFailure).where(
            OperatorLoginFailure.address == _passkey_key(passkey_id)
        )
    )


def _counter(db: Session, key: str, now: datetime) -> OperatorLoginFailure:
    """The failure row of an address, a block or a name, held until the commit."""
    # Two failures under a new key at once must not collide on the insert.
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
    db: Session, account: OperatorAccount, code: str, address: str, now: datetime
) -> bool:
    """Is `code` the one this account signed in with a moment ago, from this
    address? Then a second post of the same form (a double tap) is no sign of
    a known password (#688). The same code from another address is the race a
    password thief runs against the operator, so it stays a `factor` failure
    (review #207). Not the device cookie: a double tap from a new browser
    sends both posts before the first answer sets it."""
    if account.totp_secret is None:
        return False
    step = totp.matching_step(account.totp_secret, code, now.timestamp(), None)
    if step is None or step != account.totp_last_step:
        return False
    if not db.scalar(
        select(func.count())
        .select_from(OperatorSession)
        .where(
            OperatorSession.account_id == account.id,
            OperatorSession.created_at >= now - REPEAT_WINDOW,
        )
    ):
        return False
    # The last row that moved totp_last_step names who used this code: a TOTP
    # sign-in, a TOTP step-up or a new secret (a recovery code leaves the step
    # as it was). Only a sign-in from this very address is a double tap.
    last = db.execute(
        select(OperatorAudit.action, OperatorAudit.detail["address"].astext)
        .where(
            OperatorAudit.actor == account.name,
            (
                OperatorAudit.action.in_(("operator.signed_in", "operator.stepped_up"))
                & (OperatorAudit.detail["factor"].astext == "totp")
            )
            | (OperatorAudit.action == "operator.totp_set"),
        )
        .order_by(OperatorAudit.id.desc())
        .limit(1)
    ).first()
    return (
        bool(address)
        and last is not None
        and tuple(last) == ("operator.signed_in", address)
    )


def _note_failure(
    db: Session,
    name: str | None,
    address: str,
    reason: str,
    now: datetime,
    alarm: bool = False,
) -> None:
    """A failure on a real account goes into the audit; one on an unknown name
    does not, so a spray of names cannot fill it. A right password with a wrong
    factor says the password is known. Stamped with the sign-in's clock, and
    marked when it was pushed, which `_known_password_alarm_due` reads."""
    if name is None:
        return
    detail: dict[str, Any] = {"account": name, "reason": reason, "address": address}
    if alarm:
        detail["alarm"] = True
    db.add(
        OperatorAudit(
            at=now, actor="anonymous", action="operator.sign_in_failed", detail=detail
        )
    )
    db.commit()


def _hold(db: Session, account_id: uuid.UUID) -> None:
    """The account's row, held until the caller's commit: a failure's alarm is
    decided and its audit row written in one go, so failures at once cannot all
    count the same rows and miss their alarm (Codex on #933)."""
    db.get(OperatorAccount, account_id, with_for_update=True, populate_existing=True)


def _known_password_alarm_due(db: Session, name: str, now: datetime) -> bool:
    """No known-password alarm on this account in the last hour: this failure
    is pushed (#888 A2). Measured from the last alarm, not the last failure, so
    a guess every few minutes cannot keep the alarm quiet for good."""
    return not db.scalar(
        select(func.count())
        .select_from(OperatorAudit)
        .where(
            OperatorAudit.action == "operator.sign_in_failed",
            OperatorAudit.detail["account"].astext == name,
            OperatorAudit.detail["alarm"].astext == "true",
            OperatorAudit.at > now - KNOWN_PASSWORD_ALARM_EVERY,
        )
    )


def _code_push_due(db: Session, account: OperatorAccount, now: datetime) -> bool:
    """A sign-in or step-up with a TOTP code is pushed (#888 A1). On an account
    with a passkey the code is only the way back in, so each use is; on one
    without, once in `CODE_PUSH_EVERY` (#990), measured from the last push and
    not the last use, so daily use cannot keep it quiet for good. Read under
    the account's row, held by the caller: two at once cannot both miss it."""
    if passkeys.count(db, account.id):
        return True
    return not db.scalar(
        select(func.count())
        .select_from(OperatorAudit)
        .where(
            OperatorAudit.actor == account.name,
            OperatorAudit.action.in_(("operator.signed_in", "operator.stepped_up")),
            OperatorAudit.detail["pushed"].astext == "true",
            OperatorAudit.at > now - CODE_PUSH_EVERY,
        )
    )


def _audit_code(
    db: Session,
    name: str,
    action: str,
    factor: str,
    address: str | None,
    pushed: bool,
    now: datetime,
) -> None:
    """The audit row of a sign-in or step-up. One whose code is pushed is
    marked and stamped with the request's clock, which `_code_push_due`
    reads, as `_note_failure` stamps a failure."""
    detail: dict[str, Any] = {"factor": factor}
    if address is not None:
        detail["address"] = address
    if pushed:
        detail["pushed"] = True
    row = OperatorAudit(actor=name, action=action, detail=detail)
    if pushed:
        row.at = now
    db.add(row)


def _failed_step_ups(db: Session, name: str) -> int:
    """Wrong step-ups on this account since its last sign-in or step-up."""
    last_in = db.scalar(
        select(func.max(OperatorAudit.id)).where(
            OperatorAudit.actor == name,
            OperatorAudit.action.in_(("operator.signed_in", "operator.stepped_up")),
        )
    )
    return (
        db.scalar(
            select(func.count())
            .select_from(OperatorAudit)
            .where(
                OperatorAudit.actor == name,
                OperatorAudit.action == "operator.step_up_failed",
                OperatorAudit.id > (last_in or 0),
            )
        )
        or 0
    )


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
