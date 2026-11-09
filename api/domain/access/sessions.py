"""Sign-in, device sessions, the idle lock and the team PIN (#570, D34).

One login per restaurant. The password signs a device in once; its session
slides for 30 days on use. Fifteen minutes after the last sign-in, PIN or change
the session is locked: the board keeps showing, the next change asks the team
PIN, and five wrong PINs end the session (D34, Maxi 09.11.2026).

Failed sign-ins are counted per account and per client address. From the fifth
in a row each one adds a growing wait (1, 2, 4, 8, then 15 minutes) instead of a
hard lock, so a guesser cannot lock the restaurant out for good, and while a
wait runs no password is even checked (docs/23 §5, §6). A day without a failure
starts the count again.
"""

import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from api.core.errors import Unauthorized
from api.domain.access.hashing import token_hash, verify_secret
from api.models import AuditLog, GuiAccount, GuiLoginFailure, GuiSession, Tenant

SESSION_DAYS = 30
IDLE = timedelta(minutes=15)
PIN_TRIES = 5
DELAY_FROM = 5
DELAY_MAX_MINUTES = 15
FAILURES_FORGOTTEN = timedelta(days=1)
# last_seen and last_active move at most once a minute, not on every request.
TOUCH_EVERY = timedelta(minutes=1)
DEVICE_CHARS = 120
TOKEN_BYTES = 32


@dataclass(frozen=True)
class Access:
    session_id: uuid.UUID
    tenant_id: uuid.UUID
    locked: bool


class SignInRefused(Unauthorized):
    """A wrong password, or a wait that is still running (`wait_minutes`)."""

    def __init__(self, wait_minutes: int | None = None):
        super().__init__("sign-in refused")
        self.wait_minutes = wait_minutes


def stack_tenant_id(session: Session) -> uuid.UUID | None:
    """The restaurant of this stack: one tenant per stack (D32), the oldest, as
    the operations view has always read it."""
    return session.scalars(select(Tenant.id).order_by(Tenant.created_at)).first()


def sign_in(
    session: Session,
    tenant_id: uuid.UUID,
    password: str,
    address: str,
    device: str,
    now: datetime,
) -> str:
    """Check the restaurant's password and open a session for this device.

    Returns the session token for the cookie; only its hash is stored. Raises
    SignInRefused, with the minutes to wait while a wait runs. The attempt is
    counted as a failure before the password is checked, in a transaction of
    its own: a burst of parallel guesses meets the wait from its fifth on, and
    no row lock or pooled connection is held during the slow hash. A right
    password takes the count back. Commits."""
    stored = _count_attempt(session, tenant_id, address, now)
    if not verify_secret(password, stored):
        raise SignInRefused()
    account = session.scalars(
        select(GuiAccount)
        .where(GuiAccount.tenant_id == tenant_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one()
    account.failed_logins, account.blocked_until = 0, None
    account.last_failed_at = None
    session.execute(delete(GuiLoginFailure).where(GuiLoginFailure.address == address))
    token = open_session(session, account, device, now)
    audit(session, account.tenant_id, "gui:tablet", "gui.signed_in", account.id, {})
    session.commit()
    return token


def _count_attempt(
    session: Session, tenant_id: uuid.UUID, address: str, now: datetime
) -> str | None:
    """Refuse while a wait runs, otherwise count one more failure for the
    account and the address and return the stored hash. Commits."""
    account = session.scalars(
        select(GuiAccount)
        .where(GuiAccount.tenant_id == tenant_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()
    # Two failures from a new address at once must not collide on the insert.
    session.execute(
        insert(GuiLoginFailure)
        .values(address=address, failures=0, last_failed_at=now)
        .on_conflict_do_nothing(index_elements=["address"])
    )
    known = session.get(
        GuiLoginFailure, address, with_for_update=True, populate_existing=True
    )
    assert known is not None
    waits = [
        row.blocked_until
        for row in (account, known)
        if row is not None and row.blocked_until is not None
    ]
    if waits and max(waits) > now:
        session.rollback()
        raise SignInRefused(_minutes(max(waits) - now))
    if account is not None:
        account.failed_logins, account.blocked_until = _count(
            account.failed_logins, account.last_failed_at, now
        )
        account.last_failed_at = now
    previous = known.last_failed_at if known.failures else None
    known.failures, known.blocked_until = _count(known.failures, previous, now)
    known.last_failed_at = now
    stored = account.password_hash if account else None
    session.commit()
    return stored


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
    session: Session, token: str, tenant_id: uuid.UUID | None, now: datetime
) -> Access | None:
    """The session behind a cookie, or None when there is none for this stack.

    A session of another tenant is not valid here, even if its row were found:
    the stack serves one tenant (docs/23 §6). Moves `last_seen_at`, which slides
    the 30 days, and commits that."""
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
    if now - row.last_seen_at >= TOUCH_EVERY:
        row.last_seen_at = now
        session.commit()
    return Access(row.id, row.tenant_id, locked=now - row.last_active_at >= IDLE)


def mark_active(session: Session, session_id: uuid.UUID, now: datetime) -> None:
    """A change on an unlocked device: the 15 minutes start again."""
    result = session.execute(
        update(GuiSession)
        .where(
            GuiSession.id == session_id,
            GuiSession.last_active_at <= now - TOUCH_EVERY,
        )
        .values(last_active_at=now)
    )
    if result.rowcount:
        session.commit()


def unlock(
    session: Session, session_id: uuid.UUID, pin: str, now: datetime
) -> int | None:
    """Check the team PIN on a locked device.

    None when it was right (the device is unlocked); otherwise the tries left,
    0 when the fifth wrong PIN ended the session. The try is counted before the
    PIN is checked, as in `sign_in`, so parallel tries cannot pass five.
    Commits."""
    row = session.get(
        GuiSession, session_id, with_for_update=True, populate_existing=True
    )
    if row is None or row.pin_failures >= PIN_TRIES:
        session.rollback()
        return 0
    account = session.get(GuiAccount, row.account_id)
    stored = account.team_pin_hash if account else None
    row.pin_failures += 1
    tries = row.pin_failures
    session.commit()

    if verify_secret(pin, stored):
        result = session.execute(
            update(GuiSession)
            .where(GuiSession.id == session_id)
            .values(pin_failures=0, last_active_at=now)
        )
        session.commit()
        # Ended meanwhile by a fifth wrong PIN in parallel: stays signed out.
        return None if result.rowcount else 0
    if tries < PIN_TRIES:
        return PIN_TRIES - tries
    row = session.get(
        GuiSession, session_id, with_for_update=True, populate_existing=True
    )
    if row is not None:
        _end(session, row, "gui:tablet", "pin_failures")
    session.commit()
    return 0


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


def _count(
    failures: int, last_failed_at: datetime | None, now: datetime
) -> tuple[int, datetime | None]:
    """The failure count after one more, and the wait it brings."""
    if last_failed_at is None or now - last_failed_at >= FAILURES_FORGOTTEN:
        failures = 0
    failures += 1
    if failures < DELAY_FROM:
        return failures, None
    minutes = min(2 ** (failures - DELAY_FROM), DELAY_MAX_MINUTES)
    return failures, now + timedelta(minutes=minutes)


def _minutes(left: timedelta) -> int:
    return max(1, -(-int(left.total_seconds()) // 60))


def audit(
    session: Session,
    tenant_id: uuid.UUID,
    actor: str,
    action: str,
    entity_id: uuid.UUID,
    payload: dict,
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
