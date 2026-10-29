"""domain/status/mode.py and the mode switch: which mode holds now, by the
team's tap, the weekly windows and the emergency stop (T-8.1, docs/02 §4)."""

import uuid
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from api.core.errors import InvalidInput
from api.domain.status import config as switches
from api.domain.status.mode import mode_at
from api.models import AuditLog, CallModeWindow, ServiceConfig, Tenant
from scripts.seed import seed

BERLIN = ZoneInfo("Europe/Berlin")
FRIDAY = 4
# Friday 30.10.2026, 18:00 in Berlin (CET, UTC+1).
NOW = datetime(2026, 10, 30, 18, 0, tzinfo=BERLIN)


@pytest.fixture
def session(migrated_db_url):
    engine = create_engine(migrated_db_url)
    with Session(engine) as s:
        yield s
    engine.dispose()


@pytest.fixture
def tenant_id(session) -> uuid.UUID:
    return uuid.UUID(
        seed(session, tenant_name="Testbetrieb", timezone="Europe/Berlin").tenant_id
    )


def configure(session, tenant_id, **values) -> None:
    config = session.get(ServiceConfig, tenant_id)
    for name, value in values.items():
        setattr(config, name, value)
    session.commit()


def window(session, tenant_id, mode, starts, ends, weekday=FRIDAY) -> None:
    session.add(
        CallModeWindow(
            tenant_id=tenant_id,
            weekday=weekday,
            starts_at=starts,
            ends_at=ends,
            call_mode=mode,
        )
    )
    session.commit()


def now_mode(session, tenant_id, now=NOW):
    return mode_at(
        session,
        session.get(Tenant, tenant_id),
        session.get(ServiceConfig, tenant_id),
        now,
    )


def audit(session, tenant_id) -> list[dict]:
    return list(
        session.scalars(
            select(AuditLog.payload)
            .where(
                AuditLog.tenant_id == tenant_id,
                AuditLog.action == switches.ACTION_MODE,
            )
            .order_by(AuditLog.id)
        )
    )


# --- mode_at -----------------------------------------------------------------------


def test_without_a_window_the_chosen_mode_holds(session, tenant_id):
    configure(session, tenant_id, call_mode="overflow")

    now = now_mode(session, tenant_id)

    assert now.mode == "overflow"
    assert now.planned_until is None


def test_a_window_that_holds_now_decides(session, tenant_id):
    configure(session, tenant_id, call_mode="shadow")
    window(session, tenant_id, "primary", time(17), time(22))

    now = now_mode(session, tenant_id)

    assert now.mode == "primary"
    assert now.planned_until == datetime(2026, 10, 30, 22, 0, tzinfo=BERLIN)


@pytest.mark.parametrize(
    "at",
    [
        datetime(2026, 10, 30, 16, 59, tzinfo=BERLIN),
        # The end belongs to the next stretch, as for the call limit windows.
        datetime(2026, 10, 30, 22, 0, tzinfo=BERLIN),
        # Thursday at the same hour.
        datetime(2026, 10, 29, 18, 0, tzinfo=BERLIN),
        # 22:30 in Berlin is 21:30 UTC: the window is in the restaurant's clock.
        datetime(2026, 10, 30, 21, 30, tzinfo=ZoneInfo("UTC")),
    ],
)
def test_a_window_outside_its_hours_leaves_the_chosen_mode(session, tenant_id, at):
    configure(session, tenant_id, call_mode="shadow")
    window(session, tenant_id, "primary", time(17), time(22))

    assert now_mode(session, tenant_id, at).mode == "shadow"


def test_a_tap_inside_a_window_holds_until_the_next_window_begins(session, tenant_id):
    """The team switched at 17:30, after the window began: the tap counts, and
    still after the window ends, until the next window begins."""
    window(session, tenant_id, "primary", time(17), time(22))
    configure(
        session,
        tenant_id,
        call_mode="overflow",
        call_mode_chosen_at=datetime(2026, 10, 30, 17, 30, tzinfo=BERLIN),
    )

    assert now_mode(session, tenant_id).mode == "overflow"
    next_friday = NOW + timedelta(days=7)
    assert now_mode(session, tenant_id, next_friday).mode == "primary"


def test_a_window_that_begins_after_the_tap_decides(session, tenant_id):
    window(session, tenant_id, "primary", time(17), time(22))
    configure(
        session,
        tenant_id,
        call_mode="shadow",
        call_mode_chosen_at=datetime(2026, 10, 30, 16, 0, tzinfo=BERLIN),
    )

    assert now_mode(session, tenant_id).mode == "primary"


def test_the_emergency_stop_beats_every_window(session, tenant_id):
    configure(session, tenant_id, call_mode="paused")
    window(session, tenant_id, "primary", time(17), time(22))

    now = now_mode(session, tenant_id)

    assert now.mode == "paused"
    assert now.planned_until is None


def test_overlapping_windows_take_the_most_cautious_mode(session, tenant_id):
    """As the lowest limit of overlapping call limit windows (T-10.9): the
    mode in which the agent does the least."""
    configure(session, tenant_id, call_mode="primary")
    window(session, tenant_id, "primary", time(17), time(22))
    window(session, tenant_id, "overflow", time(17, 30), time(20))

    now = now_mode(session, tenant_id)

    assert now.mode == "overflow"
    assert now.planned_until == datetime(2026, 10, 30, 20, 0, tzinfo=BERLIN)


def test_a_window_of_another_tenant_does_not_count(session, tenant_id):
    configure(session, tenant_id, call_mode="shadow")
    other = uuid.UUID(
        seed(session, tenant_name="Anderer Betrieb", timezone="Europe/Berlin").tenant_id
    )
    window(session, other, "primary", time(17), time(22))

    assert now_mode(session, tenant_id).mode == "shadow"


# --- set_mode ----------------------------------------------------------------------


def test_a_tap_sets_the_mode_writes_audit_and_its_time(session, tenant_id):
    configure(session, tenant_id, call_mode="shadow")

    config = switches.set_mode(session, tenant_id, "overflow", now=NOW)

    assert config.call_mode == "overflow"
    assert config.call_mode_chosen_at == NOW
    assert audit(session, tenant_id) == [{"from": "shadow", "to": "overflow"}]


def test_a_tap_inside_a_window_wins_over_it(session, tenant_id):
    """The mode the window holds is the one the audit starts from, and a tap on
    the mode the restaurant had before the window still changes what holds."""
    configure(session, tenant_id, call_mode="shadow")
    window(session, tenant_id, "primary", time(17), time(22))

    switches.set_mode(session, tenant_id, "shadow", now=NOW)

    assert now_mode(session, tenant_id, NOW + timedelta(minutes=1)).mode == "shadow"
    assert audit(session, tenant_id) == [{"from": "primary", "to": "shadow"}]


def test_a_tap_on_the_mode_that_holds_writes_nothing(session, tenant_id):
    configure(session, tenant_id, call_mode="primary")

    config = switches.set_mode(session, tenant_id, "primary", now=NOW)

    assert config.call_mode_chosen_at is None
    assert audit(session, tenant_id) == []


@pytest.mark.parametrize("mode", ["paused", "on", ""])
def test_the_switch_takes_only_the_three_modes(session, tenant_id, mode):
    """The emergency stop has its own button with its own way back."""
    with pytest.raises(InvalidInput):
        switches.set_mode(session, tenant_id, mode, now=NOW)


def test_the_switch_never_lifts_the_emergency_stop(session, tenant_id):
    """Only "KI einschalten", which asks first, ends a pause; a stale tablet
    that still shows the switch must not."""
    configure(session, tenant_id, call_mode="paused")

    with pytest.raises(InvalidInput):
        switches.set_mode(session, tenant_id, "primary", now=NOW)
    session.rollback()

    assert session.get(ServiceConfig, tenant_id).call_mode == "paused"


def test_resume_after_a_pause_inside_a_window_returns_to_the_window(session, tenant_id):
    """Pause and resume are no choice of a mode: the window holds again."""
    configure(session, tenant_id, call_mode="shadow")
    window(session, tenant_id, "primary", time(17), time(22))

    switches.pause_ai(session, tenant_id)
    switches.resume_ai(session, tenant_id)

    assert now_mode(session, tenant_id).mode == "primary"


def test_the_header_token_changes_when_a_window_begins(session, tenant_id):
    """The tablets refresh their header on a change of the token; a window
    that begins writes nothing to the database."""
    configure(session, tenant_id, call_mode="shadow")
    window(session, tenant_id, "primary", time(17), time(22))

    before = switches.config_change_token(
        session, tenant_id, now=NOW - timedelta(hours=2)
    )
    during = switches.config_change_token(session, tenant_id, now=NOW)

    assert before != during
