"""domain/calls/routing.py: does the agent answer a call, and where is the team (docs/02 §4)."""

import uuid
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, update
from sqlalchemy.orm import Session

from api.core.errors import NotFound
from api.domain.calls import call_routing, takes_call
from api.models import CallLimitWindow, ServiceConfig
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


def test_routing_carries_the_tenants_call_limit(session, tenant):
    """The phone line ends a call by this value, not by the setting of the
    same name (audit 12.10.2026)."""
    set_config(session, tenant.id, max_call_seconds=30)

    assert call_routing(session, tenant.id).max_call_seconds == 30


def test_routing_carries_where_the_privacy_notice_is(session, tenant):
    """The greeting names it (docs/09 L4); None until the restaurant sets it."""
    assert call_routing(session, tenant.id).privacy_notice_address is None

    set_config(session, tenant.id, privacy_notice_address="example.com/datenschutz")

    assert (
        call_routing(session, tenant.id).privacy_notice_address
        == "example.com/datenschutz"
    )


# --- The limit of concurrent AI calls (D16, T-10.9) ------------------------------------

FRIDAY_EVENING = datetime(2026, 10, 30, 18, 0, tzinfo=ZoneInfo("Europe/Berlin"))


@pytest.fixture
def answering(session, tenant):
    """The agent answers: only then are the windows read."""
    set_config(session, tenant.id, call_mode="primary")
    return tenant


def add_window(session, tenant_id, weekday, starts, ends, max_calls) -> None:
    session.add(
        CallLimitWindow(
            tenant_id=tenant_id,
            weekday=weekday,
            starts_at=time.fromisoformat(starts),
            ends_at=time.fromisoformat(ends),
            max_calls=max_calls,
        )
    )
    session.commit()


def test_the_limit_is_one_without_a_window(session, answering):
    tenant = answering
    assert call_routing(session, tenant.id, FRIDAY_EVENING).call_limit == 1


def test_a_window_sets_the_limit_in_the_restaurants_clock(session, answering):
    tenant = answering
    add_window(session, tenant.id, 4, "17:00", "22:00", 3)

    assert call_routing(session, tenant.id, FRIDAY_EVENING).call_limit == 3
    # 16:59 local, and the same hour on Saturday, are outside the window.
    before = FRIDAY_EVENING.replace(hour=16, minute=59)
    assert call_routing(session, tenant.id, before).call_limit == 1
    saturday = FRIDAY_EVENING + timedelta(days=1)
    assert call_routing(session, tenant.id, saturday).call_limit == 1
    # The end is not in the window.
    end = FRIDAY_EVENING.replace(hour=22)
    assert call_routing(session, tenant.id, end).call_limit == 1


def test_overlapping_windows_take_the_lowest_limit(session, answering):
    tenant = answering
    add_window(session, tenant.id, 4, "17:00", "22:00", 3)
    add_window(session, tenant.id, 4, "18:00", "19:00", 2)

    assert call_routing(session, tenant.id, FRIDAY_EVENING).call_limit == 2


def test_a_changed_default_is_read_at_the_next_call(session, answering):
    tenant = answering
    set_config(session, tenant.id, max_concurrent_calls=2)

    assert call_routing(session, tenant.id, FRIDAY_EVENING).call_limit == 2


def test_a_mode_without_the_agent_reads_no_window(session, tenant):
    set_config(session, tenant.id, call_mode="paused", max_concurrent_calls=2)
    add_window(session, tenant.id, 4, "17:00", "22:00", 3)

    assert call_routing(session, tenant.id, FRIDAY_EVENING).call_limit == 2


@pytest.mark.parametrize(
    ("mode", "open_calls", "takes"),
    [
        ("primary", 0, True),
        ("primary", 1, False),
        ("overflow", 0, True),
        ("paused", 0, False),
        ("shadow", 0, False),
    ],
)
def test_a_call_above_the_limit_goes_to_the_team(
    session, tenant, mode, open_calls, takes
):
    set_config(session, tenant.id, call_mode=mode)

    assert (
        takes_call(call_routing(session, tenant.id, FRIDAY_EVENING), open_calls)
        is takes
    )
