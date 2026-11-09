"""The operator's one-time setup link (#570, D34 point 5, docs/23 §6).

The operator makes a link; the restaurant opens it and sets its password, the
team PIN and the owner's admin PIN (#570 part b; without one the admin PIN
stays as it was). The link is valid 24 hours, shown once, stored only as a
hash, and a newer link replaces an older one. Setting the password signs out
every device, because a reset usually means a lost tablet or a password that
got around, and signs in the device that used the link.
"""

import secrets
import uuid
from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from api.core.errors import InvalidInput
from api.domain.access.admin import NO_LOGIN, SAME_AS_TEAM, refuse_same_pins
from api.domain.access.hashing import (
    hash_secret,
    password_problem,
    pin_problem,
    token_hash,
)
from api.domain.access.sessions import (
    TOKEN_BYTES,
    Signed,
    audit,
    end_sessions,
    locked_account,
    open_session,
    trust_device,
)
from api.models import GuiAccount, GuiSetupLink

LINK_HOURS = 24
LINK_GONE = "Dieser Link gilt nicht mehr. Bitte beim Betreiber einen neuen anfordern."
ADMIN_PIN_DIGITS = "Die Inhaber-PIN hat 4 bis 6 Ziffern."


def issue_link(
    session: Session, tenant_id: uuid.UUID, actor: str, now: datetime
) -> tuple[str, datetime]:
    """A new setup link for the restaurant: the token (shown once) and its end.

    Creates the restaurant's account on the first call. Commits."""
    # Two first links at once: the second insert waits and does nothing.
    session.execute(
        insert(GuiAccount)
        .values(tenant_id=tenant_id)
        .on_conflict_do_nothing(index_elements=["tenant_id"])
    )
    # The account first, then its links: the order use_link takes too.
    account = locked_account(session, tenant_id)
    assert account is not None
    session.execute(delete(GuiSetupLink).where(GuiSetupLink.account_id == account.id))
    token = secrets.token_urlsafe(TOKEN_BYTES)
    expires_at = now + timedelta(hours=LINK_HOURS)
    session.add(
        GuiSetupLink(
            account_id=account.id,
            token_hash=token_hash(token),
            created_at=now,
            expires_at=expires_at,
        )
    )
    audit(
        session,
        tenant_id,
        actor,
        "gui.setup_link_issued",
        account.id,
        {"expires_at": expires_at.isoformat()},
    )
    session.commit()
    return token, expires_at


def use_link(
    session: Session,
    tenant_id: uuid.UUID,
    token: str,
    password: str,
    pin: str,
    device: str,
    now: datetime,
    device_token: str = "",
    admin_pin: str | None = None,
) -> Signed:
    """Set the password, the team PIN and, when given, the admin PIN through a
    setup link; returns the cookies of this device. Raises InvalidInput with
    the sentence for the page. The link is spent only when every value is
    taken. Commits.

    The slow hash runs before any lock, and the account is locked before
    its link, as in `issue_link`: a link replaced meanwhile is refused cleanly
    instead of the two transactions waiting on each other (review of #585)."""
    if _open_link(session, tenant_id, token, now, lock=False) is None:
        session.rollback()
        raise InvalidInput("setup link invalid", say=LINK_GONE)
    session.rollback()
    problem = password_problem(password) or pin_problem(pin)
    if not problem and admin_pin is not None:
        if pin_problem(admin_pin):
            problem = ADMIN_PIN_DIGITS
        elif admin_pin == pin:
            problem = SAME_AS_TEAM
    if problem:
        raise InvalidInput("setup values refused", say=problem)
    password_hash = hash_secret(password)

    found = _open_link(session, tenant_id, token, now, lock=True)
    if found is None:
        session.rollback()
        raise InvalidInput("setup link replaced or expired", say=LINK_GONE)
    account, link = found
    refuse_same_pins(session, account, pin, admin_pin or account.admin_pin)
    account.password_hash, account.team_pin = password_hash, pin
    if admin_pin is not None:
        account.admin_pin, account.admin_failures = admin_pin, 0
        account.admin_blocked_until = account.admin_last_failed_at = None
        audit(session, tenant_id, "gui:setup", "gui.admin_pin_set", account.id, {})
    account.failures, account.blocked_until, account.last_failed_at = 0, None, None
    session.delete(link)
    end_sessions(session, tenant_id, "gui:setup", "password_set")
    audit(session, tenant_id, "gui:setup", "gui.password_set", account.id, {})
    device_token = trust_device(session, account, device_token, now)
    signed = Signed(open_session(session, account, device, now), device_token)
    session.commit()
    return signed


def _open_link(
    session: Session, tenant_id: uuid.UUID, token: str, now: datetime, lock: bool
) -> tuple[GuiAccount, GuiSetupLink] | None:
    """The restaurant's account and this unexpired link of it, or None."""
    if lock:
        account = locked_account(session, tenant_id)
    else:
        account = session.scalars(
            select(GuiAccount).where(GuiAccount.tenant_id == tenant_id)
        ).first()
    if account is None:
        return None
    query = select(GuiSetupLink).where(
        GuiSetupLink.account_id == account.id,
        GuiSetupLink.token_hash == token_hash(token),
        GuiSetupLink.expires_at > now,
    )
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    link = session.scalars(query).first()
    return (account, link) if link is not None else None


def team_pin(session: Session, tenant_id: uuid.UUID, actor: str) -> str | None:
    """The restaurant's team PIN for the operator, or None. The look is
    audited, never the value. Commits."""
    account = locked_account(session, tenant_id)
    if account is None:
        session.rollback()
        return None
    audit(session, tenant_id, actor, "gui.team_pin_shown", account.id, {})
    session.commit()
    return account.team_pin


def set_team_pin(session: Session, tenant_id: uuid.UUID, pin: str, actor: str) -> None:
    """The operator sets the team PIN; signed-in devices stay signed in and
    unlock with the new one. Raises InvalidInput. Commits."""
    problem = pin_problem(pin)
    if problem:
        raise InvalidInput("team pin refused", say=problem)
    account = locked_account(session, tenant_id)
    if account is None:
        session.rollback()
        raise InvalidInput("no login yet", say=NO_LOGIN)
    refuse_same_pins(session, account, pin, account.admin_pin)
    account.team_pin = pin
    audit(session, tenant_id, actor, "gui.team_pin_set", account.id, {})
    session.commit()
