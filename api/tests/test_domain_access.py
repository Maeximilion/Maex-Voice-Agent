"""The restaurant's login (#570, D34): hashes, sign-in with its growing wait, the
device session with its 30 days and idle lock, the team PIN, the setup link."""

import hashlib
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, func, select, update
from sqlalchemy.orm import Session

from api.core.errors import InvalidInput
from api.domain.access import hashing
from api.domain.access.sessions import (
    DEVICE_DAYS,
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
from api.domain.access.setup import (
    LINK_GONE,
    issue_link,
    set_team_pin,
    team_pin,
    use_link,
)
from api.jobs.retention import purge
from api.models import (
    AuditLog,
    GuiAccount,
    GuiDevice,
    GuiLoginFailure,
    GuiSession,
    Tenant,
)

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
    return use_link(session, tenant_id, code, PASSWORD, PIN, "Tablet", T0).token


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
    assert account.team_pin == PIN
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
    assert weak.value.say is not None
    assert "12 Zeichen" in weak.value.say
    with pytest.raises(InvalidInput):
        use_link(session, tenant_id, code, PASSWORD, "12", "x", T0)
    assert use_link(session, tenant_id, code, PASSWORD, PIN, "x", T0)


def test_a_new_password_signs_out_every_other_device(session, tenant_id, ready):
    other = sign_in(session, tenant_id, PASSWORD, ADDRESS, "Handy", T0).token
    code, _ = issue_link(session, tenant_id, "operator:test", T0)
    fresh = use_link(
        session, tenant_id, code, "Neues-Passwort-99", "9876", "PC", T0
    ).token

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
    token = sign_in(session, tenant_id, PASSWORD, ADDRESS, "Tablet Küche", T0).token
    access = resolve(session, token, tenant_id, T0)
    assert access is not None and not access.locked
    row = session.get(GuiSession, access.session_id)
    assert row.token_hash == hashing.token_hash(token) and token not in row.token_hash
    actions = session.scalars(select(AuditLog.action)).all()
    assert "gui.signed_in" in actions and "gui.password_set" in actions


def test_before_the_setup_no_password_signs_in(session, tenant_id):
    assert _refused(session, tenant_id, "").wait_minutes is None
    assert _refused(session, tenant_id, PASSWORD).wait_minutes is None


def test_every_third_failure_waits_a_minute_longer_up_to_five(
    session, tenant_id, ready
):
    """Maxi, 09.11.2026: one or two wrong passwords lock nothing, the third
    waits a minute, every third after it a minute longer, at most five."""
    waits = []
    at = T0
    for failure in range(1, 19):
        assert _refused(session, tenant_id, at=at).wait_minutes is None
        blocked = session.scalars(select(GuiAccount.blocked_until)).one()
        if failure % 3:
            assert blocked is None, failure
            continue
        waits.append(round((blocked - at).total_seconds() / 60))
        # While it runs, even the right password is not checked.
        assert _refused(session, tenant_id, PASSWORD, at=at).wait_minutes == waits[-1]
        at = blocked
    assert waits == [1, 2, 3, 4, 5, 5]
    sign_in(session, tenant_id, PASSWORD, ADDRESS, "x", at)
    account = session.scalars(select(GuiAccount)).one()
    assert account.failures == 0 and account.blocked_until is None
    assert session.get(GuiLoginFailure, ADDRESS) is None


def test_an_address_keeps_its_count_when_the_restaurant_signs_in(
    session, tenant_id, ready
):
    guesser = "198.51.100.1"
    for _ in range(2):
        _refused(session, tenant_id, address=guesser)
    # The restaurant signs in from its own address: the account's count starts
    # again, the guessing address keeps its two ...
    sign_in(session, tenant_id, PASSWORD, ADDRESS, "x", T0)
    _refused(session, tenant_id, address=guesser)
    # ... so its third brings the wait to that address only.
    assert _refused(session, tenant_id, PASSWORD, guesser).wait_minutes == 1
    assert sign_in(session, tenant_id, PASSWORD, ADDRESS, "x", T0)


def test_a_day_without_a_failure_starts_the_count_again(session, tenant_id, ready):
    for _ in range(2):
        _refused(session, tenant_id)
    later = T0 + timedelta(days=1)
    for _ in range(2):
        assert _refused(session, tenant_id, at=later).wait_minutes is None
    assert session.get(GuiLoginFailure, ADDRESS).failures == 2


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
    just_before = resolve(session, ready, tenant_id, T0 + IDLE - timedelta(seconds=1))
    assert just_before is not None
    assert not just_before.locked
    locked = resolve(session, ready, tenant_id, T0 + IDLE)
    assert locked is not None
    assert locked.locked
    # Reading does not unlock; a change on an unlocked device moves the 15 minutes.
    read_again = resolve(session, ready, tenant_id, T0 + IDLE)
    assert read_again is not None
    assert read_again.locked
    mark_active(session, locked.session_id, T0 + timedelta(minutes=10))
    moved = resolve(session, ready, tenant_id, T0 + timedelta(minutes=24))
    assert moved is not None
    assert not moved.locked


def test_the_team_pin_unlocks_and_five_wrong_ones_end_the_session(
    session, tenant_id, ready
):
    late = T0 + IDLE
    access = resolve(session, ready, tenant_id, late)
    assert access is not None
    assert unlock(session, access.session_id, "0000", late) == 4
    assert unlock(session, access.session_id, PIN, late) is None
    unlocked = resolve(session, ready, tenant_id, late)
    assert unlocked is not None
    assert not unlocked.locked

    later = late + IDLE
    for left in (4, 3, 2, 1, 0):
        assert unlock(session, access.session_id, "0000", later) == left
    assert resolve(session, ready, tenant_id, later) is None
    ended = session.scalars(
        select(AuditLog.payload).where(AuditLog.action == "gui.session_ended")
    ).all()
    assert {"reason": "pin_failures", "device": "Tablet"} in ended


def test_sign_out_one_device_or_all(session, tenant_id, ready):
    second = sign_in(session, tenant_id, PASSWORD, ADDRESS, "Handy", T0).token
    second_access = resolve(session, second, tenant_id, T0)
    assert second_access is not None
    one = second_access.session_id
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
    """A burst of parallel guesses meets the wait from its third on, and the
    hash runs with no row lock and no pooled connection held, so a burst of
    sign-ins cannot starve the agent's tools of connections."""
    from api.domain.access import sessions as module

    seen = []
    other = create_engine(migrated_db_url)

    def check(secret, stored):
        seen.append(session.in_transaction())
        with Session(other) as look:
            seen.append(look.scalars(select(GuiAccount.failures)).one())
        return hashing.verify_secret(secret, stored)

    monkeypatch.setattr(module, "verify_secret", check)
    _refused(session, tenant_id)
    sign_in(session, tenant_id, PASSWORD, ADDRESS, "x", T0)
    other.dispose()
    assert seen == [False, 1, False, 2]
    assert session.scalars(select(GuiAccount.failures)).one() == 0


def test_the_operator_reads_and_sets_the_team_pin(session, tenant_id, ready):
    """Maxi, 09.11.2026: the dev sees and changes the team PIN; devices stay
    signed in and unlock with the new one. The audit names the look and the
    change, never the PIN."""
    assert team_pin(session, tenant_id, "operator:test") == PIN
    set_team_pin(session, tenant_id, "123456", "operator:test")
    late = T0 + IDLE
    access = resolve(session, ready, tenant_id, late)
    assert access is not None
    assert unlock(session, access.session_id, PIN, late) == 4
    assert unlock(session, access.session_id, "123456", late) is None
    with pytest.raises(InvalidInput):
        set_team_pin(session, tenant_id, "12", "operator:test")
    rows = session.scalars(
        select(AuditLog).where(AuditLog.action.like("gui.team_pin_%"))
    ).all()
    assert [row.action for row in rows] == ["gui.team_pin_shown", "gui.team_pin_set"]
    assert all(row.payload == {} for row in rows)


def test_no_team_pin_before_the_first_link(session, tenant_id):
    assert team_pin(session, tenant_id, "operator:test") is None
    with pytest.raises(InvalidInput):
        set_team_pin(session, tenant_id, "4711", "operator:test")


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

    monkeypatch.setattr(hashlib, "scrypt", slow)
    threads = [
        threading.Thread(target=hashing.verify_secret, args=("x", None))
        for _ in range(6)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert peak[0] == 2


# --- review of #585: device cookies, a reset during a sign-in, lock order ----


def test_a_known_device_signs_in_while_a_guesser_keeps_the_account_waiting(
    session, tenant_id
):
    """Ultra review finding 1: one wrong password every 15 minutes kept every
    sign-in out. A browser that signed in before counts on its own."""
    code, _ = issue_link(session, tenant_id, "operator:test", T0)
    tablet = use_link(session, tenant_id, code, PASSWORD, PIN, "Tablet", T0)
    at = last = T0
    for _ in range(9):
        _refused(session, tenant_id, address="198.51.100.9", at=at)
        last = at
        at = session.scalars(select(GuiAccount.blocked_until)).one() or at
    # While the guesser's last wait runs, the right password from a new
    # browser waits too ...
    at = last + timedelta(minutes=1)
    assert _refused(session, tenant_id, PASSWORD, ADDRESS, at=at).wait_minutes
    # ... but the tablet, whose session ended (five wrong PINs, say), gets in.
    end_sessions(session, tenant_id, "operator:test", "test")
    session.commit()
    again = sign_in(
        session,
        tenant_id,
        PASSWORD,
        ADDRESS,
        "Tablet",
        at,
        device_token=tablet.device_token,
    )
    assert again.device_token == tablet.device_token
    assert resolve(session, again.token, tenant_id, at) is not None
    # The guesser's wait stands.
    assert session.scalars(select(GuiAccount.blocked_until)).one() > at


def test_a_known_device_has_its_own_wait_and_leaves_the_account_count(
    session, tenant_id, ready
):
    device = sign_in(session, tenant_id, PASSWORD, ADDRESS, "Tablet", T0).device_token
    for _ in range(3):
        assert _refused_with(session, tenant_id, device).wait_minutes is None
    assert _refused_with(session, tenant_id, device).wait_minutes == 1
    assert session.scalars(select(GuiAccount.failures)).one() == 0
    assert session.get(GuiLoginFailure, ADDRESS) is None


@pytest.mark.parametrize("token", ["", "forged", "other tenant"])
def test_an_unknown_device_cookie_counts_like_any_stranger(
    session, tenant_id, ready, token
):
    if token == "other tenant":
        other = Tenant(name="Anderer Betrieb")
        session.add(other)
        session.commit()
        code, _ = issue_link(session, other.id, "operator:test", T0)
        token = use_link(session, other.id, code, PASSWORD, PIN, "x", T0).device_token
    _refused_with(session, tenant_id, token)
    assert (
        session.scalars(
            select(GuiAccount.failures).where(GuiAccount.tenant_id == tenant_id)
        ).one()
        == 1
    )
    assert session.get(GuiLoginFailure, ADDRESS).failures == 1


def test_the_device_cookie_is_kept_across_sign_ins(session, tenant_id, ready):
    first = sign_in(session, tenant_id, PASSWORD, ADDRESS, "Tablet", T0)
    second = sign_in(
        session,
        tenant_id,
        PASSWORD,
        ADDRESS,
        "Tablet",
        T0,
        device_token=first.device_token,
    )
    assert second.device_token == first.device_token
    assert second.token != first.token
    assert session.execute(select(func.count()).select_from(GuiDevice)).scalar() == 2


def test_a_password_set_during_the_check_opens_nothing(
    session, tenant_id, ready, migrated_db_url, monkeypatch
):
    """Codex P1 on #585: the old password, checked while a setup link set a new
    one, must not open a session after the reset signed every device out."""
    from api.domain.access import sessions as module

    other = create_engine(migrated_db_url)

    def reset_meanwhile(secret, stored):
        with Session(other) as second:
            code, _ = issue_link(second, tenant_id, "operator:test", T0)
            use_link(second, tenant_id, code, "Neues-Passwort-99", "9876", "PC", T0)
        return hashing.verify_secret(secret, stored)

    monkeypatch.setattr(module, "verify_secret", reset_meanwhile)
    with pytest.raises(SignInRefused):
        sign_in(session, tenant_id, PASSWORD, ADDRESS, "x", T0)
    other.dispose()
    assert [row.device for row in devices(session, tenant_id)] == ["PC"]


def test_a_link_replaced_while_it_is_used_is_refused_without_a_deadlock(
    session, tenant_id, migrated_db_url
):
    """Codex P2 on #585: use_link locks the account before its link, as
    issue_link does, so a replacement in between ends in LINK_GONE."""
    import threading
    import time

    from sqlalchemy import text

    code, _ = issue_link(session, tenant_id, "operator:test", T0)
    engine = create_engine(migrated_db_url)
    holder = engine.connect()
    holder.begin()
    holder.execute(
        text("SELECT id FROM gui_accounts WHERE tenant_id = :t FOR UPDATE"),
        {"t": tenant_id},
    )
    outcome: list[str | None] = []

    def use():
        with Session(engine) as user:
            try:
                use_link(user, tenant_id, code, PASSWORD, PIN, "x", T0)
                outcome.append("used")
            except InvalidInput as refused:
                outcome.append(refused.say)

    thread = threading.Thread(target=use)
    thread.start()
    for _ in range(200):  # until the use waits for the account lock
        # The statistics views keep one snapshot per transaction otherwise.
        holder.execute(text("SELECT pg_stat_clear_snapshot()"))
        waiting = holder.execute(
            text(
                "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock'"
                " AND datname = current_database()"
            )
        ).scalar()
        if waiting:
            break
        time.sleep(0.05)
    assert waiting, "use_link never waited for the account"
    # It holds no lock on the link: the replacement takes it at once.
    holder.execute(
        text("DELETE FROM gui_setup_links WHERE token_hash = :h"),
        {"h": hashing.token_hash(code)},
    )
    holder.commit()
    thread.join(timeout=10)
    holder.close()
    engine.dispose()
    assert outcome == [LINK_GONE]


def test_two_first_links_at_once_make_one_account(session, tenant_id, migrated_db_url):
    """Ultra review finding 4: both first calls insert the account; the second
    waits for the first and then takes its row."""
    import threading

    engine = create_engine(migrated_db_url)
    codes = []

    def link():
        with Session(engine) as own:
            codes.append(issue_link(own, tenant_id, "operator:test", T0)[0])

    threads = [threading.Thread(target=link) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    engine.dispose()
    assert len(codes) == 4
    assert session.execute(select(func.count()).select_from(GuiAccount)).scalar() == 1


def test_retention_forgets_a_device_unused_for_400_days(session, tenant_id, ready):
    purge(session, T0 + timedelta(days=DEVICE_DAYS - 1))
    assert session.execute(select(func.count()).select_from(GuiDevice)).scalar() == 1
    purge(session, T0 + timedelta(days=DEVICE_DAYS))
    assert session.execute(select(func.count()).select_from(GuiDevice)).scalar() == 0


def _refused_with(session, tenant_id, device_token):
    with pytest.raises(SignInRefused) as refused:
        sign_in(
            session,
            tenant_id,
            "falsch-falsch-1",
            ADDRESS,
            "x",
            T0,
            device_token=device_token,
        )
    return refused.value


def test_a_device_in_daily_use_is_kept_past_400_days(session, tenant_id):
    """Use slides the device's 400 days like the session's 30 (#598, Maxi
    12.11.2026): the once-a-minute touch moves `last_used_at` too and says the
    cookies are due again."""
    code, _ = issue_link(session, tenant_id, "operator:test", T0)
    signed = use_link(session, tenant_id, code, PASSWORD, PIN, "Tablet", T0)
    at = T0
    while at <= T0 + timedelta(days=DEVICE_DAYS):
        at += timedelta(days=SESSION_DAYS - 1)
        access = resolve(session, signed.token, tenant_id, at, signed.device_token)
        assert access is not None and access.slid
        purge(session, at)
    assert session.execute(select(func.count()).select_from(GuiDevice)).scalar() == 1
    again = resolve(session, signed.token, tenant_id, at, signed.device_token)
    assert again is not None and not again.slid


def test_without_a_team_pin_a_locked_device_counts_no_try(session, tenant_id, ready):
    """The state migration 031 leaves until the operator sets a team PIN (#598):
    a PIN typed then is no wrong try, and five of them end nothing."""
    session.execute(update(GuiAccount).values(team_pin=None))
    session.commit()
    access = resolve(session, ready, tenant_id, T0 + IDLE)
    assert access is not None
    for _ in range(6):
        with pytest.raises(InvalidInput) as refused:
            unlock(session, access.session_id, PIN, T0 + IDLE)
        assert "Team-PIN" in (refused.value.say or "")
    assert resolve(session, ready, tenant_id, T0 + IDLE) is not None
