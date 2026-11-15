"""Probe (review #207 area F): a 00:00 window row is the spill of the evening
before, but weekday_of picks it by the calendar date's own special day."""

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, update
from sqlalchemy.orm import Session

from api.domain.calls import call_routing
from api.domain.status.hours import load_hours, open_window_at
from api.models import CallLimitWindow, CallModeWindow, OpeningHours, ServiceConfig, SpecialDay
from scripts.seed import seed
from sim.session import resolve_tenant

BERLIN = ZoneInfo("Europe/Berlin")
MONDAY = date(2026, 11, 23)


@pytest.fixture
def session(migrated_db_url):
    engine = create_engine(migrated_db_url)
    with Session(engine) as s:
        yield s
    engine.dispose()


@pytest.fixture
def tenant(session):
    seed(session, tenant_name="Testbetrieb", timezone="Europe/Berlin")
    t = resolve_tenant(session, "Testbetrieb")
    session.execute(
        update(ServiceConfig).where(ServiceConfig.tenant_id == t.id).values(call_mode="shadow")
    )
    # Sunday evening runs to 01:00: two rows, Sunday 18-24 and Monday 00-01.
    for weekday, s, e in ((6, "18:00", "24:00"), (0, "00:00", "01:00")):
        session.execute(
            CallModeWindow.__table__.insert().values(
                tenant_id=t.id, weekday=weekday, starts_at=s, ends_at=e, call_mode="primary"
            )
        )
        session.execute(
            CallLimitWindow.__table__.insert().values(
                tenant_id=t.id, weekday=weekday, starts_at=s, ends_at=e, max_calls=3
            )
        )
    session.add(
        OpeningHours(tenant_id=t.id, weekday=6, opens_at=time(18), closes_at=time(1), service="pickup")
    )
    session.commit()
    return t


def _like_sunday(session, tenant_id, day):
    session.add(SpecialDay(tenant_id=tenant_id, date=day, like_weekday=6))
    session.commit()


def _hours_open(session, tenant, at):
    data = load_hours(session, tenant.id, at.astimezone(BERLIN).date(), days=1)
    return open_window_at(data, at, "pickup", BERLIN) is not None


def test_control_regular_monday_0030_follows_sunday_evening(session, tenant):
    at = datetime.combine(MONDAY, time(0, 30), BERLIN)
    assert _hours_open(session, tenant, at)
    r = call_routing(session, tenant.id, at)
    assert r.ai_answers and r.call_limit == 3


def test_monday_like_sunday_loses_the_sunday_evening_spill_at_0030(session, tenant):
    """Sunday 22.11 is a plain Sunday, open to 01:00; Monday 23.11 is set like
    Sunday. At 00:30 on Monday the hours still follow Sunday's evening, the
    mode and the limit look up Sunday's 00:00 rows (Saturday night's) instead."""
    _like_sunday(session, tenant.id, MONDAY)
    at = datetime.combine(MONDAY, time(0, 30), BERLIN)
    assert _hours_open(session, tenant, at)
    r = call_routing(session, tenant.id, at)
    assert r.ai_answers, "mode fell back to shadow although Sunday evening runs"
    assert r.call_limit == 3


def test_the_night_after_a_day_like_sunday_loses_its_spill(session, tenant):
    """Monday 23.11 like Sunday is open to 01:00 Tuesday (hours follow it);
    at Tuesday 00:30 the mode reads Tuesday's own 00:00 rows, none."""
    _like_sunday(session, tenant.id, MONDAY)
    at = datetime.combine(date(2026, 11, 24), time(0, 30), BERLIN)
    assert _hours_open(session, tenant, at)
    r = call_routing(session, tenant.id, at)
    assert r.ai_answers, "mode fell back to shadow although the Sunday-like evening runs"
    assert r.call_limit == 3
