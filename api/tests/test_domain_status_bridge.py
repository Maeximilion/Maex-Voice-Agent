"""domain/status/bridge.py: the print bridge's sign of life (T-8.3, docs/02 §5).

Its poll is the heartbeat. A bridge that fails while no order waits was noticed
by nobody until the next order (open point of PR #182)."""

import uuid
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import Session

from api.config import settings
from api.domain.ordering.handover import claim_tickets
from api.domain.status import bridge
from api.domain.status import config as switches
from api.models import BridgeHeartbeat, OpeningHours, SpecialDay, Tenant
from scripts.seed import seed

BERLIN = ZoneInfo("Europe/Berlin")
# Friday 30.10.2026, 18:00 in Berlin.
NOW = datetime(2026, 10, 30, 18, 0, tzinfo=BERLIN)
FRIDAY = 4


@pytest.fixture
def session(migrated_db_url):
    engine = create_engine(migrated_db_url)
    with Session(engine) as s:
        yield s
    engine.dispose()


@pytest.fixture
def tenant_id(session, monkeypatch) -> uuid.UUID:
    """A tenant the bridge is set up for, open for pickup from 17:00 to 22:00."""
    tid = uuid.UUID(
        seed(session, tenant_name="Testbetrieb", timezone="Europe/Berlin").tenant_id
    )
    session.execute(delete(OpeningHours).where(OpeningHours.tenant_id == tid))
    hours(session, tid, "pickup", time(17), time(22))
    monkeypatch.setattr(settings, "kitchen_bridge_tenant_id", str(tid))
    monkeypatch.setattr(settings, "alarm_bridge_silent_minutes", 5)
    return tid


def hours(session, tid, service, opens, closes, weekday=FRIDAY) -> None:
    session.add(
        OpeningHours(
            tenant_id=tid,
            weekday=weekday,
            opens_at=opens,
            closes_at=closes,
            service=service,
        )
    )
    session.commit()


def silence(session, tid, now=NOW):
    return bridge.silence(session, session.get(Tenant, tid), now)


def seen_at(session, tid):
    session.expire_all()
    return session.scalar(
        select(BridgeHeartbeat.seen_at).where(BridgeHeartbeat.tenant_id == tid)
    )


# --- The poll ----------------------------------------------------------------


def test_a_poll_leaves_the_time_it_was_seen(session, tenant_id):
    claim_tickets(session, tenant_id, NOW)

    assert seen_at(session, tenant_id) == NOW


def test_polls_every_two_seconds_write_only_now_and_then(session, tenant_id):
    """The bridge polls every 2 s, nothing needs a row written that often."""
    claim_tickets(session, tenant_id, NOW)
    claim_tickets(session, tenant_id, NOW + timedelta(seconds=10))

    assert seen_at(session, tenant_id) == NOW

    later = NOW + timedelta(seconds=bridge.WRITE_EVERY_SECONDS + 1)
    claim_tickets(session, tenant_id, later)

    assert seen_at(session, tenant_id) == later


def test_a_poll_leaves_the_header_of_the_tablets_alone(session, tenant_id):
    """The header's token reads service_config.updated_at: a poll that wrote
    there would redraw every tablet every two seconds."""
    claim_tickets(session, tenant_id, NOW - timedelta(minutes=1))
    before = switches.config_change_token(session, tenant_id, now=NOW)

    claim_tickets(session, tenant_id, NOW)

    assert switches.config_change_token(session, tenant_id, now=NOW) == before


# --- Silence -----------------------------------------------------------------


def test_a_bridge_silent_too_long_while_open_is_silent(session, tenant_id):
    last = NOW - timedelta(minutes=6)
    claim_tickets(session, tenant_id, last)

    assert silence(session, tenant_id).since == last


def test_a_recent_poll_is_no_silence(session, tenant_id):
    claim_tickets(session, tenant_id, NOW - timedelta(minutes=4))

    assert silence(session, tenant_id) is None


def test_a_bridge_set_up_but_never_seen_is_silent(session, tenant_id):
    assert silence(session, tenant_id).since is None


def test_a_closed_restaurant_expects_no_bridge(session, tenant_id):
    """The register PC is off at night: that is no outage."""
    claim_tickets(session, tenant_id, NOW - timedelta(hours=1))
    night = NOW.replace(hour=23)

    assert silence(session, tenant_id, now=night) is None


def test_a_closed_special_day_expects_no_bridge(session, tenant_id):
    session.add(SpecialDay(tenant_id=tenant_id, date=NOW.date(), closed=True))
    session.commit()

    assert silence(session, tenant_id) is None


def test_delivery_hours_count_too(session, tenant_id):
    session.execute(delete(OpeningHours).where(OpeningHours.tenant_id == tenant_id))
    hours(session, tenant_id, "delivery", time(17), time(22))

    assert silence(session, tenant_id) is not None


def test_table_hours_alone_expect_no_bridge(session, tenant_id):
    """Only orders print; a reservation needs no slip."""
    session.execute(delete(OpeningHours).where(OpeningHours.tenant_id == tenant_id))
    hours(session, tenant_id, "dinein", time(17), time(22))

    assert silence(session, tenant_id) is None


def test_a_tenant_without_a_bridge_is_never_silent(session, tenant_id, monkeypatch):
    monkeypatch.setattr(settings, "kitchen_bridge_tenant_id", "")

    assert silence(session, tenant_id) is None


def test_the_header_token_changes_when_the_bridge_falls_silent(session, tenant_id):
    """Silence writes nothing, and the tablets still have to show it."""
    claim_tickets(session, tenant_id, NOW - timedelta(minutes=4))

    before = switches.config_change_token(session, tenant_id, now=NOW)
    after = switches.config_change_token(
        session, tenant_id, now=NOW + timedelta(minutes=2)
    )

    assert before != after
