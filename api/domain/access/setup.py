"""The operator's one-time setup link (#570, D34 point 5, docs/23 §6).

The operator makes a link; the restaurant opens it and sets its password and
team PIN. The link is valid 24 hours, shown once, stored only as a hash, and a
newer link replaces an older one. Setting the password signs out every device,
because a reset usually means a lost tablet or a password that got around, and
signs in the device that used the link.
"""

import secrets
import uuid
from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from api.core.errors import InvalidInput
from api.domain.access.hashing import (
    hash_secret,
    password_problem,
    pin_problem,
    token_hash,
)
from api.domain.access.sessions import TOKEN_BYTES, audit, end_sessions, open_session
from api.models import GuiAccount, GuiSetupLink

LINK_HOURS = 24
LINK_GONE = "Dieser Link gilt nicht mehr. Bitte beim Betreiber einen neuen anfordern."


def issue_link(
    session: Session, tenant_id: uuid.UUID, actor: str, now: datetime
) -> tuple[str, datetime]:
    """A new setup link for the restaurant: the token (shown once) and its end.

    Creates the restaurant's account on the first call. Commits."""
    account = session.scalars(
        select(GuiAccount).where(GuiAccount.tenant_id == tenant_id).with_for_update()
    ).first()
    if account is None:
        account = GuiAccount(tenant_id=tenant_id)
        session.add(account)
        session.flush()
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
) -> str:
    """Set the password and team PIN through a setup link; returns the session
    token of this device. Raises InvalidInput with the sentence for the page.
    The link is spent only when both values are taken. Commits."""
    link = session.scalars(
        select(GuiSetupLink)
        .where(GuiSetupLink.token_hash == token_hash(token))
        .with_for_update()
    ).first()
    account = session.get(GuiAccount, link.account_id) if link else None
    if (
        link is None
        or account is None
        or link.expires_at <= now
        or account.tenant_id != tenant_id
    ):
        session.rollback()
        raise InvalidInput("setup link invalid", say=LINK_GONE)
    problem = password_problem(password) or pin_problem(pin)
    if problem:
        session.rollback()
        raise InvalidInput("setup values refused", say=problem)

    account.password_hash = hash_secret(password)
    account.team_pin_hash = hash_secret(pin)
    account.failed_logins, account.blocked_until = 0, None
    session.delete(link)
    end_sessions(session, tenant_id, "gui:setup", "password_set")
    audit(session, tenant_id, "gui:setup", "gui.password_set", account.id, {})
    token = open_session(session, account, device, now)
    session.commit()
    return token
