"""The operator's steps on a restaurant over the ops API (#571, docs/23 §8).

The dashboard shows and sets both PINs, signs devices out, makes a setup link
and changes the call mode and the team number. None of it is new logic: every
step calls the function the command line (`jobs/gui_access.py`) or the tablet
uses, with the operator as actor, so its rules, its audit row and its push to
the owner and `ops` (T12) are the ones that hold already.

What this module adds is the reason. Before a step is tried, a row
`ops.<step>` with the operator's account, the reason's category and the ticket
number is committed (`_asked`), as for a read of guest data
(`domain/calls/log.py`): a step that fails or is refused afterwards is still a
step somebody asked for. The step's own row follows when it happened. No row
holds a PIN, a link or a telephone number.
"""

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.core.errors import InvalidInput
from api.core.ops_reason import RULE_STATUS, OpsReader
from api.domain.access.admin import set_admin_pin, shown_pin
from api.domain.access.sessions import audit, end_sessions
from api.domain.access.setup import issue_link, set_team_pin, setup_url
from api.domain.status.config import pause_ai, resume_ai, set_mode
from api.domain.status.mode import CAUTION, PAUSED, mode_at
from api.domain.status.staff_changes import (
    change_team_phone,
    sheet_numbers,
    team_phone_at,
)
from api.models import GuiAccount, GuiSession, ServiceConfig, Tenant

Which = Literal["team", "admin"]
# What the operator sends to end the emergency stop: no mode, the agent goes
# back to the one it had before the pause (`config.resume_ai`).
RESUME = "resume"
# The newest devices. A restaurant has a handful; the dashboard takes no more.
MAX_DEVICES = 200
# And no more numbers to choose from; a dialplan has a few.
MAX_NUMBERS = 20


@dataclass(frozen=True)
class Device:
    id: uuid.UUID
    device: str
    created_at: datetime
    last_seen_at: datetime


@dataclass(frozen=True)
class Access:
    """What the operator sees before a step and after it. No secret: whether a
    PIN is set, never the PIN."""

    login: bool
    team_pin_set: bool
    admin_pin_set: bool
    devices: list[Device]
    # The mode that holds now, and what the switch can be set to from it.
    call_mode: str | None
    call_modes: list[str]
    # Where a call for the team goes now, the standard number, when a number
    # set for today ends (None: the standard holds), and what can be chosen.
    team_phone: str | None
    team_phone_standard: str | None
    team_phone_until: datetime | None
    team_phones: list[str]


def _asked(
    session: Session, tenant: Tenant, by: OpsReader, step: str, **detail: str
) -> None:
    """Who asked for which step and why, committed before the step."""
    audit(
        session,
        tenant.id,
        by.actor,
        f"ops.{step}",
        tenant.id,
        {"reason": by.reason, "reference": by.reference} | detail,
        entity="tenant",
    )
    session.commit()


@contextmanager
def _by_rule() -> Iterator[None]:
    """Around the function a step calls: what one of its rules refuses gets a
    status of its own (`RULE_STATUS`), so the dashboard can tell "this PIN is
    the other PIN" from a request the route itself did not take."""
    try:
        yield
    except InvalidInput as refused:
        refused.status = RULE_STATUS
        raise


def access(session: Session, tenant: Tenant, now: datetime) -> Access:
    """The state every answer of a step carries. Not recorded by itself: the
    step that asks for it is."""
    account = session.scalars(
        select(GuiAccount).where(GuiAccount.tenant_id == tenant.id)
    ).first()
    rows = session.scalars(
        select(GuiSession)
        .where(GuiSession.tenant_id == tenant.id)
        .order_by(GuiSession.last_seen_at.desc(), GuiSession.id)
        .limit(MAX_DEVICES)
    ).all()
    config = session.scalars(
        select(ServiceConfig).where(ServiceConfig.tenant_id == tenant.id)
    ).first()
    mode = mode_at(session, tenant, config, now).mode if config else None
    holding = team_phone_at(config, now) if config else None
    # Without its settings row a restaurant has no switch and no number to
    # set: both steps would end in "not found" after their reason was written.
    if config is None:
        modes: list[str] = []
    elif mode == PAUSED:
        modes = [RESUME]
    else:
        modes = [*CAUTION, PAUSED]
    return Access(
        login=account is not None and account.password_hash is not None,
        team_pin_set=bool(account and account.team_pin),
        admin_pin_set=bool(account and account.admin_pin),
        devices=[
            Device(row.id, row.device, row.created_at, row.last_seen_at) for row in rows
        ],
        call_mode=mode,
        call_modes=modes,
        # An empty number (a blank TEAM_PHONE) is no number.
        team_phone=holding or None,
        team_phone_standard=(config.team_phone or None) if config else None,
        team_phone_until=config.team_phone_today_until
        if config and holding != config.team_phone
        else None,
        team_phones=sheet_numbers()[:MAX_NUMBERS] if config else [],
    )


def read_access(
    session: Session, tenant: Tenant, now: datetime, by: OpsReader
) -> Access:
    _asked(session, tenant, by, "access_read")
    return access(session, tenant, now)


def show_pin(
    session: Session, tenant: Tenant, which: Which, by: OpsReader
) -> str | None:
    """The team or admin PIN, or None when none is set. Two rows: the reason
    here, the look itself from the function the command line uses."""
    _asked(session, tenant, by, "pin_show", which=which)
    return shown_pin(session, tenant.id, by.actor, which)


def set_pin(
    session: Session, tenant: Tenant, which: Which, pin: str, by: OpsReader
) -> None:
    """Raises InvalidInput for a PIN the rules refuse (4 to 6 digits, never
    the other PIN, a restaurant without a login)."""
    _asked(session, tenant, by, "pin_set", which=which)
    with _by_rule():
        if which == "admin":
            set_admin_pin(session, tenant.id, pin, by.actor)
        else:
            set_team_pin(session, tenant.id, pin, by.actor)


def sign_out(
    session: Session, tenant: Tenant, device: uuid.UUID | None, by: OpsReader
) -> int:
    """Sign out one device, or with None every device of the restaurant (the
    route makes sure None was asked for in words). How many were signed out:
    0 for a device that is gone already."""
    _asked(session, tenant, by, "sign_out", device=str(device) if device else "all")
    ended = end_sessions(session, tenant.id, by.actor, "operator", device)
    session.commit()
    return ended


def setup_link(
    session: Session, tenant: Tenant, now: datetime, by: OpsReader
) -> tuple[str, datetime]:
    """A new one-time setup link and its end. It replaces an older link and
    creates the restaurant's login the first time (`issue_link`)."""
    _asked(session, tenant, by, "setup_link")
    token, expires_at = issue_link(session, tenant.id, by.actor, now)
    return setup_url(token), expires_at


def set_call_mode(
    session: Session, tenant: Tenant, mode: str, now: datetime, by: OpsReader
) -> None:
    """A mode of the switch, the emergency stop, or its end. Raises
    InvalidInput for anything else and for a mode chosen during the stop; the
    reason is written first all the same, as for every refused step."""
    _asked(session, tenant, by, "call_mode", mode=mode)
    if mode not in (*CAUTION, PAUSED, RESUME):
        raise InvalidInput(f"No mode {mode!r} for the operator", status=400)
    with _by_rule():
        if mode == PAUSED:
            pause_ai(session, tenant.id, by.actor, now)
        elif mode == RESUME:
            resume_ai(session, tenant.id, by.actor, now)
        else:
            set_mode(session, tenant.id, mode, by.actor, now)


def set_team_phone(
    session: Session, tenant: Tenant, number: str, now: datetime, by: OpsReader
) -> None:
    """The team number for today, by the staff's rule (`change_team_phone`):
    one of the numbers the telephone system dials, the standard number stays,
    and the owner keeps or undoes it on the tablet's card."""
    _asked(session, tenant, by, "team_phone")
    with _by_rule():
        change_team_phone(session, tenant.id, number, now, by.actor)
