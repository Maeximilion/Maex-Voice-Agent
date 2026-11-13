"""Sign-in, device sessions, the idle lock and the team PIN (#570, D34).

One login per restaurant. The password signs a device in once; its session
slides for 30 days on use. Fifteen minutes after the last sign-in, PIN or change
the session is locked: the board keeps showing, the next change asks the team
PIN, and five wrong PINs end the session (D34, Maxi 09.11.2026).

Failed sign-ins are counted per account and per client address. One or two
wrong passwords cost nothing; every third one in a row brings a wait that grows
by a minute each time, 1, 2, 3, 4, then 5 minutes at most (Maxi, 09.11.2026),
instead of a hard lock, and while a wait runs no password is even checked. A
day without a failure starts the count again. A browser that signed in before
brings its device cookie: its attempts count on that device alone, so a guesser
who keeps the account's wait running cannot keep the restaurant's own tablets
out (OWASP device cookies; review of #585).
"""

import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, NamedTuple, cast

from sqlalchemy import CursorResult, delete, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from api.core.errors import InvalidInput, Unauthorized
from api.domain.access.hashing import same_secret, token_hash, verify_secret
from api.models import (
    AuditLog,
    GuiAccount,
    GuiDevice,
    GuiLoginFailure,
    GuiSession,
    Tenant,
)

SESSION_DAYS = 30
IDLE = timedelta(minutes=15)
PIN_TRIES = 5
# Every third wrong password in a row waits, one minute longer each time.
WAIT_EVERY = 3
WAIT_MAX_MINUTES = 5
FAILURES_FORGOTTEN = timedelta(days=1)
# last_seen and last_active move at most once a minute, not on every request.
TOUCH_EVERY = timedelta(minutes=1)
DEVICE_CHARS = 120
TOKEN_BYTES = 32
NO_TEAM_PIN = "Noch keine Team-PIN. Der Betreiber setzt sie."
# A device cookie nobody signed in with for this long is forgotten (the most a
# browser keeps a cookie).
DEVICE_DAYS = 400


@dataclass(frozen=True)
class Access:
    session_id: uuid.UUID
    locked: bool
    # The owner's admin PIN holds on this device (`domain/access/admin.py`).
    admin: bool
    # The once-a-minute touch ran: the cookies are due again (#598).
    slid: bool = False


class SignInRefused(Unauthorized):
    """A wrong password, or a wait that is still running (`wait_minutes`)."""

    def __init__(self, wait_minutes: int | None = None):
        super().__init__("sign-in refused")
        self.wait_minutes = wait_minutes


def stack_tenant_id(session: Session) -> uuid.UUID | None:
    """The restaurant of this stack: one tenant per stack (D32), the oldest, as
    the operations view has always read it."""
    return session.scalars(select(Tenant.id).order_by(Tenant.created_at)).first()


class Signed(NamedTuple):
    """The two cookies of a signed-in browser."""

    token: str
    device_token: str


def sign_in(
    session: Session,
    tenant_id: uuid.UUID,
    password: str,
    address: str,
    device: str,
    now: datetime,
    device_token: str = "",
) -> Signed:
    """Check the restaurant's password and open a session for this device.

    Returns the session token and the device token for the cookies; only their
    hashes are stored. Raises SignInRefused, with the minutes to wait while a
    wait runs. The attempt is counted as a failure before the password is
    checked, in a transaction of its own: a burst of parallel guesses meets the
    wait from its third on, and no row lock or pooled connection is held during
    the slow hash. A right password takes the count back. Commits."""
    stored, trusted = _count_attempt(session, tenant_id, address, device_token, now)
    if not verify_secret(password, stored):
        raise SignInRefused()
    account = locked_account(session, tenant_id)
    # A new password set while this one was checked: the old one opens nothing.
    if account is None or account.password_hash != stored:
        session.rollback()
        raise SignInRefused()
    if not trusted:
        account.failures, account.blocked_until, account.last_failed_at = 0, None, None
        session.execute(
            delete(GuiLoginFailure).where(GuiLoginFailure.address == address)
        )
    device_token = trust_device(session, account, device_token, now)
    token = open_session(session, account, device, now)
    audit(session, account.tenant_id, "gui:tablet", "gui.signed_in", account.id, {})
    session.commit()
    return Signed(token, device_token)


def trust_device(
    session: Session, account: GuiAccount, device_token: str, now: datetime
) -> str:
    """The device cookie of a browser that just signed in: the one it brought
    when it belongs to this account, else a new one. Clears that device's
    count. The caller commits."""
    row = _device(session, account, device_token)
    if row is None:
        device_token = secrets.token_urlsafe(TOKEN_BYTES)
        row = GuiDevice(
            account_id=account.id, token_hash=token_hash(device_token), created_at=now
        )
        session.add(row)
    row.failures, row.blocked_until, row.last_failed_at = 0, None, None
    row.last_used_at = now
    return device_token


def _count_attempt(
    session: Session,
    tenant_id: uuid.UUID,
    address: str,
    device_token: str,
    now: datetime,
) -> tuple[str | None, bool]:
    """Refuse while a wait runs, otherwise count one more failure and return
    the stored hash and whether a known device was counted. A known device is
    counted alone, anything else per account and per address. Commits."""
    account = locked_account(session, tenant_id)
    device = _device(session, account, device_token) if account else None
    counters: list[GuiAccount | GuiDevice | GuiLoginFailure]
    if device is not None:
        counters = [device]
    else:
        # Two failures from a new address at once must not collide on the insert.
        session.execute(
            insert(GuiLoginFailure)
            .values(address=address, failures=0, last_failed_at=now)
            .on_conflict_do_nothing(index_elements=["address"])
        )
        known = session.get(
            GuiLoginFailure, address, with_for_update=True, populate_existing=True
        )
        counters = [row for row in (account, known) if row is not None]
    waits = [row.blocked_until for row in counters if row.blocked_until is not None]
    if waits and max(waits) > now:
        session.rollback()
        raise SignInRefused(minutes_left(max(waits) - now))
    for row in counters:
        row.failures, row.blocked_until, row.last_failed_at = count_failure(
            row.failures, row.last_failed_at, now
        )
    stored = account.password_hash if account else None
    session.commit()
    return stored, device is not None


def locked_account(session: Session, tenant_id: uuid.UUID) -> GuiAccount | None:
    return session.scalars(
        select(GuiAccount)
        .where(GuiAccount.tenant_id == tenant_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()


def _device(
    session: Session, account: GuiAccount, device_token: str
) -> GuiDevice | None:
    if not device_token:
        return None
    return session.scalars(
        select(GuiDevice)
        .where(
            GuiDevice.account_id == account.id,
            GuiDevice.token_hash == token_hash(device_token),
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()


def open_session(
    session: Session, account: GuiAccount, device: str, now: datetime
) -> str:
    """A new device session for `account`; the caller commits."""
    token = secrets.token_urlsafe(TOKEN_BYTES)
    session.add(
        GuiSession(
            account_id=account.id,
            tenant_id=account.tenant_id,
            token_hash=token_hash(token),
            device=device[:DEVICE_CHARS],
            created_at=now,
            last_seen_at=now,
            last_active_at=now,
        )
    )
    return token


def resolve(
    session: Session,
    token: str,
    tenant_id: uuid.UUID | None,
    now: datetime,
    device_token: str = "",
) -> Access | None:
    """The session behind a cookie, or None when there is none for this stack.

    A session of another tenant is not valid here, even if its row were found:
    the stack serves one tenant (docs/23 §6). Moves `last_seen_at`, which slides
    the 30 days, and with it the device's 400 (`last_used_at` of the device
    cookie `device_token`), and commits that; `slid` then tells the caller to
    set both cookies again, whose browser lifetime starts anew."""
    if not token or tenant_id is None:
        return None
    row = session.scalars(
        select(GuiSession).where(GuiSession.token_hash == token_hash(token))
    ).first()
    if row is None or row.tenant_id != tenant_id:
        return None
    if row.last_seen_at <= now - timedelta(days=SESSION_DAYS):
        session.delete(row)
        session.commit()
        return None
    slid = now - row.last_seen_at >= TOUCH_EVERY
    if slid:
        row.last_seen_at = now
        if device_token:
            session.execute(
                update(GuiDevice)
                .where(
                    GuiDevice.account_id == row.account_id,
                    GuiDevice.token_hash == token_hash(device_token),
                )
                .values(last_used_at=now)
            )
        session.commit()
    locked = now - row.last_active_at >= IDLE
    # The idle lock closes owner rights too: after the team PIN, staff rights only.
    admin = not locked and row.admin_until is not None and row.admin_until > now
    return Access(row.id, locked=locked, admin=admin, slid=slid)


def mark_active(session: Session, session_id: uuid.UUID, now: datetime) -> None:
    """A change on an unlocked device: the 15 minutes start again."""
    # An UPDATE answers a CursorResult, the one that carries rowcount.
    result = cast(
        CursorResult[*tuple[Any, ...]],
        session.execute(
            update(GuiSession)
            .where(
                GuiSession.id == session_id,
                GuiSession.last_active_at <= now - TOUCH_EVERY,
            )
            .values(last_active_at=now)
        ),
    )
    if result.rowcount:
        session.commit()


def unlock(
    session: Session, session_id: uuid.UUID, pin: str, now: datetime
) -> int | None:
    """Check the team PIN on a locked device.

    None when it was right (the device is unlocked); otherwise the tries left,
    0 when the fifth wrong PIN ended the session. The session row stays locked
    for the check, so parallel tries cannot pass five. Without a team PIN
    yet, InvalidInput and no try counts. Commits."""
    row = session.get(
        GuiSession, session_id, with_for_update=True, populate_existing=True
    )
    if row is None:
        session.rollback()
        return 0
    account = session.get(GuiAccount, row.account_id)
    stored = account.team_pin if account else None
    if not stored:
        # Migration 031 leaves this until the operator sets one: no try counts.
        session.rollback()
        raise InvalidInput("no team pin yet", say=NO_TEAM_PIN)
    if same_secret(pin, stored):
        # The team PIN opens staff rights: an owner's hour left open ends here.
        row.pin_failures, row.last_active_at, row.admin_until = 0, now, None
        session.commit()
        return None
    row.pin_failures += 1
    left = max(PIN_TRIES - row.pin_failures, 0)
    if not left:
        _end(session, row, "gui:tablet", "pin_failures")
    session.commit()
    return left


def end_sessions(
    session: Session,
    tenant_id: uuid.UUID,
    actor: str,
    reason: str,
    session_id: uuid.UUID | None = None,
) -> int:
    """Sign out one device, or every device of the restaurant. The caller commits."""
    query = select(GuiSession).where(GuiSession.tenant_id == tenant_id)
    if session_id is not None:
        query = query.where(GuiSession.id == session_id)
    rows = session.scalars(query.with_for_update()).all()
    for row in rows:
        _end(session, row, actor, reason)
    return len(rows)


def devices(session: Session, tenant_id: uuid.UUID) -> list[GuiSession]:
    return list(
        session.scalars(
            select(GuiSession)
            .where(GuiSession.tenant_id == tenant_id)
            .order_by(GuiSession.last_seen_at.desc())
        )
    )


def _end(session: Session, row: GuiSession, actor: str, reason: str) -> None:
    audit(
        session,
        row.tenant_id,
        actor,
        "gui.session_ended",
        row.id,
        {"reason": reason, "device": row.device},
        entity="gui_session",
    )
    session.delete(row)


def count_failure(
    failures: int, last_failed_at: datetime | None, now: datetime
) -> tuple[int, datetime | None, datetime]:
    """One more failure: the new count, the wait it brings and the time to keep
    as the last failure. The one step every failure counter takes."""
    if last_failed_at is None or now - last_failed_at >= FAILURES_FORGOTTEN:
        failures = 0
    failures += 1
    if failures % WAIT_EVERY:
        return failures, None, now
    minutes = min(failures // WAIT_EVERY, WAIT_MAX_MINUTES)
    return failures, now + timedelta(minutes=minutes), now


def minutes_left(left: timedelta) -> int:
    return max(1, -(-int(left.total_seconds()) // 60))


def audit(
    session: Session,
    tenant_id: uuid.UUID,
    actor: str,
    action: str,
    entity_id: uuid.UUID,
    payload: dict[str, Any],
    entity: str = "gui_account",
) -> None:
    session.add(
        AuditLog(
            tenant_id=tenant_id,
            actor=actor,
            action=action,
            entity=entity,
            entity_id=entity_id,
            payload=payload,
        )
    )
