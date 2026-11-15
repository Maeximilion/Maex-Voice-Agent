"""Probe (review #207 area F): 24:00 in the other TIME columns of the hours,
which Postgres stores and Python cannot read (#682's class)."""

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, text, update
from sqlalchemy.orm import Session

from api.domain.status.bridge import silence
from api.domain.status.hours import load_hours, open_window_at
from api.models import BridgeHeartbeat, OpeningHours, ServiceConfig
from api.config import settings
from scripts.seed import seed
from sim.session import resolve_tenant

BERLIN = ZoneInfo("Europe/Berlin")


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


def test_opening_hours_to_2400_can_be_read(session, tenant):
    """Postgres takes closes_at '24:00' (no check refuses it), as for the
    windows docs/03 now says end at 24:00."""
    session.execute(
        OpeningHours.__table__.insert().values(
            tenant_id=tenant.id, weekday=4, opens_at="18:00", closes_at="24:00", service="pickup"
        )
    )
    session.commit()
    friday = datetime(2026, 11, 20, 23, 30, tzinfo=BERLIN)
    data = load_hours(session, tenant.id, friday.date(), days=1)
    assert open_window_at(data, friday, "pickup", BERLIN) is not None


from datetime import UTC
from api.domain.status.mode import mode_at
from api.models import CallModeWindow, Tenant


def _window(session, tenant, weekday, s, e, mode):
    session.execute(
        CallModeWindow.__table__.insert().values(
            tenant_id=tenant.id, weekday=weekday, starts_at=s, ends_at=e, call_mode=mode
        )
    )
    session.commit()


@pytest.mark.parametrize(
    "utc",
    [
        datetime(2026, 10, 25, 0, 30, tzinfo=UTC),  # 02:30 CEST, first pass
        datetime(2026, 10, 25, 1, 30, tzinfo=UTC),  # 02:30 CET, second pass
        datetime(2026, 10, 25, 22, 30, tzinfo=UTC),  # 23:30 CET
        datetime(2026, 3, 29, 1, 30, tzinfo=UTC),  # 03:30 CEST, after the gap
        datetime(2026, 3, 29, 21, 30, tzinfo=UTC),  # 23:30 CEST
    ],
)
def test_dst_nights_with_a_window_to_2400(session, tenant, utc):
    session.execute(
        update(ServiceConfig).where(ServiceConfig.tenant_id == tenant.id).values(call_mode="shadow")
    )
    _window(session, tenant, 6, "00:00", "24:00", "primary")
    config = session.get(ServiceConfig, tenant.id)
    now = mode_at(session, session.get(Tenant, tenant.id), config, utc)
    assert now.mode == "primary"
    local = utc.astimezone(BERLIN)
    assert now.planned_until == datetime.combine(local.date() + timedelta(days=1), time(0), BERLIN)
    assert now.planned_until > utc
