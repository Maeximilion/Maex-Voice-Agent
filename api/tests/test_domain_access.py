"""The restaurant's login (#570, D34): hashes, sign-in with its growing wait, the
device session with its 30 days and idle lock, the team PIN, the setup link."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from api.core.errors import InvalidInput
from api.domain.access import hashing
from api.domain.access.sessions import (
    IDLE,
    SESSION_DAYS,
    SignInRefused,
    devices,
    end_sessions,
    mark_active,
    resolve,
    sign_in,
    stack_tenant_id,
    unlock,
)
from api.domain.access.setup import LINK_GONE, issue_link, use_link
from api.jobs.retention import purge
from api.models import AuditLog, GuiAccount, GuiLoginFailure, GuiSession, Tenant

T0 = datetime(2026, 11, 9, 10, 0, tzinfo=UTC)
PASSWORD = "Kuechentablet-2026"
PIN = "4711"
ADDRESS = "203.0.113.7"


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
def ready(session, tenant_id) -> str:
    """The restaurant has set its password and PIN; returns that device's token."""
    code, _ = issue_link(session, tenant_id, "operator:test", T0)
    return use_link(session, tenant_id, code, PASSWORD, PIN, "Tablet", T0)


def _refused(session, tenant_id, password="falsch-falsch-1", address=ADDRESS, at=T0):
    with pytest.raises(SignInRefused) as refused:
        sign_in(session, tenant_id, password, address, "x", at)
    return refused.value


# --- hashes and rules --------------------------------------------------------


def test_a_hash_checks_its_own_secret_only():
    stored = hashing.hash_secret(PASSWORD)
    assert stored.startswith("scrypt$32768$8$1$")
    assert hashing.verify_secret(PASSWORD, stored)
    assert not hashing.verify_secret(PASSWORD + "x", stored)
    assert hashing.hash_secret(PASSWORD) != stored  # a salt of its own


@pytest.mark.parametrize("stored", [None, "", "md5$abc", "scrypt$1$2"])
def test_a_missing_or_broken_hash_opens_nothing(stored):
    assert not hashing.verify_secret(PASSWORD, stored)


@pytest.mark.parametrize(
    ("password", "refused"),
    [
        ("kurz", True),
        ("x" * 129, True),
        ("restaurant1234", True),
        ("aaaaaaaaaaaaaa", True),
        ("abababababab", True),
        (PASSWORD, False),
    ],
)
def test_password_rules(password, refused):
    assert (hashing.password_problem(password) is not None) is refused


@pytest.mark.parametrize(
    ("pin", "ok"),
    [
        ("123", False),
        ("1234", True),
        ("123456", True),
        ("1234567", False),
        ("12a4", False),
        ("١٢٣٤", False),
    ],
)
def test_pin_has_four_to_six_ascii_digits(pin, ok):
    assert (hashing.pin_problem(pin) is None) is ok


# --- setup link --------------------------------------------------------------


def test_the_setup_link_sets_password_and_pin_and_signs_in(session, tenant_id, ready):
    account = session.scalars(select(GuiAccount)).one()
    assert hashing.verify_secret(PASSWORD, account.password_hash)
    assert hashing.verify_secret(PIN, account.team_pin_hash)
    assert resolve(session, ready, tenant_id, T0) is not None


def test_a_link_works_once_for_24_hours_and_a_newer_one_replaces_it(session, tenant_id):
    old, _ = issue_link(session, tenant_id, "operator:test", T0)
    new, expires = issue_link(session, tenant_id, "operator:test", T0)
    assert expires == T0 + timedelta(hours=24)
    with pytest.raises(InvalidInput) as replaced:
        use_link(session, tenant_id, old, PASSWORD, PIN, "x", T0)
    assert replaced.value.say == LINK_GONE
    with pytest.raises(InvalidInput):
        use_link(session, tenant_id, new, PASSWORD, PIN, "x", expires)
    late, _ = issue_link(session, tenant_id, "operator:test", T0)
    use_link(session, tenant_id, late, PASSWORD, PIN, "x", T0 + timedelta(hours=23))
    with pytest.raises(InvalidInput):
        use_link(session, tenant_id, late, PASSWORD, PIN, "x", T0)


def test_a_refused_value_does_not_spend_the_link(session, tenant_id):
    code, _ = issue_link(session, tenant_id, "operator:test", T0)
    with pytest.raises(InvalidInput) as weak:
        use_link(session, tenant_id, code, "kurz", PIN, "x", T0)
    assert "12 Zeichen" in weak.value.say
    with pytest.raises(InvalidInput):
        use_link(session, tenant_id, code, PASSWORD, "12", "x", T0)
    assert use_link(session, tenant_id, code, PASSWORD, PIN, "x", T0)


def test_a_new_password_signs_out_every_other_device(session, tenant_id, ready):
    other = sign_in(session, tenant_id, PASSWORD, ADDRESS, "Handy", T0)
    code, _ = issue_link(session, tenant_id, "operator:test", T0)
    fresh = use_link(session, tenant_id, code, "Neues-Passwort-99", "9876", "PC", T0)

    assert resolve(session, ready, tenant_id, T0) is None
    assert resolve(session, other, tenant_id, T0) is None
    assert resolve(session, fresh, tenant_id, T0) is not None
    _refused(session, tenant_id, PASSWORD)


def test_a_link_of_another_tenant_is_refused(session, tenant_id):
    other = Tenant(name="Anderer Betrieb")
    session.add(other)
    session.commit()
    code, _ = issue_link(session, other.id, "operator:test", T0)
    with pytest.raises(InvalidInput):
        use_link(session, tenant_id, code, PASSWORD, PIN, "x", T0)


# --- sign-in -----------------------------------------------------------------


def test_sign_in_with_the_password_opens_a_session(session, tenant_id, ready):
    token = sign_in(session, tenant_id, PASSWORD, ADDRESS, "Tablet Küche", T0)
    access = resolve(session, token, tenant_id, T0)
    assert access is not None and not access.locked
    row = session.get(GuiSession, access.session_id)
    assert row.token_hash == hashing.token_hash(token) and token not in row.token_hash
    actions = session.scalars(select(AuditLog.action)).all()
    assert "gui.signed_in" in actions and "gui.password_set" in actions


def test_before_the_setup_no_password_signs_in(session, tenant_id):
    assert _refused(session, tenant_id, "").wait_minutes is None
    assert _refused(session, tenant_id, PASSWORD).wait_minutes is None


def test_the_fifth_failure_brings_a_wait_that_grows_to_15_minutes(
    session, tenant_id, ready
):
    waits = []
    at = T0
    for _ in range(4):
        assert _refused(session, tenant_id, at=at).wait_minutes is None
    for _ in range(6):
        _refused(session, tenant_id, at=at)
        account = session.scalars(select(GuiAccount)).one()
        waits.append(round((account.blocked_until - at).total_seconds() / 60))
        # While it runs, even the right password is not checked.
        assert _refused(session, tenant_id, PASSWORD, at=at).wait_minutes == waits[-1]
        at = account.blocked_until
    assert waits == [1, 2, 4, 8, 15, 15]
    sign_in(session, tenant_id, PASSWORD, ADDRESS, "x", at)
    account = session.scalars(select(GuiAccount)).one()
    assert account.failed_logins == 0 and account.blocked_until is None
    assert session.get(GuiLoginFailure, ADDRESS) is None


def test_an_address_keeps_its_count_when_the_restaurant_signs_in(
    session, tenant_id, ready
):
    guesser = "198.51.100.1"
    for _ in range(4):
        _refused(session, tenant_id, address=guesser)
    # The restaurant signs in from its own address: the account's count starts
    # again, the guessing address keeps its four ...
    sign_in(session, tenant_id, PASSWORD, ADDRESS, "x", T0)
    _refused(session, tenant_id, address=guesser)
    # ... so its fifth brings the wait to that address only.
    assert _refused(session, tenant_id, PASSWORD, guesser).wait_minutes == 1
    assert sign_in(session, tenant_id, PASSWORD, ADDRESS, "x", T0)


def test_a_day_without_a_failure_starts_the_count_again(session, tenant_id, ready):
    for _ in range(4):
        _refused(session, tenant_id)
    later = T0 + timedelta(days=1)
    for _ in range(4):
        assert _refused(session, tenant_id, at=later).wait_minutes is None
    assert session.get(GuiLoginFailure, ADDRESS).failures == 4


# --- the session -------------------------------------------------------------


def test_the_session_slides_for_30_days_on_use(session, tenant_id, ready):
    at = T0
    for _ in range(3):
        at += timedelta(days=SESSION_DAYS - 1)
        assert resolve(session, ready, tenant_id, at) is not None
    assert resolve(session, ready, tenant_id, at + timedelta(days=SESSION_DAYS)) is None
    assert session.execute(select(func.count()).select_from(GuiSession)).scalar() == 0


def test_a_session_is_valid_for_its_own_tenant_only(session, tenant_id, ready):
    assert resolve(session, ready, uuid.uuid4(), T0) is None
    assert resolve(session, ready, None, T0) is None
    assert resolve(session, "", tenant_id, T0) is None
    assert resolve(session, ready + "x", tenant_id, T0) is None


def test_15_minutes_without_a_change_lock_the_session(session, tenant_id, ready):
    assert not resolve(
        session, ready, tenant_id, T0 + IDLE - timedelta(seconds=1)
    ).locked
    locked = resolve(session, ready, tenant_id, T0 + IDLE)
    assert locked.locked
    # Reading does not unlock; a change on an unlocked device moves the 15 minutes.
    assert resolve(session, ready, tenant_id, T0 + IDLE).locked
    mark_active(session, locked.session_id, T0 + timedelta(minutes=10))
    assert not resolve(session, ready, tenant_id, T0 + timedelta(minutes=24)).locked


def test_the_team_pin_unlocks_and_five_wrong_ones_end_the_session(
    session, tenant_id, ready
):
    late = T0 + IDLE
    access = resolve(session, ready, tenant_id, late)
    assert unlock(session, access.session_id, "0000", late) == 4
    assert unlock(session, access.session_id, PIN, late) is None
    assert not resolve(session, ready, tenant_id, late).locked

    later = late + IDLE
    for left in (4, 3, 2, 1, 0):
        assert unlock(session, access.session_id, "0000", later) == left
    assert resolve(session, ready, tenant_id, later) is None
    ended = session.scalars(
        select(AuditLog.payload).where(AuditLog.action == "gui.session_ended")
    ).all()
    assert {"reason": "pin_failures", "device": "Tablet"} in ended


def test_sign_out_one_device_or_all(session, tenant_id, ready):
    second = sign_in(session, tenant_id, PASSWORD, ADDRESS, "Handy", T0)
    one = resolve(session, second, tenant_id, T0).session_id
    assert end_sessions(session, tenant_id, "operator:test", "lost", one) == 1
    session.commit()
    assert resolve(session, second, tenant_id, T0) is None
    assert [row.device for row in devices(session, tenant_id)] == ["Tablet"]
    assert end_sessions(session, tenant_id, "operator:test", "lost") == 1
    session.commit()
    assert devices(session, tenant_id) == []


def test_the_stack_serves_its_oldest_tenant(session, tenant_id):
    session.add(Tenant(name="Später"))
    session.commit()
    assert stack_tenant_id(session) == tenant_id


# --- retention ---------------------------------------------------------------


def test_retention_drops_old_sessions_and_spent_addresses(session, tenant_id, ready):
    _refused(session, tenant_id)
    purge(session, T0 + timedelta(hours=23))
    assert session.get(GuiLoginFailure, ADDRESS) is not None
    assert session.execute(select(func.count()).select_from(GuiSession)).scalar() == 1

    purge(session, T0 + timedelta(days=SESSION_DAYS))
    session.expire_all()
    assert session.get(GuiLoginFailure, ADDRESS) is None
    assert session.execute(select(func.count()).select_from(GuiSession)).scalar() == 0


# --- load: no lock or connection during the hash, at most two hashes ---------


def test_the_attempt_is_counted_before_the_slow_check_and_holds_no_transaction(
    session, tenant_id, ready, migrated_db_url, monkeypatch
):
    """A burst of parallel guesses meets the wait from its fifth on, and the
    hash runs with no row lock and no pooled connection held, so a burst of
    sign-ins cannot starve the agent's tools of connections."""
    from api.domain.access import sessions as module

    seen = []
    other = create_engine(migrated_db_url)

    def check(secret, stored):
        seen.append(session.in_transaction())
        with Session(other) as look:
            seen.append(look.scalars(select(GuiAccount.failed_logins)).one())
        return hashing.verify_secret(secret, stored)

    monkeypatch.setattr(module, "verify_secret", check)
    _refused(session, tenant_id)
    sign_in(session, tenant_id, PASSWORD, ADDRESS, "x", T0)
    other.dispose()
    assert seen == [False, 1, False, 2]
    assert session.scalars(select(GuiAccount.failed_logins)).one() == 0


def test_the_pin_try_is_counted_before_the_check_too(
    session, tenant_id, ready, monkeypatch
):
    from api.domain.access import sessions as module

    access = resolve(session, ready, tenant_id, T0 + IDLE)
    seen = []

    def check(secret, stored):
        seen.append(session.in_transaction())
        return hashing.verify_secret(secret, stored)

    monkeypatch.setattr(module, "verify_secret", check)
    assert unlock(session, access.session_id, "0000", T0 + IDLE) == 4
    assert seen == [False]
    assert session.get(GuiSession, access.session_id).pin_failures == 1


def test_at_most_two_hashes_run_at_once(monkeypatch):
    import threading
    import time

    running, peak = [0], [0]
    lock = threading.Lock()

    def slow(*args, **kwargs):
        with lock:
            running[0] += 1
            peak[0] = max(peak[0], running[0])
        time.sleep(0.05)
        with lock:
            running[0] -= 1
        return b"\0" * hashing.KEY_BYTES

    monkeypatch.setattr(hashing.hashlib, "scrypt", slow)
    threads = [
        threading.Thread(target=hashing.verify_secret, args=("x", None))
        for _ in range(6)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert peak[0] == 2
