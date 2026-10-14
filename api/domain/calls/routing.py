"""call_routing: does the agent answer this call, where is the team, and how
long may the call last (docs/02 §4).

Read once at the start of every call. The mode comes fresh from the database, not
from a row this session may have cached: the emergency stop in the tablet header
(`domain/status/config.py`) writes it in another session, and the next call has to
reach the team, not the switched-off agent.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.core.errors import NotFound
from api.models import ServiceConfig, Tenant

# `shadow`: the team takes every call, the agent only listens later (stage 4).
# `paused`: the agent is off. Only in these two modes does it pick up.
AI_ANSWERS = frozenset({"overflow", "primary"})


@dataclass(frozen=True)
class CallRouting:
    ai_answers: bool
    team_phone: str
    tenant_name: str
    # The longest a call with the agent may last before it goes to the team.
    max_call_seconds: int
    # The longest a guest waits in silence for the answer to one sentence
    # before the call goes to the team (#252).
    turn_deadline_seconds: int


def tenant_named(session: Session, name: str) -> uuid.UUID:
    """The tenant a process serves, by the name in its settings (docs/20 §7).
    Names are not unique in the table: two of the same name are an error, not
    a guess."""
    found = session.scalars(select(Tenant.id).where(Tenant.name == name)).all()
    if len(found) != 1:
        # Without the name: it is the restaurant's, and this ends in a log.
        raise NotFound(f"TENANT_NAME names {len(found)} tenants, expected one")
    return found[0]


def call_routing(session: Session, tenant_id: uuid.UUID) -> CallRouting:
    tenant = session.get(Tenant, tenant_id)
    config = session.get(ServiceConfig, tenant_id, populate_existing=True)
    if tenant is None or config is None:
        raise NotFound("Tenant unknown or without service_config")
    return CallRouting(
        ai_answers=config.call_mode in AI_ANSWERS,
        team_phone=config.team_phone,
        tenant_name=tenant.name,
        max_call_seconds=config.max_call_seconds,
        turn_deadline_seconds=config.turn_deadline_seconds,
    )
