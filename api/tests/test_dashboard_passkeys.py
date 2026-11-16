"""Passkeys, step-up and the push on a changed factor (#571, docs/23 §5),
against a scratch operator database, with a software authenticator
(`passkey_device.py`) in place of the operator's device."""

from datetime import timedelta

import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from api.dashboard import access, passkeys, push
from api.dashboard import app as web
from api.dashboard.config import settings
from api.dashboard.db import get_db
from api.dashboard.models import OperatorAudit, OperatorChallenge, OperatorPasskey
from api.tests.passkey_device import Device
from api.tests.test_dashboard_login import (
    PASSWORD,
    Clock,
    _actions,
    _code,
    _config,
    _sign_in,
)
from evals.scratch_db import create_scratch_db, drop_scratch_db

ORIGIN = "http://localhost:8002"


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


@pytest.fixture
def clock(monkeypatch):
    moving = Clock()
    monkeypatch.setattr(web, "now", moving)
    return moving


@pytest.fixture
def client(sessions, clock, monkeypatch):
    def db():
        session = sessions()
        try:
            yield session
        finally:
            session.close()

    monkeypatch.setattr(settings, "operator_origin", ORIGIN)
    web.app.dependency_overrides[get_db] = db
    yield TestClient(web.app)
    web.app.dependency_overrides.pop(get_db, None)


@pytest.fixture
def factors(sessions):
    with sessions() as db:
        return access.create_account(db, "maxi", PASSWORD)


@pytest.fixture
def pushes(monkeypatch):
    """What would have gone to the `ops` topic."""
    sent: list[str] = []

    def notify(title: str, text: str) -> bool:
        sent.append(text)
        return True

    monkeypatch.setattr(push, "notify", notify)
    return sent


def _csrf(client) -> str:
    return client.cookies.get("ops_csrf", "")


def _post(client, path: str, **fields: str):
    return client.post(
        path, data={"csrf_token": _csrf(client), **fields}, follow_redirects=False
    )


def _signed_in(client, factors, clock) -> None:
    assert _sign_in(client, _code(factors.totp_secret, clock.at)).status_code == 303


def _register(client, device: Device, name: str = "Laptop", **bent):
    options = _post(client, "/factors/passkeys/options")
    assert options.status_code == 200, options.text
    answer = device.create(options.text, ORIGIN, **bent)
    return _post(client, "/factors/passkeys", name=name, passkey=answer)


def _passkey_sign_in(client, device: Device, password: str = PASSWORD, **bent):
    client.get("/login")
    options = _post(client, "/login/passkey/options")
    assert options.status_code == 200, options.text
    return _post(
        client,
        "/login",
        name="maxi",
        password=password,
        passkey=device.get(options.text, ORIGIN, **bent),
    )


def test_a_passkey_is_registered_and_then_signs_in(
    client, factors, clock, sessions, pushes
):
    _signed_in(client, factors, clock)
    device = Device()
    answer = _register(client, device)
    assert answer.headers["location"] == "/factors?note=passkey-added"
    assert "Laptop" in client.get("/factors").text
    assert "operator.passkey_added" in _actions(sessions)
    assert pushes == ["maxi: new passkey 'Laptop'"]
    client.cookies.clear()
    answer = _passkey_sign_in(client, device)
    assert answer.status_code == 303 and answer.headers["location"] == "/"
    with sessions() as db:
        row = db.scalars(select(OperatorPasskey)).one()
        signed = db.scalars(
            select(OperatorAudit).where(OperatorAudit.action == "operator.signed_in")
        ).all()
        assert db.scalars(select(OperatorChallenge)).all() == []
    assert row.sign_count == 2 and row.last_used_at == clock.at
    assert signed[-1].detail["factor"] == "passkey"


def test_the_password_alone_never_signs_in_with_a_passkey_registered(
    client, factors, clock
):
    _signed_in(client, factors, clock)
    device = Device()
    _register(client, device)
    client.cookies.clear()
    client.get("/login")
    for fields in ({}, {"passkey": "{}"}):
        answer = _post(client, "/login", name="maxi", password=PASSWORD, **fields)
        assert answer.status_code == 401 and web.FAILED in answer.text
    # A wrong password with a right passkey is refused the same way.
    answer = _passkey_sign_in(client, device, password="falsches-passwort")
    assert answer.status_code == 401 and web.FAILED in answer.text


def test_an_answer_is_taken_once(client, factors, clock):
    _signed_in(client, factors, clock)
    device = Device()
    _register(client, device)
    client.cookies.clear()
    client.get("/login")
    options = _post(client, "/login/passkey/options")
    answer = device.get(options.text, ORIGIN)
    fields = {"name": "maxi", "password": PASSWORD, "passkey": answer}
    assert _post(client, "/login", **fields).status_code == 303
    client.cookies.delete("ops_session")
    assert _post(client, "/login", **fields).status_code == 401


def test_a_challenge_expires(client, factors, clock):
    _signed_in(client, factors, clock)
    device = Device()
    _register(client, device)
    client.cookies.clear()
    client.get("/login")
    options = _post(client, "/login/passkey/options")
    clock.at += passkeys.CHALLENGE_LIFE
    answer = _post(
        client,
        "/login",
        name="maxi",
        password=PASSWORD,
        passkey=device.get(options.text, ORIGIN),
    )
    assert answer.status_code == 401


def test_step_up_is_needed_after_five_minutes(client, factors, clock, sessions):
    _signed_in(client, factors, clock)
    clock.at += timedelta(minutes=4, seconds=59)
    assert _post(client, "/factors/passkeys/options").status_code == 200
    clock.at += timedelta(seconds=1)
    stale = _post(client, "/factors/passkeys/options", back="/factors")
    assert stale.status_code == 303
    assert stale.headers["location"] == "/step-up?next=%2Ffactors"
    done = _post(
        client, "/step-up", next="/factors", code=_code(factors.totp_secret, clock.at)
    )
    assert done.status_code == 303 and done.headers["location"] == "/factors"
    assert _post(client, "/factors/passkeys/options").status_code == 200
    assert _actions(sessions)[-1] == "operator.stepped_up"
