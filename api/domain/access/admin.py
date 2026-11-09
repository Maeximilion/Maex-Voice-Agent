"""The owner's admin PIN (#570 part b, D34 points 3 and 8, docs/23 §6).

Staff share the restaurant's login and the team PIN. The owner's admin PIN opens
the protected part of the admin area (the device list, and the delivery zones,
fees, minimum order and weekly hours once their pages exist) on one device for
60 minutes. The idle lock closes it early: a device left 15 minutes without a
change is unlocked again with the team PIN, and then holds staff rights only, so
an owner who walks away leaves nothing open. The admin PIN also unlocks a locked
device, as the stronger of the two.

Wrong admin PINs count on the account like wrong passwords: every third in a row
brings a wait one minute longer than the last, up to five, during which no PIN
is checked. They never end the session, the team PIN's rule: the board stays
usable for the team, the admin PIN guards the owner's settings only, and a
lost tablet is signed out by the operator (Maxi, 09.11.2026, review of #587).
Stored readable like the team PIN (Maxi 09.11.2026): the operator sees and
sets it. It never equals the team PIN.
"""

import hmac
import uuid
from datetime import datetime, timedelta

from sqlalchemy import update
from sqlalchemy.orm import Session

from api.core.errors import InvalidInput, Unauthorized
from api.domain.access.hashing import pin_problem
from api.domain.access.sessions import (
    audit,
    count_failure,
    locked_account,
    minutes_left,
)
from api.models import GuiAccount, GuiSession

ADMIN_FOR = timedelta(minutes=60)
SAME_AS_TEAM = "Die Inhaber-PIN muss sich von der Team-PIN unterscheiden."
NO_LOGIN = "Noch kein Zugang: erst einen Link machen."


class AdminRefused(Unauthorized):
    """A wrong admin PIN, a wait that still runs (`wait_minutes`), or no admin
    PIN set yet (`missing`)."""

    def __init__(self, wait_minutes: int | None = None, missing: bool = False):
        super().__init__("admin pin refused")
        self.wait_minutes = wait_minutes
        self.missing = missing


def unlock_admin(
    session: Session, session_id: uuid.UUID, pin: str, now: datetime
) -> None:
    """Check the admin PIN on a signed-in device; right, it holds owner rights
    for 60 minutes and is unlocked. Raises AdminRefused. Commits.

    The account is locked before the session row, the order `use_link` and
    `end_sessions` take, so parallel tries count one after the other."""
    found = session.get(GuiSession, session_id)
    account = locked_account(session, found.tenant_id) if found else None
    row = session.get(
        GuiSession, session_id, with_for_update=True, populate_existing=True
    )
    if account is None or row is None or row.account_id != account.id:
        session.rollback()
        raise AdminRefused()
    if not account.admin_pin:
        session.rollback()
        raise AdminRefused(missing=True)
    # Read before the rollback, which expires the row (review of #587).
    blocked = account.admin_blocked_until
    if blocked is not None and blocked > now:
        session.rollback()
        raise AdminRefused(minutes_left(blocked - now))
    if hmac.compare_digest(pin.encode(), account.admin_pin.encode()):
        account.admin_failures = 0
        account.admin_blocked_until = account.admin_last_failed_at = None
        row.admin_until, row.last_active_at, row.pin_failures = now + ADMIN_FOR, now, 0
        audit(
            session,
            row.tenant_id,
            "gui:owner",
            "gui.admin_unlocked",
            row.id,
            {"device": row.device},
            entity="gui_session",
        )
        session.commit()
        return
    previous = account.admin_last_failed_at if account.admin_failures else None
    account.admin_failures, account.admin_blocked_until = count_failure(
        account.admin_failures, previous, now
    )
    account.admin_last_failed_at = now
    audit(
        session,
        row.tenant_id,
        "gui:tablet",
        "gui.admin_pin_wrong",
        row.id,
        {"device": row.device, "failures": account.admin_failures},
        entity="gui_session",
    )
    blocked = account.admin_blocked_until
    session.commit()
    raise AdminRefused(minutes_left(blocked - now) if blocked else None)


def close_admin(session: Session, session_id: uuid.UUID) -> None:
    """The owner is done: the device keeps staff rights only. Commits."""
    row = session.get(GuiSession, session_id)
    if row is not None:
        row.admin_until = None
    session.commit()


def admin_pin(session: Session, tenant_id: uuid.UUID, actor: str) -> str | None:
    """The owner's admin PIN for the operator, or None. The look is audited,
    never the value. Commits."""
    account = locked_account(session, tenant_id)
    if account is None:
        session.rollback()
        return None
    audit(session, tenant_id, actor, "gui.admin_pin_shown", account.id, {})
    session.commit()
    return account.admin_pin


def set_admin_pin(session: Session, tenant_id: uuid.UUID, pin: str, actor: str) -> None:
    """The operator sets the admin PIN. A new PIN is often one that leaked: it
    starts with no wait, and owner rights still open on any device end (review
    of #587). Raises InvalidInput. Commits."""
    problem = pin_problem(pin)
    if problem:
        raise InvalidInput("admin pin refused", say=problem)
    account = locked_account(session, tenant_id)
    if account is None:
        session.rollback()
        raise InvalidInput("no login yet", say=NO_LOGIN)
    refuse_same_pins(session, account, account.team_pin, pin)
    account.admin_pin, account.admin_failures = pin, 0
    account.admin_blocked_until = account.admin_last_failed_at = None
    session.execute(
        update(GuiSession)
        .where(GuiSession.tenant_id == tenant_id)
        .values(admin_until=None)
    )
    audit(session, tenant_id, actor, "gui.admin_pin_set", account.id, {})
    session.commit()


def refuse_same_pins(
    session: Session, account: GuiAccount, team_pin: str | None, admin: str | None
) -> None:
    """The staff know the team PIN, so it can never open owner rights. Rolls
    back and raises InvalidInput when the two would be equal."""
    if team_pin and admin and hmac.compare_digest(team_pin.encode(), admin.encode()):
        session.rollback()
        raise InvalidInput("admin pin equals team pin", say=SAME_AS_TEAM)
