"""The standard wait and today's wait (T-3.14, docs/06 §3).

A rush value is today's: it ends with the business day at 05:00 local time, by
itself, without a job that could miss its run.
"""

import uuid
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from api.core.errors import InvalidInput
from api.domain.status import config as switches
from api.domain.status import get_service_status
from api.domain.status.waits import current_wait, standard_wait
from api.models import AuditLog, ServiceConfig
from scripts.seed import seed

BERLIN = ZoneInfo("Europe/Berlin")
EVENING = datetime(2026, 9, 15, 19, 0, tzinfo=BERLIN)
NEXT_MORNING = datetime(2026, 9, 16, 5, 0, tzinfo=BERLIN)


@pytest.fixture
def engine(migrated_db_url):
    engine = create_engine(migrated_db_url)
    yield engine
    engine.dispose()


@pytest.fixture
def session(engine):
    with Session(engine) as s:
        yield s


@pytest.fixture
def tenant_id(session) -> uuid.UUID:
    return uuid.UUID(
        seed(session, tenant_name="Testbetrieb", timezone="Europe/Berlin").tenant_id
    )


def config_of(session, tenant_id) -> ServiceConfig:
    return session.get(ServiceConfig, tenant_id)


def audits(session, tenant_id, action: str) -> list[dict]:
    rows = session.scalars(
        select(AuditLog)
        .where(AuditLog.tenant_id == tenant_id, AuditLog.action == action)
        .order_by(AuditLog.id)
    ).all()
    return [row.payload for row in rows]


def test_a_new_tenant_has_a_standard_and_no_value_for_today(session, tenant_id):
    config = config_of(session, tenant_id)

    assert current_wait(config, "pickup", EVENING) == standard_wait(config, "pickup")
    assert current_wait(config, "delivery", EVENING) == standard_wait(
        config, "delivery"
    )


def test_a_tap_changes_today_and_leaves_the_standard(session, tenant_id):
    before = standard_wait(config_of(session, tenant_id), "pickup")

    config = switches.change_wait(session, tenant_id, "pickup", 15, now=EVENING)

    assert current_wait(config, "pickup", EVENING) == before + 15
    assert standard_wait(config, "pickup") == before


def test_a_typed_wait_changes_today_and_leaves_the_standard(session, tenant_id):
    before = standard_wait(config_of(session, tenant_id), "delivery")

    config = switches.set_wait(session, tenant_id, "delivery", 75, now=EVENING)

    assert current_wait(config, "delivery", EVENING) == 75
    assert standard_wait(config, "delivery") == before


def test_todays_wait_touches_only_its_service(session, tenant_id):
    config = switches.set_wait(session, tenant_id, "pickup", 75, now=EVENING)

    assert current_wait(config, "delivery", EVENING) == standard_wait(
        config, "delivery"
    )


def test_todays_wait_ends_at_five_in_the_morning(session, tenant_id):
    config = switches.set_wait(session, tenant_id, "pickup", 75, now=EVENING)
    standard = standard_wait(config, "pickup")

    assert current_wait(config, "pickup", NEXT_MORNING - timedelta(seconds=1)) == 75
    assert current_wait(config, "pickup", NEXT_MORNING) == standard


def test_a_tap_at_three_in_the_night_ends_the_same_morning(session, tenant_id):
    """The night belongs to the business day before, as with "Gericht aus"."""
    night = datetime(2026, 9, 16, 3, 0, tzinfo=BERLIN)

    config = switches.set_wait(session, tenant_id, "pickup", 75, now=night)

    assert current_wait(config, "pickup", night) == 75
    assert current_wait(config, "pickup", NEXT_MORNING) == standard_wait(
        config, "pickup"
    )


@pytest.mark.parametrize(
    "set_at, last_second, first_second",
    [
        # Summer time ends on 25.10.2026, 05:00 local is 04:00 UTC that morning.
        (
            datetime(2026, 10, 24, 22, 0, tzinfo=BERLIN),
            datetime(2026, 10, 25, 3, 59, 59, tzinfo=UTC),
            datetime(2026, 10, 25, 4, 0, tzinfo=UTC),
        ),
        # Summer time begins on 28.03.2027, 05:00 local is 03:00 UTC that morning.
        (
            datetime(2027, 3, 27, 22, 0, tzinfo=BERLIN),
            datetime(2027, 3, 28, 2, 59, 59, tzinfo=UTC),
            datetime(2027, 3, 28, 3, 0, tzinfo=UTC),
        ),
    ],
)
def test_the_end_of_the_day_holds_across_a_clock_change(
    session, tenant_id, set_at, last_second, first_second
):
    config = switches.set_wait(session, tenant_id, "pickup", 75, now=set_at)

    assert current_wait(config, "pickup", last_second) == 75
    assert current_wait(config, "pickup", first_second) == standard_wait(
        config, "pickup"
    )


def test_todays_wait_outlives_a_restart(engine, session, tenant_id):
    switches.set_wait(session, tenant_id, "pickup", 75, now=EVENING)

    with Session(engine) as restarted:
        config = restarted.get(ServiceConfig, tenant_id)
        assert config is not None
        assert current_wait(config, "pickup", EVENING) == 75


def test_the_next_tap_after_the_morning_starts_from_the_standard(session, tenant_id):
    switches.set_wait(session, tenant_id, "pickup", 75, now=EVENING)
    standard = standard_wait(config_of(session, tenant_id), "pickup")

    config = switches.change_wait(session, tenant_id, "pickup", 15, now=NEXT_MORNING)

    assert current_wait(config, "pickup", NEXT_MORNING) == standard + 15
    assert audits(session, tenant_id, switches.ACTION_WAIT)[-1]["from"] == standard


def test_the_audit_says_when_todays_wait_ends(session, tenant_id):
    switches.set_wait(session, tenant_id, "pickup", 75, now=EVENING)

    entry = audits(session, tenant_id, switches.ACTION_WAIT)[-1]

    assert entry["to"] == 75
    assert entry["until"] == NEXT_MORNING.astimezone(UTC).isoformat()


def test_the_standard_is_set_and_outlives_a_restart(engine, session, tenant_id):
    """Maxi, 02.11.2026: set 15 and it stays 15."""
    before = standard_wait(config_of(session, tenant_id), "pickup")

    switches.set_standard_wait(session, tenant_id, "pickup", 15)

    with Session(engine) as restarted:
        config = restarted.get(ServiceConfig, tenant_id)
        assert config is not None
        assert standard_wait(config, "pickup") == 15
    assert audits(session, tenant_id, switches.ACTION_WAIT_STANDARD) == [
        {"service": "pickup", "from": before, "to": 15}
    ]


def test_a_new_standard_holds_at_once_without_a_value_for_today(session, tenant_id):
    config = switches.set_standard_wait(session, tenant_id, "pickup", 15)

    assert current_wait(config, "pickup", EVENING) == 15


def test_a_new_standard_leaves_todays_wait_alone(session, tenant_id):
    switches.set_wait(session, tenant_id, "pickup", 75, now=EVENING)

    config = switches.set_standard_wait(session, tenant_id, "pickup", 15)

    assert current_wait(config, "pickup", EVENING) == 75
    assert current_wait(config, "pickup", NEXT_MORNING) == 15


@pytest.mark.parametrize(
    "minutes", [switches.MIN_WAIT_MINUTES, switches.MAX_WAIT_MINUTES]
)
def test_a_standard_may_stand_on_a_limit(session, tenant_id, minutes):
    config = switches.set_standard_wait(session, tenant_id, "delivery", minutes)

    assert standard_wait(config, "delivery") == minutes


@pytest.mark.parametrize(
    "minutes", [0, switches.MIN_WAIT_MINUTES - 1, switches.MAX_WAIT_MINUTES + 1]
)
def test_a_standard_outside_the_limits_changes_nothing(session, tenant_id, minutes):
    before = standard_wait(config_of(session, tenant_id), "pickup")

    with pytest.raises(InvalidInput) as refused:
        switches.set_standard_wait(session, tenant_id, "pickup", minutes)

    assert refused.value.say is not None
    assert "10 bis 180 Minuten" in refused.value.say
    session.rollback()
    assert standard_wait(config_of(session, tenant_id), "pickup") == before
    assert audits(session, tenant_id, switches.ACTION_WAIT_STANDARD) == []


def test_the_same_standard_again_writes_nothing(session, tenant_id):
    switches.set_standard_wait(session, tenant_id, "pickup", 15)

    switches.set_standard_wait(session, tenant_id, "pickup", 15)

    assert len(audits(session, tenant_id, switches.ACTION_WAIT_STANDARD)) == 1


def test_an_unknown_service_has_no_standard(session, tenant_id):
    with pytest.raises(InvalidInput):
        switches.set_standard_wait(session, tenant_id, "dinein", 30)


def test_one_tap_puts_todays_wait_back(session, tenant_id):
    switches.set_wait(session, tenant_id, "pickup", 75, now=EVENING)

    config = switches.reset_wait(session, tenant_id, "pickup", now=EVENING)

    standard = standard_wait(config, "pickup")
    assert current_wait(config, "pickup", EVENING) == standard
    entry = audits(session, tenant_id, switches.ACTION_WAIT)[-1]
    assert entry == {"service": "pickup", "reset": True, "from": 75, "to": standard}


def test_putting_back_with_no_value_for_today_writes_nothing(session, tenant_id):
    switches.reset_wait(session, tenant_id, "pickup", now=EVENING)

    assert audits(session, tenant_id, switches.ACTION_WAIT) == []


def test_a_value_of_yesterday_is_not_put_back_as_a_change(session, tenant_id):
    switches.set_wait(session, tenant_id, "pickup", 75, now=EVENING)
    entries = len(audits(session, tenant_id, switches.ACTION_WAIT))

    switches.reset_wait(session, tenant_id, "pickup", now=NEXT_MORNING)

    assert len(audits(session, tenant_id, switches.ACTION_WAIT)) == entries


def test_the_stream_sees_the_morning_without_a_write(session, tenant_id):
    """At 05:00 nothing is written and the tablets still have to redraw."""
    switches.set_wait(session, tenant_id, "pickup", 75, now=EVENING)

    late = switches.config_change_token(
        session, tenant_id, NEXT_MORNING - timedelta(seconds=1)
    )
    morning = switches.config_change_token(session, tenant_id, NEXT_MORNING)

    assert late != morning


def test_the_stream_sees_a_new_standard_behind_a_value_for_today(session, tenant_id):
    switches.set_wait(session, tenant_id, "pickup", 75, now=EVENING)
    before = switches.config_change_token(session, tenant_id, EVENING)

    switches.set_standard_wait(session, tenant_id, "pickup", 15)

    assert switches.config_change_token(session, tenant_id, EVENING) != before


def test_the_agent_is_told_todays_wait_and_the_standard_next_morning(
    session, tenant_id
):
    switches.set_wait(session, tenant_id, "pickup", 75, now=EVENING)
    standard = standard_wait(config_of(session, tenant_id), "pickup")

    assert get_service_status(session, tenant_id, EVENING).pickup_wait_minutes == 75
    assert (
        get_service_status(session, tenant_id, NEXT_MORNING).pickup_wait_minutes
        == standard
    )
