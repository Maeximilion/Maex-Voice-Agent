"""call_routing: does the agent answer this call, where is the team, and how
long may the call last (docs/02 §4).

Read once at the start of every call. The mode comes fresh from the database, not
from a row this session may have cached: the emergency stop in the tablet header
(`domain/status/config.py`) writes it in another session, and the next call has to
reach the team, not the switched-off agent.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.core.errors import NotFound
from api.core.time import to_local, utcnow
from api.domain.status.mode import mode_at
from api.models import CallLimitWindow, ServiceConfig, Tenant

# `shadow`: the team takes every call, the agent only listens later (stage 4).
# `paused`: the agent is off. Only in the two modes below does it pick up.
AI_ANSWERS = frozenset({"overflow", "primary"})


@dataclass(frozen=True)
class CallRouting:
    # The mode lets the agent answer; whether it takes this call also depends
    # on the calls it has open (`takes_call`).
    ai_answers: bool
    # How many calls the agent takes at once right now (D16, T-10.9).
    call_limit: int
    team_phone: str
    tenant_name: str
    # The longest a call with the agent may last before it goes to the team.
    max_call_seconds: int
    # The longest a guest waits in silence for the answer to one sentence
    # before the call goes to the team (#252).
    turn_deadline_seconds: int
    # Where the privacy notice is, for the greeting; None until it is set
    # (docs/09 L4, T-11.5).
    privacy_notice_address: str | None


def tenant_named(session: Session, name: str) -> uuid.UUID:
    """The tenant a process serves, by the name in its settings (docs/20 §7).
    Names are not unique in the table: two of the same name are an error, not
    a guess."""
    found = session.scalars(select(Tenant.id).where(Tenant.name == name)).all()
    if len(found) != 1:
        # Without the name: it is the restaurant's, and this ends in a log.
        raise NotFound(f"TENANT_NAME names {len(found)} tenants, expected one")
    return found[0]


def call_routing(
    session: Session, tenant_id: uuid.UUID, now: datetime | None = None
) -> CallRouting:
    tenant = session.get(Tenant, tenant_id)
    config = session.get(ServiceConfig, tenant_id, populate_existing=True)
    if tenant is None or config is None:
        raise NotFound("Tenant unknown or without service_config")
    now = now or utcnow()
    # The mode that holds now, a window of `call_mode_windows` included (T-8.1).
    ai_answers = mode_at(session, tenant, config, now).mode in AI_ANSWERS
    return CallRouting(
        ai_answers=ai_answers,
        # The limit windows only matter when the agent answers; a mode that
        # sends every call to the team needs no read that could fail on the way.
        call_limit=(
            _call_limit(session, tenant, config, now)
            if ai_answers
            else config.max_concurrent_calls
        ),
        team_phone=config.team_phone,
        tenant_name=tenant.name,
        max_call_seconds=config.max_call_seconds,
        turn_deadline_seconds=config.turn_deadline_seconds,
        privacy_notice_address=config.privacy_notice_address,
    )


def _call_limit(
    session: Session, tenant: Tenant, config: ServiceConfig, now: datetime
) -> int:
    """The limit at `now` in the restaurant's clock: the lowest of the windows
    that hold it, else `max_concurrent_calls`."""
    local = to_local(now, tenant.timezone)
    limits = session.scalars(
        select(CallLimitWindow.max_calls).where(
            CallLimitWindow.tenant_id == tenant.id,
            CallLimitWindow.weekday == local.weekday(),
            CallLimitWindow.starts_at <= local.time(),
            CallLimitWindow.ends_at > local.time(),
        )
    ).all()
    return min(limits, default=config.max_concurrent_calls)


def takes_call(routing: CallRouting, open_calls: int) -> bool:
    """Does the agent take a call while `open_calls` of its own are open? A
    call above the limit rings the team (D16, CLAUDE.md §2 rule 5)."""
    return routing.ai_answers and open_calls < routing.call_limit
