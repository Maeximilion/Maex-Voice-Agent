"""The owner's admin PIN (#570 part b, D34 points 3 and 8): owner rights on one
device for 60 minutes, closed early by the idle lock, waits after wrong PINs,
never the team PIN, readable and settable by the operator."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import Session

from api.core.errors import InvalidInput
from api.domain.access.admin import (
    ADMIN_FOR,
    AdminRefused,
    admin_pin,
    close_admin,
    mark_admin_active,
    set_admin_pin,
    unlock_admin,
)
from api.domain.access.sessions import (
    ADMIN_IDLE,
    IDLE,
    mark_active,
    resolve,
    sign_in,
    unlock,
)
from api.domain.access.setup import issue_link, set_team_pin, use_link
from api.models import AuditLog, GuiAccount, Tenant

T0 = datetime(2026, 11, 9, 10, 0, tzinfo=UTC)
PASSWORD = "Kuechentablet-2026"
PIN = "4711"
ADMIN = "080815"


@pytest.fixture
def session(migrated_db_url):
    engine = create_engine(migrated_db_url)
    with Session(engine) as s:
        yield s
    engine.dispose()


@pytest.fixture
def tenant_id(session) -> uuid.UUID:
    tenant = Tenant(name="Testbetrieb")
    session.add(tenant)
    session.commit()
    return tenant.id


@pytest.fixture
def token(session, tenant_id) -> str:
    """Set up with password, team PIN and admin PIN; returns that device's token."""
    code, _ = issue_link(session, tenant_id, "operator:test", T0)
    return use_link(
        session, tenant_id, code, PASSWORD, PIN, "Tablet", T0, admin_pin=ADMIN
    ).token


def _access(session, tenant_id, token, at):
    access = resolve(session, token, tenant_id, at)
    assert access is not None
    return access


def _refused(session, session_id, pin, at) -> AdminRefused:
    with pytest.raises(AdminRefused) as refused:
        unlock_admin(session, session_id, pin, at)
    return refused.value


def test_the_admin_pin_opens_owner_rights_for_60_minutes(session, tenant_id, token):
    access = _access(session, tenant_id, token, T0)
    assert not access.admin
    unlock_admin(session, access.session_id, ADMIN, T0)
    assert _access(session, tenant_id, token, T0).admin
    # Staff changes keep the device unlocked; the owner's hour does not slide
    # with them. An owner page every 10 minutes keeps the owner's own clock.
    for minutes in range(10, 60, 10):
        at = T0 + timedelta(minutes=minutes)
        mark_active(session, access.session_id, at)
        mark_admin_active(session, access.session_id, at)
    assert _access(
        session, tenant_id, token, T0 + ADMIN_FOR - timedelta(seconds=1)
    ).admin
    assert not _access(session, tenant_id, token, T0 + ADMIN_FOR).admin
    entry = session.scalars(
        select(AuditLog).where(AuditLog.action == "gui.admin_unlocked")
    ).one()
    assert ADMIN not in str(entry.payload)


def test_the_idle_lock_closes_owner_rights_and_the_team_pin_does_not_reopen_them(
    session, tenant_id, token
):
    access = _access(session, tenant_id, token, T0)
    unlock_admin(session, access.session_id, ADMIN, T0)
    later = T0 + IDLE
    locked = _access(session, tenant_id, token, later)
    assert locked.locked and not locked.admin
    # Staff unlock the device with the team PIN: staff rights only.
    assert unlock(session, access.session_id, PIN, later) is None
    assert not _access(session, tenant_id, token, later).admin


def test_owner_rights_end_after_15_idle_minutes_though_staff_keep_the_tablet_busy(
    session, tenant_id, token
):
    access = _access(session, tenant_id, token, T0)
    unlock_admin(session, access.session_id, ADMIN, T0)
    # Staff write every 5 minutes: the idle lock never comes, the owner's clock does.
    for minutes in range(5, 15, 5):
        mark_active(session, access.session_id, T0 + timedelta(minutes=minutes))
    last = T0 + ADMIN_IDLE - timedelta(seconds=1)
    assert _access(session, tenant_id, token, last).admin
    mark_active(session, access.session_id, T0 + timedelta(minutes=14))
    gone = _access(session, tenant_id, token, T0 + ADMIN_IDLE)
    assert not gone.locked and not gone.admin


def test_an_owner_page_at_14_minutes_keeps_owner_rights_to_the_cap(
    session, tenant_id, token
):
    access = _access(session, tenant_id, token, T0)
    unlock_admin(session, access.session_id, ADMIN, T0)
    at = T0 + timedelta(minutes=14)
    mark_active(session, access.session_id, at)
    mark_admin_active(session, access.session_id, at)
    later = at + timedelta(minutes=14)
    assert _access(session, tenant_id, token, later).admin
    # The 60-minute cap holds however often the owner comes back.
    for minutes in range(28, 60, 10):
        at = T0 + timedelta(minutes=minutes)
        mark_active(session, access.session_id, at)
        mark_admin_active(session, access.session_id, at)
    end = T0 + ADMIN_FOR
    assert _access(session, tenant_id, token, end - timedelta(seconds=1)).admin
    assert not _access(session, tenant_id, token, end).admin


def test_an_owner_page_does_not_open_rights_that_are_gone(session, tenant_id, token):
    access = _access(session, tenant_id, token, T0)
    mark_admin_active(session, access.session_id, T0)
    assert not _access(session, tenant_id, token, T0).admin
    unlock_admin(session, access.session_id, ADMIN, T0)
    close_admin(session, access.session_id)
    mark_admin_active(session, access.session_id, T0 + timedelta(minutes=5))
    assert not _access(session, tenant_id, token, T0 + timedelta(minutes=5)).admin


def test_the_admin_pin_unlocks_a_locked_device_too(session, tenant_id, token):
    access = _access(session, tenant_id, token, T0)
    later = T0 + IDLE + timedelta(minutes=5)
    assert _access(session, tenant_id, token, later).locked
    unlock_admin(session, access.session_id, ADMIN, later)
    opened = _access(session, tenant_id, token, later)
    assert not opened.locked and opened.admin


def test_every_third_wrong_admin_pin_waits_a_minute_longer(session, tenant_id, token):
    access = _access(session, tenant_id, token, T0)
    assert _refused(session, access.session_id, "0000", T0).wait_minutes is None
    assert _refused(session, access.session_id, "0001", T0).wait_minutes is None
    assert _refused(session, access.session_id, "0002", T0).wait_minutes == 1
    # While the wait runs, not even the right PIN is checked.
    assert _refused(session, access.session_id, ADMIN, T0).wait_minutes == 1
    assert not _access(session, tenant_id, token, T0).admin
    after = T0 + timedelta(minutes=1)
    for _ in range(2):
        _refused(session, access.session_id, "0003", after)
    assert _refused(session, access.session_id, "0004", after).wait_minutes == 2
    # The right PIN after the wait opens and starts the count again.
    done = after + timedelta(minutes=2)
    unlock_admin(session, access.session_id, ADMIN, done)
    account = session.scalars(select(GuiAccount)).one()
    assert (account.admin_failures, account.admin_blocked_until) == (0, None)
    # A wrong admin PIN is no wrong team PIN: the device stays signed in.
    assert _access(session, tenant_id, token, done).admin


def test_a_wait_ended_meanwhile_still_answers_the_wait(
    session, tenant_id, token, monkeypatch
):
    """The rollback expires the account row; a parallel right PIN that clears
    the wait just then must not turn this answer into a crash (review of #587)."""
    access = _access(session, tenant_id, token, T0)
    for pin in ("0000", "0001", "0002"):
        _refused(session, access.session_id, pin, T0)
    rollback = session.rollback

    def rollback_while_the_wait_is_cleared() -> None:
        rollback()
        with Session(session.get_bind()) as other:
            other.execute(update(GuiAccount).values(admin_blocked_until=None))
            other.commit()

    monkeypatch.setattr(session, "rollback", rollback_while_the_wait_is_cleared)
    assert _refused(session, access.session_id, ADMIN, T0).wait_minutes == 1


def test_without_an_admin_pin_nothing_opens_and_nothing_counts(session, tenant_id):
    code, _ = issue_link(session, tenant_id, "operator:test", T0)
    token = use_link(session, tenant_id, code, PASSWORD, PIN, "Tablet", T0).token
    access = _access(session, tenant_id, token, T0)
    for _ in range(4):
        assert _refused(session, access.session_id, ADMIN, T0).missing
    assert session.scalars(select(GuiAccount)).one().admin_failures == 0


def test_closing_ends_owner_rights_at_once(session, tenant_id, token):
    access = _access(session, tenant_id, token, T0)
    unlock_admin(session, access.session_id, ADMIN, T0)
    close_admin(session, access.session_id)
    assert not _access(session, tenant_id, token, T0).admin


def test_the_admin_pin_never_equals_the_team_pin(session, tenant_id, token):
    with pytest.raises(InvalidInput):
        set_admin_pin(session, tenant_id, PIN, "operator:test")
    with pytest.raises(InvalidInput):
        set_team_pin(session, tenant_id, ADMIN, "operator:test")
    code, _ = issue_link(session, tenant_id, "operator:test", T0)
    with pytest.raises(InvalidInput):
        use_link(session, tenant_id, code, PASSWORD, "1234", "x", T0, admin_pin="1234")
    # The refusals spent nothing: the link still sets new, different PINs.
    use_link(session, tenant_id, code, PASSWORD, "1234", "x", T0, admin_pin="5678")
    account = session.scalars(select(GuiAccount)).one()
    assert (account.team_pin, account.admin_pin) == ("1234", "5678")


def test_a_link_without_an_admin_pin_keeps_the_owners_one(session, tenant_id, token):
    code, _ = issue_link(session, tenant_id, "operator:test", T0)
    use_link(session, tenant_id, code, PASSWORD, "2468", "x", T0)
    assert session.scalars(select(GuiAccount)).one().admin_pin == ADMIN
    code, _ = issue_link(session, tenant_id, "operator:test", T0)
    with pytest.raises(InvalidInput):
        use_link(session, tenant_id, code, PASSWORD, "2468", "x", T0, admin_pin="12")
    # Without a new admin PIN, the team PIN still may not take the owner's.
    with pytest.raises(InvalidInput) as same:
        use_link(session, tenant_id, code, PASSWORD, ADMIN, "x", T0)
    assert "unterscheiden" in (same.value.say or "")
    # Neither refusal spent the link.
    assert use_link(session, tenant_id, code, PASSWORD, "1357", "x", T0)


def test_a_new_admin_pin_starts_without_a_wait_and_closes_owner_rights(
    session, tenant_id, token
):
    """A new PIN is often one that leaked: the old hour and the old wait end."""
    access = _access(session, tenant_id, token, T0)
    unlock_admin(session, access.session_id, ADMIN, T0)
    other = sign_in(session, tenant_id, PASSWORD, "10.0.0.2", "Handy", T0).token
    other_id = _access(session, tenant_id, other, T0).session_id
    for pin in ("0000", "0001", "0002"):
        _refused(session, other_id, pin, T0)
    set_admin_pin(session, tenant_id, "135790", "operator:test")
    assert not _access(session, tenant_id, token, T0).admin
    account = session.scalars(select(GuiAccount)).one()
    assert (account.admin_failures, account.admin_blocked_until) == (0, None)
    unlock_admin(session, other_id, "135790", T0)
    assert _access(session, tenant_id, other, T0).admin


def test_a_new_team_pin_forgets_the_devices_wrong_tries(session, tenant_id, token):
    """Four wrong tries, then the operator's new team PIN: one typo must not
    end the session (review of #586)."""
    access = _access(session, tenant_id, token, T0)
    later = T0 + IDLE + timedelta(minutes=5)
    for pin in ("0000", "0001", "0002", "0003"):
        unlock(session, access.session_id, pin, later)
    set_team_pin(session, tenant_id, "2468", "operator:test")
    assert unlock(session, access.session_id, "0004", later) == 4
    assert resolve(session, token, tenant_id, later) is not None


def test_the_operator_reads_and_sets_the_admin_pin(session, tenant_id, token):
    assert admin_pin(session, tenant_id, "operator:test") == ADMIN
    set_admin_pin(session, tenant_id, "135790", "operator:test")
    assert admin_pin(session, tenant_id, "operator:test") == "135790"
    with pytest.raises(InvalidInput):
        set_admin_pin(session, tenant_id, "12a4", "operator:test")
    actions = session.scalars(
        select(AuditLog.action).where(AuditLog.action.like("gui.admin_pin%"))
    ).all()
    # The setup link of the fixture set it first.
    assert sorted(actions) == [
        "gui.admin_pin_set",
        "gui.admin_pin_set",
        "gui.admin_pin_shown",
        "gui.admin_pin_shown",
    ]
    payloads = session.scalars(
        select(AuditLog.payload).where(AuditLog.action.like("gui.admin_pin%"))
    ).all()
    assert all("135790" not in str(p) and ADMIN not in str(p) for p in payloads)


def test_no_admin_pin_before_the_first_link(session, tenant_id):
    assert admin_pin(session, tenant_id, "operator:test") is None
    with pytest.raises(InvalidInput):
        set_admin_pin(session, tenant_id, ADMIN, "operator:test")


def test_a_device_signed_out_meanwhile_is_told_so_not_a_wrong_pin(
    session, tenant_id, token
):
    """The owner types the right PIN on a device another tablet signed out
    (#597): the answer is the sign-in, not "Die Inhaber-PIN stimmt nicht"."""
    _access(session, tenant_id, token, T0)
    refused = _refused(session, uuid.uuid4(), ADMIN, T0)
    assert refused.ended and not refused.missing and refused.wait_minutes is None
    assert session.scalars(select(GuiAccount)).one().admin_failures == 0
