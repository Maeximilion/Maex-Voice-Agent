"""call_routing: does the agent answer this call, and where is the team (docs/02 §4).

Read once at the start of every call. The mode comes fresh from the database, not
from a row this session may have cached: the emergency stop in the tablet header
(`domain/status/config.py`) writes it in another session, and the next call has to
reach the team, not the switched-off agent.
"""

import uuid
from dataclasses import dataclass

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


def call_routing(session: Session, tenant_id: uuid.UUID) -> CallRouting:
    tenant = session.get(Tenant, tenant_id)
    config = session.get(ServiceConfig, tenant_id, populate_existing=True)
    if tenant is None or config is None:
        raise NotFound("Tenant unknown or without service_config")
    return CallRouting(
        ai_answers=config.call_mode in AI_ANSWERS,
        team_phone=config.team_phone,
        tenant_name=tenant.name,
    )
