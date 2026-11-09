"""The operator login (#571, docs/23 §5): password plus a second factor, the
same answer to every wrong input, waits after failures, sessions, CSRF, the
proxy check and the break-glass command, against a scratch operator database."""

import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import sessionmaker

from api.dashboard import access, cli, totp
from api.dashboard import app as web
from api.dashboard.config import DashboardSettings, settings
from api.dashboard.db import get_db
from api.dashboard.models import (
    OperatorAccount,
    OperatorAudit,
    OperatorLoginFailure,
    OperatorSession,
)
from evals.scratch_db import create_scratch_db, drop_scratch_db

ROOT = Path(__file__).resolve().parents[2]
INI = ROOT / "db" / "dashboard" / "alembic.ini"
PASSWORD = "lange-geheime-phrase"
START = datetime(2026, 11, 10, 9, 0, tzinfo=UTC)
TABLES = {
    "operator_accounts",
    "operator_recovery_codes",
    "operator_sessions",
    "operator_devices",
    "operator_login_failures",
    "operator_audit",
}


def _config(url: str) -> Config:
    cfg = Config(str(INI))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


@pytest.fixture
def operator_url():
    url = create_scratch_db(prefix="maex_ops")
    try:
        yield url
    finally:
        drop_scratch_db(url)


@pytest.fixture
def sessions(operator_url):
    command.upgrade(_config(operator_url), "head")
    engine = create_engine(operator_url)
    yield sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    engine.dispose()


class Clock:
    def __init__(self) -> None:
        self.at = START

    def __call__(self) -> datetime:
        return self.at


@pytest.fixture
def clock(monkeypatch):
    moving = Clock()
    monkeypatch.setattr(web, "now", moving)
    return moving


@pytest.fixture
def client(sessions, clock):
    def db():
        session = sessions()
        try:
            yield session
        finally:
            session.close()

    web.app.dependency_overrides[get_db] = db
    yield TestClient(web.app)
    web.app.dependency_overrides.pop(get_db, None)


@pytest.fixture
def factors(sessions):
    with sessions() as db:
        return access.create_account(db, "Maxi", PASSWORD)


def _code(secret: str, at: datetime) -> str:
    return totp.code_at(secret, int(at.timestamp()) // totp.STEP_SECONDS)


def _sign_in(client, code: str, name: str = "maxi", password: str = PASSWORD):
    client.get("/login")
    return client.post(
        "/login",
        data={
            "name": name,
            "password": password,
            "code": code,
            "csrf_token": client.cookies.get("ops_csrf", ""),
        },
        follow_redirects=False,
    )


def _actions(sessions) -> list[str]:
    with sessions() as db:
        return list(db.scalars(select(OperatorAudit.action).order_by(OperatorAudit.id)))


def test_password_and_totp_open_the_dashboard(client, factors, clock, sessions):
    answer = _sign_in(client, _code(factors.totp_secret, clock.at), name=" Maxi ")
    assert answer.status_code == 303 and answer.headers["location"] == "/"
    assert "HttpOnly" in answer.headers["set-cookie"]
    page = client.get("/")
    assert page.status_code == 200
    assert "Signed in as <strong>maxi</strong>" in page.text
    assert "10 recovery codes left" in page.text
    assert _actions(sessions)[-1] == "operator.signed_in"


@pytest.mark.parametrize(
    "name, password, code",
    [
        ("nobody", PASSWORD, "right"),
        ("maxi", "falsches-passwort", "right"),
        ("maxi", PASSWORD, "000000"),
        ("maxi", PASSWORD, ""),
        ("maxi", PASSWORD, "aaaa-bbbb-cccc-dddd"),
    ],
)
def test_every_wrong_input_gets_the_same_answer(
    client, factors, clock, name, password, code
):
    if code == "right":
        code = _code(factors.totp_secret, clock.at)
    elif code == "000000" and _code(factors.totp_secret, clock.at) == code:
        pytest.skip("the real code happens to be 000000")
    answer = _sign_in(client, code, name=name, password=password)
    assert answer.status_code == 401
    assert web.FAILED in answer.text
    assert "ops_session" not in answer.cookies
    assert client.get("/", follow_redirects=False).headers["location"] == "/login"


def test_a_right_password_with_a_wrong_factor_is_audited_as_such(
    client, factors, sessions
):
    _sign_in(client, "abcdef")
    _sign_in(client, "123456", password="falsch-falsch-falsch")
    _sign_in(client, "123456", name="nobody")
    with sessions() as db:
        rows = db.scalars(
            select(OperatorAudit).where(
                OperatorAudit.action == "operator.sign_in_failed"
            )
        ).all()
    # The unknown name leaves nothing in the audit.
    assert [row.detail["reason"] for row in rows] == ["factor", "password"]
    assert all(row.detail["account"] == "maxi" for row in rows)


def test_a_totp_code_opens_once(client, factors, clock):
    code = _code(factors.totp_secret, clock.at)
    assert _sign_in(client, code).status_code == 303
    client.cookies.clear()
    assert _sign_in(client, code).status_code == 401
    # The next step's code still opens.
    clock.at += timedelta(seconds=totp.STEP_SECONDS)
    assert _sign_in(client, _code(factors.totp_secret, clock.at)).status_code == 303


def test_a_recovery_code_opens_once_and_is_audited(client, factors, sessions):
    first = factors.recovery_codes[0]
    assert _sign_in(client, first.upper().replace("-", " ")).status_code == 303
    assert "9 recovery codes left" in client.get("/").text
    assert "operator.recovery_code_used" in _actions(sessions)
    client.cookies.clear()
    assert _sign_in(client, first).status_code == 401


def test_few_recovery_codes_left_are_shown_as_a_warning(client, factors):
    for used in factors.recovery_codes[:7]:
        client.cookies.clear()
        assert _sign_in(client, used).status_code == 303
    page = client.get("/").text
    assert "3 recovery codes left" in page and 'role="alert"' in page


def test_every_third_failure_brings_a_wait_even_for_the_right_input(
    client, factors, clock
):
    for _ in range(3):
        assert _sign_in(client, "000001").status_code == 401
    right = _code(factors.totp_secret, clock.at)
    answer = _sign_in(client, right)
    assert answer.status_code == 401
    assert web.WAIT.format(minutes=1) in answer.text
    clock.at += timedelta(minutes=1, seconds=1)
    assert _sign_in(client, _code(factors.totp_secret, clock.at)).status_code == 303


def test_an_address_waits_on_its_own_and_is_forgotten_after_a_day(
    client, sessions, clock
):
    # Unknown names have no account count: only the address brings the wait.
    for name in ("anna", "bert", "carl"):
        assert _sign_in(client, "000001", name=name).status_code == 401
    assert web.WAIT.format(minutes=1) in _sign_in(client, "000001", name="dora").text
    with sessions() as db:
        db.add(
            OperatorLoginFailure(
                address="198.51.100.7", failures=2, last_failed_at=clock.at
            )
        )
        db.commit()
    clock.at += timedelta(days=1, minutes=1)
    _sign_in(client, "000001", name="emil")
    with sessions() as db:
        left = {
            row.address: row.failures
            for row in db.scalars(select(OperatorLoginFailure))
        }
    # The old address is gone; this one starts again from one.
    assert left == {"testclient": 1}


def test_ended_sessions_go_with_the_next_sign_in(client, factors, clock, sessions):
    _sign_in(client, _code(factors.totp_secret, clock.at))
    client.cookies.clear()
    clock.at += timedelta(minutes=31)
    _sign_in(client, _code(factors.totp_secret, clock.at))
    with sessions() as db:
        rows = db.scalars(select(OperatorSession)).all()
    assert len(rows) == 1 and rows[0].created_at == clock.at


def test_a_browser_that_signed_in_before_gets_past_the_account_wait(
    sessions, factors, clock
):
    with TestClient(web.app) as known, TestClient(web.app) as guesser:

        def db():
            session = sessions()
            try:
                yield session
            finally:
                session.close()

        web.app.dependency_overrides[get_db] = db
        try:
            assert (
                _sign_in(known, _code(factors.totp_secret, clock.at)).status_code == 303
            )
            known.post(
                "/logout",
                data={"csrf_token": known.cookies.get("ops_csrf", "")},
                follow_redirects=False,
            )
            for _ in range(3):
                _sign_in(guesser, "000001", password="falsch-falsch-falsch")
            assert web.WAIT.format(minutes=1) in _sign_in(guesser, "000001").text
            clock.at += timedelta(seconds=totp.STEP_SECONDS)
            # The device cookie of the earlier sign-in counts on its own.
            answer = _sign_in(known, _code(factors.totp_secret, clock.at))
            assert answer.status_code == 303
        finally:
            web.app.dependency_overrides.pop(get_db, None)


def test_the_session_ends_after_30_idle_minutes(client, factors, clock):
    _sign_in(client, _code(factors.totp_secret, clock.at))
    clock.at += timedelta(minutes=29)
    assert client.get("/").status_code == 200
    clock.at += timedelta(minutes=29)
    assert client.get("/").status_code == 200
    clock.at += timedelta(minutes=30)
    assert client.get("/", follow_redirects=False).headers["location"] == "/login"


def test_the_session_ends_after_12_hours_however_busy(client, factors, clock):
    _sign_in(client, _code(factors.totp_secret, clock.at))
    for _ in range(12 * 3 - 1):
        clock.at += timedelta(minutes=20)
        assert client.get("/").status_code == 200
    clock.at += timedelta(minutes=20)
    assert client.get("/", follow_redirects=False).status_code == 303


def test_a_new_sign_in_ends_the_session_the_browser_brought(client, factors, clock):
    _sign_in(client, _code(factors.totp_secret, clock.at))
    old = client.cookies.get("ops_session")
    clock.at += timedelta(seconds=totp.STEP_SECONDS)
    _sign_in(client, _code(factors.totp_secret, clock.at))
    assert client.cookies.get("ops_session") != old
    client.cookies.set("ops_session", old)
    assert client.get("/", follow_redirects=False).status_code == 303


def test_sign_out_ends_the_session(client, factors, clock, sessions):
    _sign_in(client, _code(factors.totp_secret, clock.at))
    token = client.cookies.get("ops_session")
    answer = client.post(
        "/logout",
        data={"csrf_token": client.cookies.get("ops_csrf", "")},
        follow_redirects=False,
    )
    assert answer.headers["location"] == "/login"
    client.cookies.set("ops_session", token)
    assert client.get("/", follow_redirects=False).status_code == 303
    assert _actions(sessions)[-1] == "operator.signed_out"


def test_a_post_without_the_page_token_or_from_another_site_is_refused(
    client, factors, clock
):
    client.get("/login")
    fields = {
        "name": "maxi",
        "password": PASSWORD,
        "code": _code(factors.totp_secret, clock.at),
    }
    assert client.post("/login", data=fields).status_code == 403
    token = client.cookies.get("ops_csrf", "")
    other = client.post(
        "/login",
        data={**fields, "csrf_token": token},
        headers={"Origin": "https://evil.example.com"},
    )
    assert other.status_code == 403
    assert client.post("/logout", data={"csrf_token": token}).status_code == 401


def test_outside_dev_and_test_only_the_proxy_gets_in(
    client, factors, clock, monkeypatch, sessions
):
    secret = "p" * 40
    monkeypatch.setattr(settings, "env", "prod")
    monkeypatch.setattr(settings, "operator_proxy_secret", secret)
    assert client.get("/health").status_code == 200
    assert client.get("/login").status_code == 403
    wrong = {"X-Maex-Proxy-Secret": "q" * 40}
    assert client.get("/login", headers=wrong).status_code == 403
    proxy = {"X-Maex-Proxy-Secret": secret, "X-Forwarded-For": "1.2.3.4, 5.6.7.8"}
    # Over https, or the client would not send the Secure cookies back.
    browser = TestClient(web.app, base_url="https://testserver")
    page = browser.get("/login", headers=proxy)
    assert page.status_code == 200
    assert "__Host-ops_csrf" in page.headers["set-cookie"]
    assert "Secure" in page.headers["set-cookie"]
    answer = browser.post(
        "/login",
        data={
            "name": "maxi",
            "password": PASSWORD,
            "code": _code(factors.totp_secret, clock.at),
            "csrf_token": browser.cookies.get("__Host-ops_csrf", ""),
        },
        headers=proxy,
        follow_redirects=False,
    )
    assert answer.status_code == 303
    assert "__Host-ops_session" in answer.headers["set-cookie"]
    assert browser.get("/", headers=proxy).status_code == 200
    with sessions() as db:
        row = db.scalars(
            select(OperatorAudit).where(OperatorAudit.action == "operator.signed_in")
        ).one()
    # The last entry, the one Caddy added: the client cannot choose it.
    assert row.detail["address"] == "5.6.7.8"


def test_production_settings_need_a_strong_proxy_secret():
    for weak in ("", "short", "change_me_long_random", " " * 40):
        with pytest.raises(ValueError, match="OPERATOR_PROXY_SECRET"):
            DashboardSettings(env="prod", operator_proxy_secret=weak)
    DashboardSettings(env="prod", operator_proxy_secret="x" * 40)
    DashboardSettings(env="dev", operator_proxy_secret="")


def test_the_dashboard_never_builds_the_customer_settings():
    """The operator stack has no AGENT_API_TOKEN; importing api.config there
    would stop the start (docs/23 §5)."""
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", "/tmp"),
        "ENV": "prod",
        "OPERATOR_PROXY_SECRET": "z" * 40,
    }
    probe = (
        "import sys, api.dashboard.app, api.dashboard.cli;"
        "assert 'api.config' not in sys.modules, 'api.config imported';"
        "assert 'api.db' not in sys.modules, 'api.db imported'"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr[-2000:]


def test_cli_add_prints_the_factors_once_and_reset_replaces_them(
    sessions, monkeypatch, capsys, clock
):
    monkeypatch.setattr(cli, "SessionLocal", sessions)
    monkeypatch.setattr(cli, "getpass", lambda _prompt: PASSWORD)
    assert cli.main(["add", "maxi"]) == 0
    first = capsys.readouterr().out
    assert "otpauth://totp/Maex%20Operator:maxi?secret=" in first
    codes = [line.strip() for line in first.splitlines() if line.count("-") == 3]
    assert len(codes) == 10
    with sessions() as db:
        old_secret = db.scalars(select(OperatorAccount.totp_secret)).one()
    with TestClient(web.app) as browser:

        def db():
            session = sessions()
            try:
                yield session
            finally:
                session.close()

        web.app.dependency_overrides[get_db] = db
        try:
            assert _sign_in(browser, codes[0]).status_code == 303
            assert cli.main(["reset", "maxi"]) == 0
            second = capsys.readouterr().out
            # The reset ended the session and every old code.
            assert browser.get("/", follow_redirects=False).status_code == 303
            assert _sign_in(browser, codes[1]).status_code == 401
            with sessions() as session:
                secret = session.scalars(select(OperatorAccount.totp_secret)).one()
            assert secret != old_secret and secret in second
            clock.at += timedelta(seconds=totp.STEP_SECONDS)
            assert _sign_in(browser, _code(secret, clock.at)).status_code == 303
        finally:
            web.app.dependency_overrides.pop(get_db, None)
    assert _actions(sessions)[:3] == [
        "operator.account_created",
        "operator.recovery_code_used",
        "operator.signed_in",
    ]
    assert "operator.factors_reset" in _actions(sessions)


def test_cli_refuses_a_weak_password_a_taken_name_and_an_unknown_one(
    sessions, monkeypatch, capsys
):
    monkeypatch.setattr(cli, "SessionLocal", sessions)
    monkeypatch.setattr(cli, "getpass", lambda _prompt: "kurz")
    assert cli.main(["add", "maxi"]) == 1
    assert "mindestens" in capsys.readouterr().err
    monkeypatch.setattr(cli, "getpass", lambda _prompt: PASSWORD)
    assert cli.main(["add", "Maxi Muster"]) == 1
    assert "2 to 40 characters" in capsys.readouterr().err
    assert cli.main(["add", "maxi"]) == 0
    assert cli.main(["add", "MAXI"]) == 1
    assert "exists already" in capsys.readouterr().err
    assert cli.main(["reset", "nobody"]) == 1
    assert "No account named nobody" in capsys.readouterr().err
    answers = iter([PASSWORD, PASSWORD + "x"])
    monkeypatch.setattr(cli, "getpass", lambda _prompt: next(answers))
    assert cli.main(["reset", "maxi"]) == 1
    assert "differ" in capsys.readouterr().err


def test_the_operator_migration_goes_up_and_down(operator_url):
    cfg = _config(operator_url)
    command.upgrade(cfg, "head")
    engine = create_engine(operator_url)
    try:
        assert set(inspect(engine).get_table_names()) >= TABLES
        command.downgrade(cfg, "base")
        assert not TABLES & set(inspect(engine).get_table_names())
        command.upgrade(cfg, "head")
        assert set(inspect(engine).get_table_names()) >= TABLES
    finally:
        engine.dispose()


def test_the_models_match_the_migration(sessions, operator_url):
    """Alembic's own comparison: nothing in api/dashboard/models.py that the
    migration does not create, and the other way round."""
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    from api.dashboard.models import Base

    engine = create_engine(operator_url)
    try:
        with engine.connect() as conn:
            context = MigrationContext.configure(conn, opts={"compare_type": True})
            assert compare_metadata(context, Base.metadata) == []
    finally:
        engine.dispose()
