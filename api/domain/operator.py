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
step somebody asked for. The step's own row follows when it happened, and
then a row `ops.<step>.outcome` with the id of the ask (`_step`, #811):
`done`, `refused` (a rule or the stack said no) or `failed` (it broke). An ask
without its outcome is a crash that took the process down. No row holds a
PIN or a link, and no `ops.*` row a telephone number (the step's own row
does, as the tablet's).

And the page (#811): every answer carries a version of what it shows
(`Access.version`), and a step that changes something sends back the version
of the page it came from (`seen`). When the restaurant changed since, the
owner on the tablet, the staff or a second operator, the step is refused
before anything happens (`_as_seen`): an old page that offered to switch the
agent on again must not end the owner's newer emergency stop.
"""

import hashlib
import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Literal

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from api.core.errors import AppError, Conflict, InvalidInput
from api.core.ops_reason import RULE_STATUS, STALE, STALE_STATUS, OpsReader
from api.domain.access.admin import set_admin_pin, shown_pin
from api.domain.access.sessions import audit, end_sessions, locked_account
from api.domain.access.setup import issue_link, set_team_pin, setup_url
from api.domain.status.config import ACTION_MODE, pause_ai, resume_ai, set_mode
from api.domain.status.mode import CAUTION, PAUSED, mode_at
from api.domain.status.staff_changes import (
    ACTION_STANDARD,
    ACTION_TEAM_PHONE,
    KEPT,
    TEAM_PHONE,
    UNDONE,
    change_team_phone,
    sheet_numbers,
    team_phone_at,
)
from api.domain.status.staff_changes import (
    set_standard_team_phone as set_standard,
)
from api.models import AuditLog, GuiAccount, GuiSession, ServiceConfig, Tenant

Which = Literal["team", "admin"]
# What the operator sends to end the emergency stop: no mode, the agent goes
# back to the one it had before the pause (`config.resume_ai`).
RESUME = "resume"
# The newest devices. A restaurant has a handful; the dashboard takes no more.
MAX_DEVICES = 200
# And no more numbers to choose from; a dialplan has a few.
MAX_NUMBERS = 20
# The audit rows of a change to what the page shows. A state can come back to
# what a page showed (the agent switched on and stopped again): the newest of
# these rows tells the newer stop from the one on the page. The owner's answer
# on a card counts only for a team number card (`DECIDED`): the page shows no
# opening hours.
MARKS = (
    ACTION_MODE,
    ACTION_TEAM_PHONE,
    ACTION_STANDARD,
    "gui.team_pin_set",
    "gui.admin_pin_set",
    "gui.password_set",
    "gui.setup_link_issued",
)
DECIDED = (f"staff_change.{KEPT}", f"staff_change.{UNDONE}")


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
    # What a step sends back as `seen` (#811); it holds nothing of a secret.
    version: str


def _asked(
    session: Session, tenant: Tenant, by: OpsReader, step: str, **detail: str
) -> int:
    """Who asked for which step and why, committed before the step. The id
    of its row."""
    row = audit(
        session,
        tenant.id,
        by.actor,
        f"ops.{step}",
        tenant.id,
        {"reason": by.reason, "reference": by.reference} | detail,
        entity="tenant",
    )
    session.flush()
    asked = row.id
    session.commit()
    return asked


def _outcome(
    session: Session, tenant: Tenant, by: OpsReader, step: str, ask: int, how: str
) -> None:
    audit(
        session,
        tenant.id,
        by.actor,
        f"ops.{step}.outcome",
        tenant.id,
        {"ask": ask, "outcome": how},
        entity="tenant",
    )
    session.commit()


@contextmanager
def _step(
    session: Session, tenant: Tenant, by: OpsReader, step: str, **detail: str
) -> Iterator[None]:
    """The ask, then the step, then its outcome paired with the ask: the
    step's own row (`gui.team_pin_set`, say) shares only actor and time with
    the ask, and its absence alone does not tell a refusal from a crash."""
    ask = _asked(session, tenant, by, step, **detail)
    try:
        yield
    except AppError:
        session.rollback()
        _outcome(session, tenant, by, step, ask, "refused")
        raise
    except Exception:
        # The step's own error is the one to raise. Should the database be
        # gone too, the ask without its outcome says the same: it broke.
        with suppress(Exception):
            session.rollback()
            _outcome(session, tenant, by, step, ask, "failed")
        raise
    _outcome(session, tenant, by, step, ask, "done")


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
    """The state every answer of a step carries, and its version. Not
    recorded by itself: the step that asks for it is."""
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
    found = Access(
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
        version="",
    )
    return replace(found, version=_version(session, tenant, found))


def _version(session: Session, tenant: Tenant, found: Access) -> str:
    """What the page shows and the newest change to it, as 16 hex digits. A
    device's last use is left out: a tablet in use would make every page old."""
    mark = session.scalar(
        select(func.max(AuditLog.id)).where(
            AuditLog.tenant_id == tenant.id,
            or_(
                AuditLog.action.in_(MARKS),
                and_(
                    AuditLog.action.in_(DECIDED),
                    AuditLog.payload["kind"].astext == TEAM_PHONE,
                ),
            ),
        )
    )
    shown = [
        found.login,
        found.team_pin_set,
        found.admin_pin_set,
        sorted(str(device.id) for device in found.devices),
        found.call_mode,
        found.team_phone,
        found.team_phone_standard,
        # An instant, not its text: the offset read back follows the database.
        found.team_phone_until.timestamp() if found.team_phone_until else None,
        mark,
    ]
    text = json.dumps(shown, default=str)
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _as_seen(session: Session, tenant: Tenant, now: datetime, seen: str) -> None:
    """Refuses a step from a page the restaurant has changed since (#811).
    The settings row and the login are locked first, as the changes of the
    mode, the number and the PINs lock them: such a change waits for the step,
    or the step sees it."""
    session.scalars(
        select(ServiceConfig)
        .where(ServiceConfig.tenant_id == tenant.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()
    locked_account(session, tenant.id)
    if access(session, tenant, now).version != seen:
        raise Conflict(STALE, status=STALE_STATUS)


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
    with _step(session, tenant, by, "pin_show", which=which):
        pin = shown_pin(session, tenant.id, by.actor, which)
    return pin


def set_pin(
    session: Session,
    tenant: Tenant,
    which: Which,
    pin: str,
    now: datetime,
    by: OpsReader,
    *,
    seen: str,
) -> None:
    """Raises InvalidInput for a PIN the rules refuse (4 to 6 digits, never
    the other PIN, a restaurant without a login), Conflict for an old page."""
    with _step(session, tenant, by, "pin_set", which=which), _by_rule():
        _as_seen(session, tenant, now, seen)
        if which == "admin":
            set_admin_pin(session, tenant.id, pin, by.actor)
        else:
            set_team_pin(session, tenant.id, pin, by.actor)


def sign_out(
    session: Session,
    tenant: Tenant,
    device: uuid.UUID | None,
    now: datetime,
    by: OpsReader,
    *,
    seen: str,
) -> int:
    """Sign out one device, or with None every device of the restaurant (the
    route makes sure None was asked for in words). How many were signed out:
    0 for a device that is gone already."""
    with _step(
        session, tenant, by, "sign_out", device=str(device) if device else "all"
    ):
        _as_seen(session, tenant, now, seen)
        ended = end_sessions(session, tenant.id, by.actor, "operator", device)
        session.commit()
    return ended


def setup_link(
    session: Session, tenant: Tenant, now: datetime, by: OpsReader, *, seen: str
) -> tuple[str, datetime]:
    """A new one-time setup link and its end. It replaces an older link and
    creates the restaurant's login the first time (`issue_link`)."""
    with _step(session, tenant, by, "setup_link"):
        _as_seen(session, tenant, now, seen)
        token, expires_at = issue_link(session, tenant.id, by.actor, now)
    return setup_url(token), expires_at


def set_call_mode(
    session: Session,
    tenant: Tenant,
    mode: str,
    now: datetime,
    by: OpsReader,
    *,
    seen: str,
) -> None:
    """A mode of the switch, the emergency stop, or its end. Raises
    InvalidInput for anything else and for a mode chosen during the stop,
    Conflict for an old page; the reason is written first all the same, as for
    every refused step."""
    with _step(session, tenant, by, "call_mode", mode=mode):
        _as_seen(session, tenant, now, seen)
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
    session: Session,
    tenant: Tenant,
    number: str,
    now: datetime,
    by: OpsReader,
    *,
    seen: str,
) -> None:
    """The team number for today, by the staff's rule (`change_team_phone`):
    one of the numbers the telephone system dials, the standard number stays,
    and the owner keeps or undoes it on the tablet's card, which names the
    operator (#811)."""
    with _step(session, tenant, by, "team_phone"), _by_rule():
        _as_seen(session, tenant, now, seen)
        change_team_phone(session, tenant.id, number, now, by.actor)


def set_standard_team_phone(
    session: Session,
    tenant: Tenant,
    number: str,
    now: datetime,
    by: OpsReader,
    *,
    seen: str,
) -> None:
    """The standard team number (#811 question 2), checked as the number for
    today, with a push to the owner and no card (`staff_changes`). A number
    set for today still holds to the end of its day."""
    with _step(session, tenant, by, "team_phone_standard"), _by_rule():
        _as_seen(session, tenant, now, seen)
        set_standard(session, tenant.id, number, now, by.actor)
