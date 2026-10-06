"""domain/calls/routing.py: does the agent answer a call, and where is the team (docs/02 §4)."""

import uuid

import pytest
from sqlalchemy import create_engine, update
from sqlalchemy.orm import Session

from api.core.errors import NotFound
from api.domain.calls import call_routing
from api.models import ServiceConfig
from scripts.seed import seed
from sim.session import resolve_tenant


@pytest.fixture
def session(migrated_db_url):
    engine = create_engine(migrated_db_url)
    with Session(engine) as s:
        yield s
    engine.dispose()


@pytest.fixture
def tenant(session):
    seed(session, tenant_name="Testbetrieb", timezone="Europe/Berlin")
    return resolve_tenant(session, "Testbetrieb")


def set_config(session, tenant_id, **values) -> None:
    session.execute(
        update(ServiceConfig)
        .where(ServiceConfig.tenant_id == tenant_id)
        .values(**values)
    )
    session.commit()


@pytest.mark.parametrize(
    ("mode", "answers"),
    [("primary", True), ("overflow", True), ("shadow", False), ("paused", False)],
)
def test_the_agent_answers_only_in_overflow_and_primary(session, tenant, mode, answers):
    set_config(session, tenant.id, call_mode=mode, team_phone="+497215550000")

    routing = call_routing(session, tenant.id)

    assert routing.ai_answers is answers
    assert routing.team_phone == "+497215550000"
    assert routing.tenant_name == "Testbetrieb"


def test_routing_reads_the_mode_fresh_from_the_database(session, tenant):
    """The emergency stop is written by the tablet in another session."""
    set_config(session, tenant.id, call_mode="primary")
    # Held, so the identity map keeps the row: it holds rows only weakly, and an
    # unreferenced row would be read fresh anyway.
    cached = session.get(ServiceConfig, tenant.id)
    assert call_routing(session, tenant.id).ai_answers is True

    with Session(session.get_bind()) as other:
        set_config(other, tenant.id, call_mode="paused")

    assert call_routing(session, tenant.id).ai_answers is False
    assert cached.call_mode == "paused"


def test_unknown_tenant_is_not_found(session, tenant):
    with pytest.raises(NotFound):
        call_routing(session, uuid.uuid4())
