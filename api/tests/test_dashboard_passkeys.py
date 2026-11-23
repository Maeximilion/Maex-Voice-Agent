"""Passkeys, step-up and the push on a changed factor (#571, docs/23 §5),
against a scratch operator database, with a software authenticator
(`passkey_device.py`) in place of the operator's device."""

import json
import re
import threading
import uuid
from datetime import timedelta
from typing import Any

import httpx
import pytest
from alembic import command
from fastapi import Depends
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import sessionmaker

from api.dashboard import access, cli, passkeys, push
from api.dashboard import app as web
from api.dashboard.config import settings
from api.dashboard.db import get_db
from api.dashboard.models import (
    OperatorAccount,
    OperatorAudit,
    OperatorChallenge,
    OperatorDevice,
    OperatorPasskey,
    OperatorSession,
)
from api.domain.access.hashing import token_hash
from api.tests.passkey_device import Device
from api.tests.test_dashboard_login import (
    PASSWORD,
    START,
    Clock,
    _actions,
    _code,
    _config,
    _sign_in,
    _wrong,
)
from evals.scratch_db import create_scratch_db, drop_scratch_db

ORIGIN = "http://localhost:8002"
EVIL_ORIGIN = "https://ops.example.com.evil.example"
OTHER_CHALLENGE = "A" * 43

# What a browser or an attacker can bend in an answer, one thing at a time.
BENT = [
    pytest.param({"origin": EVIL_ORIGIN}, id="another origin"),
    pytest.param({"cross_origin": True}, id="cross origin"),
    pytest.param({"rp_id": "evil.example"}, id="another relying party"),
    pytest.param({"verified": False}, id="no user verification"),
    pytest.param({"challenge": OTHER_CHALLENGE}, id="another challenge"),
]


def _bend(answer: str, **response: str) -> str:
    parsed = json.loads(answer)
    parsed["response"].update(response)
    return json.dumps(parsed)


def _garbage(field: str) -> Any:
    return lambda answer: _bend(answer, **{field: "AAAA"})


def _garbage_for(field: str) -> list[Any]:
    return [
        pytest.param(lambda answer: "", id="empty"),
        pytest.param(lambda answer: "{}", id="empty object"),
        pytest.param(lambda answer: "[]", id="list"),
        pytest.param(lambda answer: "null", id="null"),
        pytest.param(lambda answer: "x", id="not json"),
        pytest.param(lambda answer: answer[:60], id="truncated"),
        pytest.param(_garbage(field), id="broken response"),
    ]


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
    """What would have gone to the `ops` topic, but for the pushes of a sign-in
    from a new browser and of a sign-in or step-up with a TOTP code: most
    tests make them, and they have tests of their own (#888 A1)."""
    sent: list[str] = []

    def queue(db: Any, text: str, at: Any) -> None:
        if web.NEW_BROWSER not in text and web.BY_CODE not in text:
            sent.append(text)

    monkeypatch.setattr(push, "queue", queue)
    return sent


@pytest.fixture
def guarded():
    """A route behind `require_step_up` on every method, to see the dependency
    itself: the app has no GET that needs it yet."""

    def handler() -> PlainTextResponse:
        return PlainTextResponse("done")

    web.app.add_api_route(
        "/guarded",
        handler,
        methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        dependencies=[Depends(web.require_step_up)],
    )
    route = web.app.router.routes[-1]
    yield
    web.app.router.routes.remove(route)


def _csrf(client) -> str:
    return client.cookies.get("ops_csrf", "")


def _post(client, path: str, **fields: str):
    return client.post(
        path, data={"csrf_token": _csrf(client), **fields}, follow_redirects=False
    )


def _signed_in(client, factors, clock) -> None:
    assert _sign_in(client, _code(factors.totp_secret, clock.at)).status_code == 303


def _register(client, device: Device, name: str = "Laptop", origin=ORIGIN, **bent):
    options = _post(client, "/factors/passkeys/options")
    assert options.status_code == 200, options.text
    answer = device.create(options.text, origin, **bent)
    return _post(client, "/factors/passkeys", name=name, passkey=answer)


def _asked(client) -> str:
    client.get("/login")
    options = _post(client, "/login/passkey/options")
    assert options.status_code == 200, options.text
    return options.text


def _passkey_sign_in(
    client, device: Device, password: str = PASSWORD, origin=ORIGIN, **bent
):
    passkey = device.get(_asked(client), origin, **bent)
    return _post(client, "/login", name="maxi", password=password, passkey=passkey)


def _with_passkey(client, factors, clock, name="Laptop", device=None) -> Device:
    """A passkey registered by a TOTP sign-in; the browser is signed out after."""
    _signed_in(client, factors, clock)
    device = device or Device()
    answer = _register(client, device, name)
    assert answer.headers["location"] == "/factors?note=passkey-added"
    client.cookies.clear()
    return device


def _passkey_step_up(client, device: Device) -> None:
    """A passkey step-up of this session: once the account has a passkey, a
    change of its factors needs one (D37)."""
    asked = _post(client, "/step-up/passkey/options")
    answer = _post(client, "/step-up", passkey=device.get(asked.text, ORIGIN))
    assert answer.status_code == 303, answer.text


def _signed_in_with(client, factors, clock, count: int, totp: bool = True):
    """Signed in with `count` passkeys, `Key 0` and so on, and TOTP on or off;
    stepped up with `Key 0` once it is there."""
    _signed_in(client, factors, clock)
    devices = [Device() for _ in range(count)]
    for number, device in enumerate(devices):
        answer = _register(client, device, f"Key {number}")
        assert answer.headers["location"] == "/factors?note=passkey-added"
        if number == 0:
            _passkey_step_up(client, device)
    if not totp:
        answer = _post(client, "/factors/totp/remove")
        assert answer.headers["location"] == "/factors?note=totp-removed"
    return devices


def _anna(sessions, clock) -> TestClient:
    """A second account, signed in on a browser of its own."""
    with sessions() as db:
        made = access.create_account(db, "anna", PASSWORD)
    browser = TestClient(web.app)
    code = _code(made.totp_secret, clock.at)
    assert _sign_in(browser, code, name="anna").status_code == 303
    return browser


def _refused(client, answer, message: str = web.FAILED) -> None:
    assert answer.status_code == 401 and message in answer.text
    assert client.cookies.get("ops_session") is None


def _rows(sessions) -> list[OperatorPasskey]:
    with sessions() as db:
        return list(db.scalars(select(OperatorPasskey).order_by(OperatorPasskey.name)))


def _audited(sessions, action: str) -> list[OperatorAudit]:
    with sessions() as db:
        return list(
            db.scalars(
                select(OperatorAudit)
                .where(OperatorAudit.action == action)
                .order_by(OperatorAudit.id)
            )
        )


def _recovery_left(sessions) -> int:
    with sessions() as db:
        account = db.scalars(select(OperatorAccount)).one()
        return access.recovery_left(db, account.id)


def _pending(sessions) -> str | None:
    with sessions() as db:
        found = [p for p in db.scalars(select(OperatorSession.pending_totp)) if p]
    return found[0] if found else None


def _call(client, method: str, path: str, **fields: str):
    return client.request(
        method,
        path,
        data={"csrf_token": _csrf(client), **fields},
        follow_redirects=False,
    )


def _step_up_with_code(client, factors, clock):
    return _post(client, "/step-up", code=_code(factors.totp_secret, clock.at))


# --- the first five tests ---------------------------------------------------------


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


# --- registration -----------------------------------------------------------------


@pytest.mark.parametrize("bent", BENT)
def test_a_bent_registration_is_refused_and_adds_nothing(
    client, factors, clock, sessions, pushes, bent
):
    _signed_in(client, factors, clock)
    answer = _register(client, Device(), **bent)
    assert answer.status_code == 303
    assert answer.headers["location"] == "/factors?note=passkey-refused"
    assert _rows(sessions) == [] and pushes == []
    assert "operator.passkey_added" not in _actions(sessions)


def test_the_same_registration_posted_twice_adds_one_row(
    client, factors, clock, sessions, pushes
):
    _signed_in(client, factors, clock)
    device = Device()
    answer = device.create(_post(client, "/factors/passkeys/options").text, ORIGIN)
    first = _post(client, "/factors/passkeys", name="Laptop", passkey=answer)
    _passkey_step_up(client, device)
    second = _post(client, "/factors/passkeys", name="Laptop", passkey=answer)
    assert first.headers["location"] == "/factors?note=passkey-added"
    assert second.headers["location"] == "/factors?note=passkey-refused"
    assert len(_rows(sessions)) == 1 and len(pushes) == 1


def test_a_failed_registration_spends_the_challenge(client, factors, clock, sessions):
    _signed_in(client, factors, clock)
    device = Device()
    options = _post(client, "/factors/passkeys/options").text
    bad = device.create(options, ORIGIN, verified=False)
    refused = _post(client, "/factors/passkeys", name="A", passkey=bad)
    assert "passkey-refused" in refused.headers["location"]
    good = device.create(options, ORIGIN)
    answer = _post(client, "/factors/passkeys", name="A", passkey=good)
    assert answer.headers["location"] == "/factors?note=passkey-refused"
    assert _rows(sessions) == []


def test_a_registration_challenge_expires(client, factors, clock, sessions):
    _signed_in(client, factors, clock)
    options = _post(client, "/factors/passkeys/options").text
    clock.at += passkeys.CHALLENGE_LIFE
    assert _step_up_with_code(client, factors, clock).status_code == 303
    mine = Device().create(options, ORIGIN)
    answer = _post(client, "/factors/passkeys", name="Laptop", passkey=mine)
    assert answer.headers["location"] == "/factors?note=passkey-refused"
    assert _rows(sessions) == []


def test_a_device_registers_once_per_account(client, factors, clock, sessions):
    _signed_in(client, factors, clock)
    device = Device()
    assert "passkey-added" in _register(client, device).headers["location"]
    _passkey_step_up(client, device)
    answer = _register(client, device, "Again")
    assert answer.headers["location"] == "/factors?note=passkey-refused"
    assert len(_rows(sessions)) == 1


def test_a_credential_of_another_account_is_refused(
    client, factors, clock, sessions, pushes
):
    _signed_in(client, factors, clock)
    anna = _anna(sessions, clock)
    device = Device()
    assert "passkey-added" in _register(client, device).headers["location"]
    answer = _register(anna, device, "Anna")
    assert answer.headers["location"] == "/factors?note=passkey-refused"
    assert len(_rows(sessions)) == 1 and pushes == ["maxi: new passkey 'Laptop'"]


@pytest.mark.parametrize("name", ["", "x" * 41, "   "])
def test_a_bad_passkey_name_is_refused(client, factors, clock, sessions, name):
    _signed_in(client, factors, clock)
    answer = _register(client, Device(), name)
    assert answer.headers["location"] == "/factors?note=passkey-refused"
    assert _rows(sessions) == []


def test_a_name_is_stored_trimmed_and_forty_characters_fit(
    client, factors, clock, sessions
):
    _signed_in(client, factors, clock)
    phone = Device()
    assert "passkey-added" in _register(client, phone, "  Phone  ").headers["location"]
    _passkey_step_up(client, phone)
    assert "passkey-added" in _register(client, Device(), "x" * 40).headers["location"]
    assert [row.name for row in _rows(sessions)] == ["Phone", "x" * 40]


def test_no_more_passkeys_than_the_limit(client, factors, clock, sessions, monkeypatch):
    monkeypatch.setattr(passkeys, "PASSKEYS_MAX", 2)
    _signed_in_with(client, factors, clock, 2)
    answer = _register(client, Device(), "Third")
    assert answer.headers["location"] == "/factors?note=passkey-refused"
    assert len(_rows(sessions)) == 2


@pytest.mark.parametrize("make", _garbage_for("attestationObject"))
def test_garbage_in_a_registration_never_fails_the_server(
    client, factors, clock, sessions, make
):
    _signed_in(client, factors, clock)
    options = _post(client, "/factors/passkeys/options").text
    mine = make(Device().create(options, ORIGIN))
    answer = _post(client, "/factors/passkeys", name="Laptop", passkey=mine)
    assert answer.status_code == 303
    assert answer.headers["location"] == "/factors?note=passkey-refused"
    assert _rows(sessions) == []


def test_an_answer_for_another_challenge_of_the_session_does_not_register(
    client, factors, clock, sessions
):
    _signed_in_with(client, factors, clock, 1)
    registration = _post(client, "/factors/passkeys/options").text
    step_up = json.loads(_post(client, "/step-up/passkey/options").text)
    answer = Device().create(registration, ORIGIN, challenge=step_up["challenge"])
    refused = _post(client, "/factors/passkeys", name="Second", passkey=answer)
    assert refused.headers["location"] == "/factors?note=passkey-refused"
    assert len(_rows(sessions)) == 1


@pytest.mark.parametrize(
    "path",
    [
        "/factors/passkeys/options",
        "/factors/passkeys",
        "/factors/passkeys/remove",
        "/factors/totp/new",
        "/factors/totp",
        "/factors/totp/remove",
    ],
)
class TestAWriteOnTheFactors:
    def test_needs_a_session(self, client, factors, path):
        assert client.post(path, data={}).status_code == 401

    def test_needs_the_page_token(self, client, factors, clock, path):
        _signed_in(client, factors, clock)
        assert client.post(path, data={"name": "x"}).status_code == 403

    def test_must_come_from_this_site(self, client, factors, clock, path):
        _signed_in(client, factors, clock)
        answer = client.post(
            path,
            data={"csrf_token": _csrf(client)},
            headers={"Origin": "https://evil.example.com"},
        )
        assert answer.status_code == 403


# --- sign-in with a passkey -------------------------------------------------------


def test_the_signature_of_another_key_is_refused(client, factors, clock, sessions):
    device = _with_passkey(client, factors, clock)
    copy = Device()
    copy.credential_id, copy.user_handle = device.credential_id, device.user_handle
    _refused(client, _passkey_sign_in(client, copy))


def test_an_answer_replayed_under_the_same_attempt_is_refused(client, factors, clock):
    device = _with_passkey(client, factors, clock)
    answer = device.get(_asked(client), ORIGIN)
    attempt = client.cookies.get("ops_attempt")
    fields = {"name": "maxi", "password": PASSWORD, "passkey": answer}
    assert _post(client, "/login", **fields).status_code == 303
    client.cookies.clear()
    client.get("/login")
    client.cookies.set("ops_attempt", attempt)
    _refused(client, _post(client, "/login", **fields))


@pytest.mark.parametrize("bent", BENT)
def test_a_bent_passkey_sign_in_is_refused(client, factors, clock, sessions, bent):
    device = _with_passkey(client, factors, clock)
    _refused(client, _passkey_sign_in(client, device, **bent))
    reasons = [
        row.detail["reason"] for row in _audited(sessions, "operator.sign_in_failed")
    ]
    assert reasons == ["factor"]


@pytest.mark.parametrize("own_attempt", [False, True])
def test_an_answer_posted_by_another_browser_is_refused(
    client, factors, clock, own_attempt
):
    device = _with_passkey(client, factors, clock)
    passkey = device.get(_asked(client), ORIGIN)
    other = TestClient(web.app)
    other.get("/login")
    if own_attempt:
        assert _post(other, "/login/passkey/options").status_code == 200
    fields = {"name": "maxi", "password": PASSWORD, "passkey": passkey}
    _refused(other, _post(other, "/login", **fields))
    # The challenge of the browser that asked is still its own.
    assert _post(client, "/login", **fields).status_code == 303


def test_the_passkey_of_another_account_does_not_open_this_one(
    client, factors, sessions, clock
):
    anna = _anna(sessions, clock)
    device = Device()
    assert "passkey-added" in _register(anna, device, "Anna").headers["location"]
    _refused(client, _passkey_sign_in(client, device))


@pytest.mark.parametrize(
    ("signed_before", "sign_count"),
    [(0, 1), (1, 1), (1, 0)],
    ids=["standing still", "going back", "reset to zero"],
)
def test_a_counter_that_does_not_grow_is_refused_and_audited(
    client, factors, clock, sessions, signed_before, sign_count
):
    device = _with_passkey(client, factors, clock)
    for _ in range(signed_before):
        assert _passkey_sign_in(client, device).status_code == 303
        client.cookies.clear()
    _refused(client, _passkey_sign_in(client, device, sign_count=sign_count))
    assert _rows(sessions)[0].sign_count == 1 + signed_before
    failed = _audited(sessions, "operator.sign_in_failed")
    assert failed[-1].detail["reason"] == "counter"


def test_a_synced_passkey_with_a_counter_of_zero_keeps_signing_in(
    client, factors, clock, sessions
):
    device = _with_passkey(client, factors, clock, device=Device(counts=False))
    for _ in range(2):
        assert _passkey_sign_in(client, device).status_code == 303
        client.cookies.clear()
    assert _rows(sessions)[0].sign_count == 0


@pytest.mark.parametrize("make", _garbage_for("authenticatorData"))
def test_garbage_in_a_passkey_sign_in_never_fails_the_server(
    client, factors, clock, make
):
    device = _with_passkey(client, factors, clock)
    passkey = make(device.get(_asked(client), ORIGIN))
    answer = _post(client, "/login", name="maxi", password=PASSWORD, passkey=passkey)
    _refused(client, answer)


def test_three_failed_passkey_sign_ins_bring_the_wait(client, factors, clock):
    device = _with_passkey(client, factors, clock)
    for _ in range(3):
        _refused(client, _passkey_sign_in(client, device, verified=False))
    _refused(client, _passkey_sign_in(client, device), web.WAIT.format(minutes=1))
    clock.at += timedelta(minutes=1, seconds=1)
    assert _passkey_sign_in(client, device).status_code == 303


def test_a_sign_in_challenge_does_not_step_up(client, factors, clock):
    device = _with_passkey(client, factors, clock)
    assert _passkey_sign_in(client, device).status_code == 303
    clock.at += timedelta(minutes=6)
    asked = _post(client, "/login/passkey/options")
    answer = _post(client, "/step-up", passkey=device.get(asked.text, ORIGIN))
    assert answer.status_code == 401 and web.FAILED in answer.text
    stale = _post(client, "/factors/totp/new")
    assert stale.status_code == 303 and "step-up" in stale.headers["location"]


def test_a_step_up_challenge_does_not_sign_in(client, factors, clock):
    device = _with_passkey(client, factors, clock)
    assert _passkey_sign_in(client, device).status_code == 303
    asked = _post(client, "/step-up/passkey/options")
    passkey = device.get(asked.text, ORIGIN)
    other = TestClient(web.app)
    other.get("/login")
    assert _post(other, "/login/passkey/options").status_code == 200
    fields = {"name": "maxi", "password": PASSWORD, "passkey": passkey}
    _refused(other, _post(other, "/login", **fields))


def test_the_attempt_cookie_goes_with_a_successful_sign_in(client, factors, clock):
    device = _with_passkey(client, factors, clock)
    passkey = device.get(_asked(client), ORIGIN)
    assert client.cookies.get("ops_attempt")
    fields = {"name": "maxi", "password": PASSWORD, "passkey": passkey}
    assert _post(client, "/login", **fields).status_code == 303
    assert client.cookies.get("ops_attempt") is None


def test_outside_dev_and_test_the_attempt_cookie_is_host_bound_and_secure(
    client, factors, clock, sessions, monkeypatch
):
    secret = "p" * 40
    monkeypatch.setattr(settings, "env", "prod")
    monkeypatch.setattr(settings, "operator_proxy_secret", secret)
    monkeypatch.setattr(settings, "operator_origin", "https://testserver")
    proxy = {"X-Maex-Proxy-Secret": secret}
    browser = TestClient(web.app, base_url="https://testserver")

    def post(path: str, **fields: str):
        token = browser.cookies.get("__Host-ops_csrf", "")
        return browser.post(
            path,
            data={"csrf_token": token, **fields},
            headers=proxy,
            follow_redirects=False,
        )

    browser.get("/login", headers=proxy)
    code = _code(factors.totp_secret, clock.at)
    assert post("/login", name="maxi", password=PASSWORD, code=code).status_code == 303
    device = Device()
    options = post("/factors/passkeys/options")
    answer = device.create(options.text, "https://testserver")
    assert (
        "passkey-added"
        in post("/factors/passkeys", name="Laptop", passkey=answer).headers["location"]
    )
    browser.cookies.clear()
    browser.get("/login", headers=proxy)
    asked = post("/login/passkey/options")
    cookie = asked.headers["set-cookie"]
    assert "__Host-ops_attempt" in cookie and "Secure" in cookie
    passkey = device.get(asked.text, "https://testserver")
    done = post("/login", name="maxi", password=PASSWORD, passkey=passkey)
    assert done.status_code == 303
    assert browser.cookies.get("__Host-ops_attempt") is None


# --- step-up ----------------------------------------------------------------------


@pytest.mark.parametrize("how", ["totp", "passkey", "recovery"])
def test_any_sign_in_factor_is_fresh_for_five_minutes(
    client, factors, clock, guarded, how
):
    if how == "passkey":
        device = _with_passkey(client, factors, clock)
        assert _passkey_sign_in(client, device).status_code == 303
    elif how == "recovery":
        assert _sign_in(client, factors.recovery_codes[0]).status_code == 303
    else:
        _signed_in(client, factors, clock)
    assert _call(client, "POST", "/guarded").text == "done"
    clock.at += timedelta(minutes=4, seconds=59)
    assert _call(client, "POST", "/guarded").text == "done"
    clock.at += timedelta(seconds=1)
    assert _call(client, "POST", "/guarded").status_code == 303


@pytest.mark.parametrize("method", ["GET", "POST", "PUT", "PATCH", "DELETE"])
def test_a_stale_session_is_sent_to_the_step_up_by_any_method(
    client, factors, clock, guarded, method
):
    _signed_in(client, factors, clock)
    assert _call(client, method, "/guarded").text == "done"
    clock.at += timedelta(minutes=5)
    answer = _call(client, method, "/guarded?x=1&y=2", back="/factors?note=x")
    assert answer.status_code == 303
    own = "%2Fguarded%3Fx%3D1%26y%3D2"
    back = "%2Ffactors%3Fnote%3Dx"
    assert (
        answer.headers["location"]
        == f"/step-up?next={own if method == 'GET' else back}"
    )


@pytest.mark.parametrize("fields", [{}, {"back": "https://evil.example"}])
def test_a_write_without_a_local_back_goes_back_to_the_start_page(
    client, factors, clock, guarded, fields
):
    _signed_in(client, factors, clock)
    clock.at += timedelta(minutes=5)
    answer = _call(client, "POST", "/guarded", **fields)
    assert answer.headers["location"] == "/step-up?next=%2F"


@pytest.mark.parametrize(
    ("passkey_count", "totp", "button", "field"),
    [(0, True, False, True), (1, True, True, True), (2, False, True, False)],
    ids=["totp only", "both", "passkeys only"],
)
def test_the_step_up_page_offers_only_the_factors_the_account_has(
    client, factors, clock, passkey_count, totp, button, field
):
    _signed_in_with(client, factors, clock, passkey_count, totp)
    clock.at += timedelta(minutes=5)
    page = client.get("/step-up?next=/factors")
    assert page.status_code == 200 and 'value="/factors"' in page.text
    assert ('data-passkey="get"' in page.text) is button
    assert ('name="code"' in page.text) is field


def test_a_passkey_steps_up(client, factors, clock, sessions):
    device = _signed_in_with(client, factors, clock, 1)[0]
    clock.at += timedelta(minutes=5)
    assert _post(client, "/factors/totp/new").status_code == 303
    asked = _post(client, "/step-up/passkey/options")
    answer = _post(
        client, "/step-up", next="/factors", passkey=device.get(asked.text, ORIGIN)
    )
    assert answer.status_code == 303 and answer.headers["location"] == "/factors"
    assert _post(client, "/factors/passkeys/options").status_code == 200
    assert _audited(sessions, "operator.stepped_up")[-1].detail["factor"] == "passkey"


def test_a_recovery_code_is_no_step_up(client, factors, clock, sessions):
    _signed_in(client, factors, clock)
    clock.at += timedelta(minutes=5)
    answer = _post(client, "/step-up", code=factors.recovery_codes[0])
    assert answer.status_code == 401 and web.FAILED in answer.text
    assert _recovery_left(sessions) == 10
    assert _post(client, "/factors/passkeys/options").status_code == 303


def test_a_wrong_step_up_code_is_audited(client, factors, clock, sessions):
    _signed_in(client, factors, clock)
    clock.at += timedelta(minutes=5)
    wrong = _wrong(factors.totp_secret, clock.at)
    assert _post(client, "/step-up", code=wrong).status_code == 401
    failed = _audited(sessions, "operator.step_up_failed")
    assert [row.detail["reason"] for row in failed] == ["factor"]
    assert _post(client, "/factors/passkeys/options").status_code == 303


def test_three_wrong_step_up_codes_bring_the_wait(client, factors, clock):
    _signed_in(client, factors, clock)
    clock.at += timedelta(minutes=5)
    for _ in range(3):
        wrong = _wrong(factors.totp_secret, clock.at)
        assert _post(client, "/step-up", code=wrong).status_code == 401
    answer = _step_up_with_code(client, factors, clock)
    assert answer.status_code == 401 and web.WAIT.format(minutes=1) in answer.text
    clock.at += timedelta(minutes=1, seconds=1)
    assert _step_up_with_code(client, factors, clock).status_code == 303


def test_the_code_that_signed_in_cannot_step_up_but_the_next_one_can(
    client, factors, clock
):
    _signed_in(client, factors, clock)
    assert _step_up_with_code(client, factors, clock).status_code == 401
    clock.at += timedelta(seconds=30)
    assert _step_up_with_code(client, factors, clock).status_code == 303


def test_a_step_up_freshens_only_its_own_session(client, factors, clock):
    _signed_in(client, factors, clock)
    clock.at += timedelta(seconds=30)
    other = TestClient(web.app)
    assert _sign_in(other, _code(factors.totp_secret, clock.at)).status_code == 303
    clock.at += timedelta(minutes=5, seconds=30)
    assert _step_up_with_code(client, factors, clock).status_code == 303
    assert _post(client, "/factors/passkeys/options").status_code == 200
    assert _post(other, "/factors/passkeys/options").status_code == 303


@pytest.mark.parametrize(
    "bad",
    [
        "https://evil.example",
        "//evil.example",
        "/\\evil.example",
        "javascript:alert(1)",
        "",
    ],
)
def test_a_step_up_never_leads_to_another_site(client, factors, clock, bad):
    _signed_in(client, factors, clock)
    clock.at += timedelta(minutes=5)
    answer = _post(
        client, "/step-up", next=bad, code=_code(factors.totp_secret, clock.at)
    )
    assert answer.status_code == 303 and answer.headers["location"] == "/"
    assert "evil" not in client.get(f"/step-up?next={bad}").text


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/factors", "/factors"),
        ("/customers/abc?x=1", "/customers/abc?x=1"),
        ("/", "/"),
        ("https://evil.example", "/"),
        ("//evil.example", "/"),
        ("/\\evil.example", "/"),
        ("javascript:alert(1)", "/"),
        ("", "/"),
        ("/über", "/"),
        ("/a\nb", "/"),
    ],
)
def test_local_keeps_a_path_of_this_site_and_nothing_else(path, expected):
    assert web._local(path) == expected


# --- the two-passkey rule and TOTP ------------------------------------------------


@pytest.mark.parametrize("count", [0, 1])
def test_totp_stays_without_two_passkeys(
    client, factors, clock, sessions, pushes, count
):
    _signed_in_with(client, factors, clock, count)
    answer = _post(client, "/factors/totp/remove")
    assert answer.headers["location"] == "/factors?note=factor-needed"
    assert "operator.totp_removed" not in _actions(sessions)
    assert len(pushes) == count
    client.cookies.clear()
    clock.at += timedelta(seconds=30)
    assert _sign_in(client, _code(factors.totp_secret, clock.at)).status_code == 303


def test_totp_goes_with_two_passkeys_and_a_passkey_signs_in_after(
    client, factors, clock, sessions, pushes
):
    devices = _signed_in_with(client, factors, clock, 2, totp=False)
    assert pushes[-1] == "maxi: TOTP removed"
    assert len(_audited(sessions, "operator.totp_removed")) == 1
    client.cookies.clear()
    clock.at += timedelta(seconds=30)
    _refused(client, _sign_in(client, _code(factors.totp_secret, clock.at)))
    assert _passkey_sign_in(client, devices[0]).status_code == 303


def test_without_totp_a_passkey_goes_only_while_three_are_left(
    client, factors, clock, sessions, pushes
):
    _signed_in_with(client, factors, clock, 3, totp=False)
    first, second, third = _rows(sessions)
    answer = _post(client, "/factors/passkeys/remove", id=str(first.id))
    assert answer.headers["location"] == "/factors?note=passkey-removed"
    assert pushes[-1] == "maxi: passkey 'Key 0' removed"
    answer = _post(client, "/factors/passkeys/remove", id=str(second.id))
    assert answer.headers["location"] == "/factors?note=factor-needed"
    assert [row.id for row in _rows(sessions)] == [second.id, third.id]
    removed = _audited(sessions, "operator.passkey_removed")
    assert [row.detail["passkey"] for row in removed] == ["Key 0"]


def test_with_totp_every_passkey_may_go(client, factors, clock, sessions):
    _signed_in_with(client, factors, clock, 2)
    for row in _rows(sessions):
        answer = _post(client, "/factors/passkeys/remove", id=str(row.id))
        assert answer.headers["location"] == "/factors?note=passkey-removed"
    assert _rows(sessions) == []
    client.cookies.clear()
    clock.at += timedelta(seconds=30)
    assert _sign_in(client, _code(factors.totp_secret, clock.at)).status_code == 303


@pytest.mark.parametrize("which", ["another account", "not-a-uuid", "unknown"])
def test_a_passkey_that_is_not_ours_is_not_removed(
    client, factors, clock, sessions, pushes, which
):
    _signed_in_with(client, factors, clock, 1)
    anna = _anna(sessions, clock)
    assert "passkey-added" in _register(anna, Device(), "Anna").headers["location"]
    hers = next(row for row in _rows(sessions) if row.name == "Anna")
    wanted = {
        "another account": str(hers.id),
        "not-a-uuid": "not-a-uuid",
        "unknown": str(uuid.uuid4()),
    }[which]
    answer = _post(client, "/factors/passkeys/remove", id=wanted)
    assert answer.status_code == 303
    assert answer.headers["location"] == "/factors?note="
    assert len(_rows(sessions)) == 2 and len(pushes) == 2


def test_a_new_totp_secret_is_shown_only_while_the_session_is_fresh(
    client, factors, clock, sessions
):
    _signed_in(client, factors, clock)
    assert _post(client, "/factors/totp/new").headers["location"] == "/factors?note="
    pending = _pending(sessions)
    assert pending and pending != factors.totp_secret
    assert pending in client.get("/factors").text
    clock.at += timedelta(minutes=5)
    assert pending not in client.get("/factors").text


def test_a_new_totp_secret_replaces_the_old_one_after_its_own_code(
    client, factors, clock, sessions, pushes
):
    old = factors.totp_secret
    _signed_in(client, factors, clock)
    _post(client, "/factors/totp/new")
    pending = _pending(sessions)
    assert pending
    for code in (_code(old, clock.at), _wrong(pending, clock.at)):
        answer = _post(client, "/factors/totp", code=code)
        assert answer.headers["location"] == "/factors?note=code-wrong"
    assert _pending(sessions) == pending and pushes == []
    clock.at += timedelta(seconds=30)
    assert _sign_in(TestClient(web.app), _code(old, clock.at)).status_code == 303
    answer = _post(client, "/factors/totp", code=_code(pending, clock.at))
    assert answer.headers["location"] == "/factors?note=totp-set"
    # The sign-in with the old code above is a session of its own, and D39 ends it.
    assert pushes == ["maxi: new TOTP secret, other sessions ended: 1"]
    assert _pending(sessions) is None
    assert len(_audited(sessions, "operator.totp_set")) == 1
    clock.at += timedelta(seconds=30)
    before, after = TestClient(web.app), TestClient(web.app)
    _refused(before, _sign_in(before, _code(old, clock.at)))
    assert _sign_in(after, _code(pending, clock.at)).status_code == 303


# --- push and audit ---------------------------------------------------------------


def test_every_factor_change_is_audited_and_pushed(
    client, factors, clock, sessions, pushes
):
    _signed_in(client, factors, clock)
    keys = {name: Device() for name in "ABC"}
    _register(client, keys["A"], "A")
    _passkey_step_up(client, keys["A"])
    _register(client, keys["B"], "B")
    _post(client, "/factors/totp/new")
    _post(client, "/factors/totp", code=_code(_pending(sessions) or "", clock.at))
    second = next(row for row in _rows(sessions) if row.name == "B")
    _post(client, "/factors/passkeys/remove", id=str(second.id))
    _register(client, keys["C"], "C")
    _post(client, "/factors/totp/remove")
    assert pushes == [
        "maxi: new passkey 'A'",
        "maxi: new passkey 'B'",
        "maxi: new TOTP secret",
        "maxi: passkey 'B' removed",
        "maxi: new passkey 'C'",
        "maxi: TOTP removed",
    ]
    assert [a for a in _actions(sessions) if a != "operator.account_created"] == [
        "operator.signed_in",
        "operator.passkey_added",
        "operator.stepped_up",
        "operator.passkey_added",
        "operator.totp_set",
        "operator.passkey_removed",
        "operator.passkey_added",
        "operator.totp_removed",
    ]


def test_a_recovery_code_at_sign_in_is_audited_and_pushed(
    client, factors, sessions, pushes
):
    assert _sign_in(client, factors.recovery_codes[0]).status_code == 303
    assert pushes == ["maxi: a recovery code was used"]
    used = _audited(sessions, "operator.recovery_code_used")
    assert [row.detail["left"] for row in used] == [9]
    assert _audited(sessions, "operator.signed_in")[-1].detail["factor"] == "recovery"


def test_a_sign_in_from_a_new_browser_is_pushed_as_one(
    client, factors, clock, monkeypatch
):
    """#888 A1: a phished password and TOTP code signed in unseen. A known
    browser's sign-in with a code is pushed too, but not as a new browser."""
    sent: list[str] = []
    monkeypatch.setattr(push, "queue", lambda db, text, at: sent.append(text))
    _signed_in(client, factors, clock)
    assert sent == [f"maxi: {web.NEW_BROWSER} (totp)"]

    clock.at += timedelta(seconds=30)
    _signed_in(client, factors, clock)  # the same browser, its device cookie kept
    assert sent[1:] == [f"maxi: signed in {web.BY_CODE}"]
    sent.clear()

    client.cookies.clear()
    clock.at += timedelta(seconds=30)
    _signed_in(client, factors, clock)
    assert sent == [f"maxi: {web.NEW_BROWSER} (totp)"]


def test_a_failed_push_leaves_the_change_in_place_and_is_audited(
    client, factors, clock, sessions, monkeypatch
):
    def queue(db: Any, text: str, at: Any) -> None:
        raise httpx.ConnectError("down")

    monkeypatch.setattr(push, "queue", queue)
    _signed_in(client, factors, clock)
    answer = _register(client, Device())
    assert answer.headers["location"] == "/factors?note=passkey-added"
    assert len(_rows(sessions)) == 1
    failed = _audited(sessions, "operator.push_failed")
    assert [row.detail["what"] for row in failed] == [
        f"{web.NEW_BROWSER} (totp)",
        "new passkey 'Laptop'",
    ]


def test_a_failed_push_does_not_stop_a_recovery_sign_in(
    client, factors, sessions, monkeypatch
):
    def queue(db: Any, text: str, at: Any) -> None:
        raise httpx.ConnectError("down")

    monkeypatch.setattr(push, "queue", queue)
    assert _sign_in(client, factors.recovery_codes[0]).status_code == 303
    failed = _audited(sessions, "operator.push_failed")
    assert [row.detail["what"] for row in failed] == [
        "a recovery code was used",
        f"{web.NEW_BROWSER} (recovery)",
    ]


# --- the challenges table ---------------------------------------------------------


def test_a_new_challenge_replaces_the_holders_earlier_one(sessions):
    with sessions() as db:
        first = passkeys.issue(db, "session:a", passkeys.REGISTER, START)
        second = passkeys.issue(db, "session:a", passkeys.REGISTER, START)
        passkeys.issue(db, "session:a", passkeys.STEP_UP, START)
        rows = list(db.scalars(select(OperatorChallenge)))
    assert first != second
    kept = {row.purpose: bytes(row.challenge) for row in rows}
    assert len(rows) == 2 and kept[passkeys.REGISTER] == second


def test_expired_challenges_of_anyone_are_pruned_by_the_next_one(sessions):
    with sessions() as db:
        passkeys.issue(db, "session:a", passkeys.REGISTER, START)
        passkeys.issue(db, "session:b", passkeys.STEP_UP, START + timedelta(minutes=1))
        later = START + passkeys.CHALLENGE_LIFE
        passkeys.issue(db, "session:c", passkeys.REGISTER, later)
        holders = {row.bound_to for row in db.scalars(select(OperatorChallenge))}
    assert holders == {"session:b", "session:c"}


def test_a_flood_of_sign_in_challenges_takes_nobody_elses_away(
    client, factors, clock, sessions
):
    """Security review of 16.11.2026: only the newest 1,000 sign-in challenges
    were kept, so anyone could push the operator's out while the device was
    being touched; the refusal then counted toward the operator's wait."""
    device = _with_passkey(client, factors, clock)
    passkey = device.get(_asked(client), ORIGIN)
    with sessions() as db:
        for number in range(1001):
            passkeys.issue(
                db, passkeys.for_attempt(f"flood{number}"), passkeys.SIGN_IN, clock.at
            )
    answer = _post(client, "/login", name="maxi", password=PASSWORD, passkey=passkey)
    assert answer.status_code == 303


def test_one_address_holds_only_so_many_live_sign_in_challenges(client, clock):
    """#827 F1: one client sent 3,000 posts and got 3,000 rows. Above the cap
    the address is refused with 429; its own attempt may still ask again."""
    client.get("/login")
    for _ in range(passkeys.SIGN_IN_PER_ADDRESS):
        client.cookies.delete("ops_attempt")
        assert _post(client, "/login/passkey/options").status_code == 200
    # The last attempt asks again: it replaces its own challenge.
    assert _post(client, "/login/passkey/options").status_code == 200
    client.cookies.delete("ops_attempt")
    refused = _post(client, "/login/passkey/options")
    assert refused.status_code == 429
    assert "ops_attempt" not in refused.cookies
    assert refused.headers["cache-control"] == "no-store"
    clock.at += passkeys.CHALLENGE_LIFE
    assert _post(client, "/login/passkey/options").status_code == 200


def test_the_cap_counts_one_address_and_sign_in_challenges_only(sessions):
    with sessions() as db:
        for number in range(passkeys.SIGN_IN_PER_ADDRESS):
            passkeys.issue(db, f"attempt:{number}", passkeys.SIGN_IN, START, "1.2.3.4")
        with pytest.raises(passkeys.TooMany):
            passkeys.issue(db, "attempt:more", passkeys.SIGN_IN, START, "1.2.3.4")
        passkeys.issue(db, "attempt:other", passkeys.SIGN_IN, START, "5.6.7.8")
        passkeys.issue(db, "session:a", passkeys.STEP_UP, START)
        rows = list(db.scalars(select(OperatorChallenge)))
    assert len(rows) == passkeys.SIGN_IN_PER_ADDRESS + 2
    assert "attempt:more" not in {row.bound_to for row in rows}


def test_parallel_asks_of_one_address_cannot_all_pass_the_cap(sessions):
    """#853: the advisory lock per address is what keeps the count honest. Without
    it every ask counts the rows before any other has committed and all pass."""
    asks = passkeys.SIGN_IN_PER_ADDRESS * 3
    barrier = threading.Barrier(asks)
    results: list[bool] = []

    def ask(number: int) -> None:
        with sessions() as db:
            barrier.wait()
            try:
                passkeys.issue(
                    db, f"attempt:p{number}", passkeys.SIGN_IN, START, "1.2.3.4"
                )
                results.append(True)
            except passkeys.TooMany:
                results.append(False)

    threads = [threading.Thread(target=ask, args=(n,)) for n in range(asks)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert len(results) == asks
    assert sum(results) == passkeys.SIGN_IN_PER_ADDRESS
    with sessions() as db:
        rows = db.scalars(
            select(OperatorChallenge).where(
                OperatorChallenge.client_address == "1.2.3.4"
            )
        ).all()
    assert len(rows) == passkeys.SIGN_IN_PER_ADDRESS


def test_the_challenge_address_migration_goes_down_and_up(operator_url):
    cfg = _config(operator_url)
    command.upgrade(cfg, "head")
    engine = create_engine(operator_url)

    def columns() -> set[str]:
        found = inspect(engine).get_columns("operator_challenges")
        return {col["name"] for col in found}

    try:
        assert "client_address" in columns()
        command.downgrade(cfg, "op003")
        assert "client_address" not in columns()
        command.upgrade(cfg, "head")
        assert "client_address" in columns()
    finally:
        engine.dispose()


def test_a_challenge_is_taken_once(sessions):
    with sessions() as db:
        issued = passkeys.issue(db, "session:a", passkeys.REGISTER, START)
        assert passkeys.take(db, "session:a", passkeys.REGISTER, START) == issued
        assert passkeys.take(db, "session:a", passkeys.REGISTER, START) is None


def test_a_challenge_is_not_taken_by_another_holder_or_purpose_or_late(sessions):
    with sessions() as db:
        issued = passkeys.issue(db, "session:a", passkeys.REGISTER, START)
        assert passkeys.take(db, "session:nobody", passkeys.REGISTER, START) is None
        assert passkeys.take(db, "session:a", passkeys.STEP_UP, START) is None
        # Asked for the wrong purpose, it is still there for the right one.
        again = START + passkeys.CHALLENGE_LIFE - timedelta(seconds=1)
        assert passkeys.take(db, "session:a", passkeys.REGISTER, again) == issued
        passkeys.issue(db, "session:a", passkeys.REGISTER, START)
        late = START + passkeys.CHALLENGE_LIFE
        assert passkeys.take(db, "session:a", passkeys.REGISTER, late) is None
        assert list(db.scalars(select(OperatorChallenge))) == []


# --- break glass and the migration ------------------------------------------------


def test_the_break_glass_reset_removes_the_passkeys(client, factors, clock, sessions):
    device = _with_passkey(client, factors, clock)
    with sessions() as db:
        renewed = access.reset_account(db, "maxi", "neue-lange-geheime-phrase")
    assert _rows(sessions) == []
    _refused(
        client, _passkey_sign_in(client, device, password="neue-lange-geheime-phrase")
    )
    code = _code(renewed.totp_secret, clock.at)
    assert (
        _sign_in(client, code, password="neue-lange-geheime-phrase").status_code == 303
    )


def test_the_passkey_migration_goes_down_and_up(operator_url):
    cfg = _config(operator_url)
    command.upgrade(cfg, "head")
    engine = create_engine(operator_url)
    try:
        with engine.begin() as conn:
            account = conn.execute(
                text(
                    "insert into operator_accounts (name, password_hash, totp_secret)"
                    " values ('maxi', 'x', null) returning id"
                )
            ).scalar_one()
            conn.execute(
                text(
                    "insert into operator_passkeys (account_id, credential_id,"
                    " public_key, sign_count, name, created_at)"
                    " values (:a, :c, :k, 0, 'Laptop', now())"
                ),
                {"a": account, "c": b"credential", "k": b"key"},
            )
        command.downgrade(cfg, "op002")
        names = set(inspect(engine).get_table_names())
        assert not {"operator_passkeys", "operator_challenges"} & names
        with engine.connect() as conn:
            secret = conn.execute(text("select totp_secret from operator_accounts"))
            assert re.fullmatch(r"[A-Z2-7]{32}", secret.scalar_one())
        columns = {
            col["name"] for col in inspect(engine).get_columns("operator_sessions")
        }
        assert not {"stepped_up_at", "pending_totp"} & columns
        command.upgrade(cfg, "head")
        names = set(inspect(engine).get_table_names())
        assert {"operator_passkeys", "operator_challenges"} <= names
    finally:
        engine.dispose()


# --- pages ------------------------------------------------------------------------


def test_the_sign_in_page_carries_the_passkey_button_and_script(client):
    page = client.get("/login").text
    assert '<input type="hidden" name="passkey">' in page
    assert re.search(r'<button[^>]*\bhidden\b[^>]*data-passkey="get"', page)
    assert "/static/passkey.js" in page


def test_the_factors_page_lists_passkeys_and_offers_changes_only_while_fresh(
    client, factors, clock
):
    _signed_in_with(client, factors, clock, 2)
    fresh = client.get("/factors").text
    assert "Key 0" in fresh and "Key 1" in fresh
    assert fresh.count('action="/factors/passkeys/remove"') == 2
    for action in ("/factors/passkeys", "/factors/totp/new", "/factors/totp/remove"):
        assert f'action="{action}"' in fresh
    clock.at += timedelta(minutes=5)
    stale = client.get("/factors").text
    assert "Key 0" in stale and "Key 1" in stale
    assert "/factors/passkeys/remove" not in stale and "/factors/totp/new" not in stale
    assert "Confirm it with a passkey" in stale


def test_the_factors_page_says_what_a_refusal_means_and_nothing_else(
    client, factors, clock
):
    _signed_in(client, factors, clock)
    assert "keeps TOTP" in client.get("/factors?note=factor-needed").text
    assert "<b>hi" not in client.get("/factors?note=<b>hi</b>").text


def test_the_start_page_asks_for_a_passkey_until_there_is_one(client, factors, clock):
    _signed_in(client, factors, clock)
    assert "No passkey yet" in client.get("/").text
    assert "passkey-added" in _register(client, Device()).headers["location"]
    assert "No passkey yet" not in client.get("/").text


# --- security review of 16.11.2026 --------------------------------------------------

# Deep enough for json.loads to give up with RecursionError, which is no
# ValueError; small enough for the proxy's body limit.
NESTED = "[" * 50_000


@pytest.mark.parametrize("password", [PASSWORD, "falsches-passwort"])
def test_a_passkey_field_the_parser_chokes_on_tells_nothing_about_the_password(
    client, factors, clock, sessions, password
):
    """The parse runs after the password check: a server error there answered
    500 for a right password and 401 for a wrong one, and skipped the audit."""
    _asked(client)
    answer = _post(client, "/login", name="maxi", password=password, passkey=NESTED)
    _refused(client, answer)
    reasons = [
        row.detail["reason"] for row in _audited(sessions, "operator.sign_in_failed")
    ]
    assert reasons == ["factor" if password == PASSWORD else "password"]


def test_a_passkey_field_the_parser_chokes_on_registers_and_steps_up_nothing(
    client, factors, clock, sessions
):
    _signed_in(client, factors, clock)
    _post(client, "/factors/passkeys/options")
    answer = _post(client, "/factors/passkeys", name="Laptop", passkey=NESTED)
    assert answer.headers["location"] == "/factors?note=passkey-refused"
    _post(client, "/step-up/passkey/options")
    answer = _post(client, "/step-up", next="/factors", passkey=NESTED)
    assert answer.status_code == 401 and web.FAILED in answer.text
    assert _rows(sessions) == []


def test_an_answer_longer_than_any_device_gives_is_not_parsed(
    client, factors, clock, monkeypatch
):
    device = _with_passkey(client, factors, clock)
    monkeypatch.setattr(passkeys, "ANSWER_MAX", 100)
    _refused(client, _passkey_sign_in(client, device))


@pytest.mark.parametrize(
    "broken", [httpx.InvalidURL("no scheme"), RuntimeError("anything")]
)
def test_a_push_that_breaks_in_any_way_does_not_cost_the_recovery_code_its_session(
    client, factors, sessions, monkeypatch, broken
):
    """`httpx.InvalidURL` (a mistyped OPERATOR_PUSH_URL) is no `HTTPError`: the
    code was spent and the answer was a 500 without the session cookie."""

    def queue(db: Any, text: str, at: Any) -> None:
        raise broken

    monkeypatch.setattr(push, "queue", queue)
    answer = _sign_in(client, factors.recovery_codes[0])
    assert answer.status_code == 303 and client.cookies.get("ops_session")
    # The recovery code's push and the new browser's.
    assert len(_audited(sessions, "operator.push_failed")) == 2


def test_a_copy_of_a_passkey_is_pushed(client, factors, clock, pushes):
    device = _with_passkey(client, factors, clock)
    pushes.clear()
    _refused(client, _passkey_sign_in(client, device, sign_count=1))
    assert pushes == ["maxi: a passkey answered with an old counter, a copy?"]
    # A refusal for any other reason is no copy; with the right password it is
    # the hourly alarm of a known password (#888 A2).
    clock.at += timedelta(seconds=1)
    _refused(client, _passkey_sign_in(client, device, verified=False))
    assert pushes[1:] == [KNOWN_PASSWORD]


def test_a_copy_of_a_passkey_at_the_step_up_is_pushed(client, factors, clock, pushes):
    _signed_in(client, factors, clock)
    device = Device()
    _register(client, device)
    pushes.clear()
    options = _post(client, "/step-up/passkey/options").text
    passkey = device.get(options, ORIGIN, sign_count=1)
    answer = _post(client, "/step-up", next="/factors", passkey=passkey)
    assert answer.status_code == 401
    assert pushes == ["maxi: a passkey answered with an old counter, a copy?"]


def test_the_break_glass_and_a_new_account_are_pushed(sessions, monkeypatch, pushes):
    monkeypatch.setattr(cli, "SessionLocal", sessions)
    monkeypatch.setattr(cli, "getpass", lambda _prompt: PASSWORD)
    assert cli.main(["add", "maxi"]) == 0
    assert cli.main(["reset", "Maxi"]) == 0
    assert pushes == [
        "maxi: account created on the operator host",
        "maxi: every factor reset on the operator host",
    ]


def test_the_break_glass_works_when_the_push_does_not(
    sessions, monkeypatch, capsys, factors
):
    def queue(db: Any, text: str, at: Any) -> None:
        raise httpx.ConnectError("down")

    monkeypatch.setattr(push, "queue", queue)
    monkeypatch.setattr(cli, "SessionLocal", sessions)
    monkeypatch.setattr(cli, "getpass", lambda _prompt: PASSWORD)
    assert cli.main(["reset", "maxi"]) == 0
    printed = capsys.readouterr()
    assert "otpauth://" in printed.out and "push" in printed.err
    # As the dashboard's `_alert`: a takeover push that was lost leaves a row.
    (row,) = _audited(sessions, "operator.push_failed")
    assert row.actor == "cli"
    assert row.detail == {"what": "every factor reset", "account": "maxi"}


def test_the_break_glass_still_shows_its_factors_when_the_alert_row_cannot_be_written(
    sessions, monkeypatch, capsys, factors
):
    def gone(*args: Any, **kwargs: Any) -> None:
        raise httpx.ConnectError("down")

    monkeypatch.setattr(push, "queue", gone)
    monkeypatch.setattr(cli, "audit", gone)
    monkeypatch.setattr(cli, "SessionLocal", sessions)
    monkeypatch.setattr(cli, "getpass", lambda _prompt: PASSWORD)
    assert cli.main(["reset", "maxi"]) == 0
    assert "otpauth://" in capsys.readouterr().out


def test_a_registration_overtaken_by_the_break_glass_adds_nothing(
    client, factors, clock, sessions, monkeypatch
):
    """Codex on #807: a registration that had passed the step-up check and then
    waited for the account's row while the reset ran added its passkey to the
    account the reset had just cleared."""
    _signed_in(client, factors, clock)
    options = _post(client, "/factors/passkeys/options").text
    answer = Device().create(options, ORIGIN)
    take = passkeys.take

    def reset_runs_first(db, bound_to, purpose, now, expected=None):
        challenge = take(db, bound_to, purpose, now, expected)
        with sessions() as host:
            access.reset_account(host, "maxi", PASSWORD)
        return challenge

    monkeypatch.setattr(passkeys, "take", reset_runs_first)
    refused = _post(client, "/factors/passkeys", name="Late", passkey=answer)
    assert refused.headers["location"] == "/factors?note=passkey-refused"
    assert _rows(sessions) == []
    assert "operator.passkey_added" not in _actions(sessions)


# --- the deep review of #807 (#816) ------------------------------------------------


def test_an_older_tabs_sign_in_leaves_the_newer_tabs_challenge(client, factors, clock):
    """Two tabs share the attempt cookie; the second one's options replace the
    first one's challenge. The first tab's answer must not spend it."""
    device = _with_passkey(client, factors, clock)
    older = device.get(_asked(client), ORIGIN)
    newer = device.get(_asked(client), ORIGIN)
    fields = {"name": "maxi", "password": PASSWORD}
    _refused(client, _post(client, "/login", **fields, passkey=older))
    assert _post(client, "/login", **fields, passkey=newer).status_code == 303


def test_an_older_tabs_step_up_leaves_the_newer_tabs_challenge(client, factors, clock):
    device = _signed_in_with(client, factors, clock, 1)[0]
    clock.at += timedelta(minutes=5)
    older = device.get(_post(client, "/step-up/passkey/options").text, ORIGIN)
    newer = device.get(_post(client, "/step-up/passkey/options").text, ORIGIN)
    assert _post(client, "/step-up", passkey=older).status_code == 401
    answer = _post(client, "/step-up", next="/factors", passkey=newer)
    assert answer.status_code == 303 and answer.headers["location"] == "/factors"


def test_an_older_tabs_registration_leaves_the_newer_tabs_challenge(
    client, factors, clock, sessions
):
    _signed_in(client, factors, clock)
    older = Device().create(_post(client, "/factors/passkeys/options").text, ORIGIN)
    newer = Device().create(_post(client, "/factors/passkeys/options").text, ORIGIN)
    refused = _post(client, "/factors/passkeys", name="Old", passkey=older)
    assert refused.headers["location"] == "/factors?note=passkey-refused"
    added = _post(client, "/factors/passkeys", name="New", passkey=newer)
    assert added.headers["location"] == "/factors?note=passkey-added"
    assert [row.name for row in _rows(sessions)] == ["New"]


@pytest.mark.parametrize("challenge", ["", "unreadable"])
def test_an_answer_without_a_readable_challenge_spends_nothing(
    sessions, clock, challenge
):
    with sessions() as db:
        passkeys.issue(db, "session:x", passkeys.STEP_UP, clock.at)
        assert passkeys.signed_challenge(challenge) == b""
        assert passkeys.take(db, "session:x", passkeys.STEP_UP, clock.at, b"") is None
        assert passkeys.take(db, "session:x", passkeys.STEP_UP, clock.at) is not None


@pytest.mark.parametrize("password", [PASSWORD, "not the password"])
def test_a_sign_in_without_any_factor_counts_and_audits_nothing(
    client, factors, clock, sessions, password
):
    """Enter after the password posts no code and no passkey: that says
    nothing about the password, so it is not checked, counted or audited."""
    for _ in range(3):
        _refused(client, _sign_in(client, " ", password=password))
    assert _audited(sessions, "operator.sign_in_failed") == []
    _signed_in(client, factors, clock)


def test_a_step_up_without_any_factor_counts_and_audits_nothing(
    client, factors, clock, sessions
):
    _signed_in(client, factors, clock)
    clock.at += timedelta(minutes=5)
    for _ in range(3):
        assert _post(client, "/step-up", code="").status_code == 401
    assert _audited(sessions, "operator.step_up_failed") == []
    assert _step_up_with_code(client, factors, clock).status_code == 303


def test_removing_totp_that_is_off_already_pushes_nothing(
    client, factors, clock, sessions, pushes
):
    _signed_in_with(client, factors, clock, 2, totp=False)
    again = _post(client, "/factors/totp/remove")
    assert again.headers["location"] == "/factors?note="
    assert pushes.count("maxi: TOTP removed") == 1
    assert len(_audited(sessions, "operator.totp_removed")) == 1


def test_an_answer_naming_another_user_is_refused(client, factors, clock):
    device = _with_passkey(client, factors, clock)
    device.user_handle = uuid.uuid4().bytes
    _refused(client, _passkey_sign_in(client, device))


# --- D39: a factor change ends the account's other sessions (#816) ------------------


def _remove_a_passkey(client, sessions, clock):
    return _post(client, "/factors/passkeys/remove", id=str(_rows(sessions)[0].id))


def _remove_totp(client, sessions, clock):
    return _post(client, "/factors/totp/remove")


def _new_totp_secret(client, sessions, clock):
    _post(client, "/factors/totp/new")
    return _post(
        client, "/factors/totp", code=_code(_pending(sessions) or "", clock.at)
    )


FACTOR_CHANGES = [
    pytest.param(
        _remove_a_passkey, "passkey-removed", "passkey 'Key 0' removed", id="passkey"
    ),
    pytest.param(_remove_totp, "totp-removed", "TOTP removed", id="totp"),
    pytest.param(_new_totp_secret, "totp-set", "new TOTP secret", id="new secret"),
]


def _other_browser(factors, clock) -> TestClient:
    """A second session of the same account, signed in half a minute before
    the one that makes the change (a TOTP code opens one sign-in only)."""
    browser = TestClient(web.app)
    assert _sign_in(browser, _code(factors.totp_secret, clock.at)).status_code == 303
    clock.at += timedelta(seconds=30)
    return browser


def _still_signed_in(browser) -> bool:
    return browser.get("/", follow_redirects=False).status_code == 200


@pytest.mark.parametrize(("change", "note", "what"), FACTOR_CHANGES)
def test_a_factor_change_ends_the_other_sessions_of_the_account(
    client, factors, clock, sessions, pushes, change, note, what
):
    other = _other_browser(factors, clock)
    anna = _anna(sessions, clock)
    _signed_in_with(client, factors, clock, 3)
    assert _still_signed_in(other)
    answer = change(client, sessions, clock)
    assert answer.headers["location"] == f"/factors?note={note}"
    assert not _still_signed_in(other)
    assert _still_signed_in(client) and _still_signed_in(anna)
    assert pushes[-1] == f"maxi: {what}, other sessions ended: 1"
    changed = _audited(sessions, "operator.other_sessions_ended")
    assert [row.detail["ended"] for row in changed] == [1]


def test_adding_a_passkey_ends_no_other_session(client, factors, clock, pushes):
    other = _other_browser(factors, clock)
    _signed_in(client, factors, clock)
    assert _register(client, Device()).headers["location"].endswith("passkey-added")
    assert _still_signed_in(other)
    assert pushes == ["maxi: new passkey 'Laptop'"]


def test_a_failed_push_does_not_bring_the_ended_sessions_back(
    client, factors, clock, sessions, monkeypatch
):
    other = _other_browser(factors, clock)
    _signed_in_with(client, factors, clock, 3)

    def queue(db: Any, text: str, at: Any) -> None:
        raise httpx.ConnectError("down")

    monkeypatch.setattr(push, "queue", queue)
    answer = _remove_a_passkey(client, sessions, clock)
    assert answer.headers["location"] == "/factors?note=passkey-removed"
    assert not _still_signed_in(other) and len(_rows(sessions)) == 2
    failed = _audited(sessions, "operator.push_failed")
    assert failed[-1].detail["what"] == (
        "passkey 'Key 0' removed, other sessions ended: 1"
    )


# --- Codex on #818: a session ended while its change waited for the account's row ---


def _factors_now(sessions) -> tuple[list[str], str | None]:
    with sessions() as db:
        secret = db.scalars(
            select(OperatorAccount.totp_secret).where(OperatorAccount.name == "maxi")
        ).one()
    return [row.name for row in _rows(sessions)], secret


@pytest.mark.parametrize(
    ("late", "owners"),
    [
        pytest.param(_remove_a_passkey, _remove_totp, id="passkey"),
        pytest.param(_remove_totp, _remove_a_passkey, id="totp"),
        pytest.param(_new_totp_secret, _remove_a_passkey, id="new secret"),
    ],
)
def test_a_change_of_a_session_ended_while_it_waited_changes_nothing(
    client, factors, clock, sessions, pushes, monkeypatch, late, owners
):
    """A stolen session's change that had passed the step-up check and then
    waited for the account's row, while the owner's change ended that session,
    went on to change a factor and to end the owner's session."""
    thief = _other_browser(factors, clock)
    devices = _signed_in_with(client, factors, clock, 3)
    _passkey_step_up(thief, devices[1])  # a synced passkey, or the change waits
    locked = access._locked
    after: list[Any] = []

    def the_owner_commits_first(db, operator, now=None):
        if not after:
            after.append(None)  # the owner's own request passes straight through
            assert owners(client, sessions, clock).status_code == 303
            after[0] = (_factors_now(sessions), list(pushes))
        return locked(db, operator, now)

    monkeypatch.setattr(access, "_locked", the_owner_commits_first)
    assert late(thief, sessions, clock).status_code == 401
    assert (_factors_now(sessions), pushes) == after[0]
    assert _still_signed_in(client) and not _still_signed_in(thief)
    assert len(_audited(sessions, "operator.other_sessions_ended")) == 1


# --- D37: a factor change needs a passkey step-up once there is a passkey (#884) ---

FACTOR_WRITES = [
    "/factors/passkeys/options",
    "/factors/passkeys",
    "/factors/passkeys/remove",
    "/factors/totp/new",
    "/factors/totp",
    "/factors/totp/remove",
]
TO_PASSKEY_STEP_UP = "/step-up?next=%2Ffactors&for_factors=1"


@pytest.mark.parametrize("path", FACTOR_WRITES)
def test_a_totp_session_is_refused_on_each_factor_change(
    client, factors, clock, sessions, pushes, path
):
    _with_passkey(client, factors, clock)
    clock.at += timedelta(seconds=30)
    _signed_in(client, factors, clock)
    before = (_factors_now(sessions), _actions(sessions))
    answer = _post(client, path, back="/factors", id=str(_rows(sessions)[0].id))
    assert answer.headers["location"] == TO_PASSKEY_STEP_UP
    # A TOTP step-up on top changes nothing.
    clock.at += timedelta(seconds=30)
    assert _step_up_with_code(client, factors, clock).status_code == 303
    again = _post(client, path, back="/factors", id=str(_rows(sessions)[0].id))
    assert again.headers["location"] == TO_PASSKEY_STEP_UP
    assert _factors_now(sessions) == before[0] and _pending(sessions) is None
    assert _actions(sessions) == [*before[1], "operator.stepped_up"]
    assert pushes == ["maxi: new passkey 'Laptop'"]


@pytest.mark.parametrize("how", ["passkey", "recovery"])
def test_no_sign_in_counts_for_a_factor_change_once_there_is_a_passkey(
    client, factors, clock, guarded, how
):
    device = _with_passkey(client, factors, clock)
    if how == "passkey":
        assert _passkey_sign_in(client, device).status_code == 303
    else:
        assert _sign_in(client, factors.recovery_codes[0]).status_code == 303
    answer = _post(client, "/factors/totp/new", back="/factors")
    assert answer.headers["location"] == TO_PASSKEY_STEP_UP
    # Every other dangerous action still takes the sign-in's factor.
    assert _call(client, "POST", "/guarded").text == "done"


def test_a_passkey_step_up_opens_the_factor_changes_for_five_minutes(
    client, factors, clock, sessions
):
    device = _with_passkey(client, factors, clock)
    clock.at += timedelta(seconds=30)
    _signed_in(client, factors, clock)
    _passkey_step_up(client, device)
    assert _post(client, "/factors/totp/new").headers["location"] == "/factors?note="
    assert _pending(sessions) in client.get("/factors").text
    clock.at += timedelta(minutes=4, seconds=59)
    assert _post(client, "/factors/passkeys/options").status_code == 200
    clock.at += timedelta(seconds=1)
    stale = _post(client, "/factors/passkeys/options", back="/factors")
    assert stale.headers["location"] == TO_PASSKEY_STEP_UP
    assert _pending(sessions) not in client.get("/factors").text


@pytest.mark.parametrize("how", ["totp", "recovery"])
def test_without_a_passkey_any_fresh_factor_changes_the_factors(
    client, factors, clock, sessions, how
):
    if how == "recovery":
        assert _sign_in(client, factors.recovery_codes[0]).status_code == 303
    else:
        _signed_in(client, factors, clock)
    assert _post(client, "/factors/totp/new").headers["location"] == "/factors?note="
    assert _pending(sessions) in client.get("/factors").text
    clock.at += timedelta(minutes=5)
    stale = _post(client, "/factors/totp/new", back="/factors")
    assert stale.headers["location"] == "/step-up?next=%2Ffactors"
    assert 'href="/step-up?next=/factors"' in client.get("/factors").text


def test_removing_the_last_passkey_needs_that_passkey(client, factors, clock, sessions):
    device = _with_passkey(client, factors, clock)
    clock.at += timedelta(seconds=30)
    _signed_in(client, factors, clock)
    row = str(_rows(sessions)[0].id)
    refused = _post(client, "/factors/passkeys/remove", id=row, back="/factors")
    assert refused.headers["location"] == TO_PASSKEY_STEP_UP
    _passkey_step_up(client, device)
    answer = _post(client, "/factors/passkeys/remove", id=row)
    assert answer.headers["location"] == "/factors?note=passkey-removed"
    # Without a passkey the TOTP code counts again.
    assert _post(client, "/factors/totp/new").headers["location"] == "/factors?note="


@pytest.mark.parametrize(
    ("passkey_count", "for_factors", "code_field"),
    [(1, True, False), (1, False, True), (0, True, True)],
    ids=["factor change", "other action", "no passkey"],
)
def test_the_step_up_page_for_a_factor_change_offers_the_passkey_only(
    client, factors, clock, passkey_count, for_factors, code_field
):
    _signed_in_with(client, factors, clock, passkey_count)
    query = "next=/factors" + ("&for_factors=1" if for_factors else "")
    page = client.get(f"/step-up?{query}").text
    assert ('name="code"' in page) is code_field
    assert ('data-passkey="get"' in page) is (passkey_count > 0)
    assert ('name="for_factors" value="1"' in page) is not code_field
    refused = _post(
        client,
        "/step-up",
        next="/factors",
        code=_wrong(factors.totp_secret, clock.at),
        **({"for_factors": "1"} if for_factors else {}),
    )
    assert refused.status_code == 401
    assert ('name="code"' in refused.text) is code_field


def test_the_factors_page_sends_a_totp_session_to_the_passkey_step_up(
    client, factors, clock
):
    _with_passkey(client, factors, clock)
    clock.at += timedelta(seconds=30)
    _signed_in(client, factors, clock)
    page = client.get("/factors").text
    assert "/factors/totp/new" not in page
    assert 'href="/step-up?next=/factors&amp;for_factors=1"' in page


def test_the_passkey_step_up_migration_goes_down_and_up(operator_url):
    cfg = _config(operator_url)
    command.upgrade(cfg, "head")
    engine = create_engine(operator_url)

    def columns() -> set[str]:
        found = inspect(engine).get_columns("operator_sessions")
        return {col["name"] for col in found}

    try:
        assert "passkey_stepped_up_at" in columns()
        command.downgrade(cfg, "op005")
        assert "passkey_stepped_up_at" not in columns()
        assert "stepped_up_at" in columns()
        command.upgrade(cfg, "head")
        assert "passkey_stepped_up_at" in columns()
    finally:
        engine.dispose()


def test_a_passkey_step_up_freshens_only_its_own_session(
    client, factors, clock, sessions
):
    other = _other_browser(factors, clock)
    _signed_in_with(client, factors, clock, 1)
    answer = _post(other, "/factors/totp/new", back="/factors")
    assert answer.headers["location"] == TO_PASSKEY_STEP_UP
    assert _pending(sessions) is None


# --- Review of #884: the first passkey of another session, while a change waits ---


def test_a_first_passkey_registered_while_a_totp_change_waited_stops_it(
    client, factors, clock, sessions, pushes, monkeypatch
):
    """The TOTP session passed the route's check while the account had no
    passkey; the owner's first passkey committed while its change waited for
    the account's row. Under the row D37 holds again: nothing changes."""
    owner = _other_browser(factors, clock)
    _signed_in(client, factors, clock)
    assert _post(client, "/factors/totp/new").headers["location"] == "/factors?note="
    secret = _pending(sessions) or ""
    locked = access._locked
    fired: list[None] = []

    def the_owner_registers_first(db, operator, now=None):
        if not fired:
            fired.append(None)
            answer = _register(owner, Device(), "Owner key")
            assert answer.headers["location"] == "/factors?note=passkey-added"
        return locked(db, operator, now)

    monkeypatch.setattr(access, "_locked", the_owner_registers_first)
    answer = _post(client, "/factors/totp", code=_code(secret, clock.at))
    assert answer.headers["location"] == TO_PASSKEY_STEP_UP
    assert _factors_now(sessions) == (["Owner key"], factors.totp_secret)
    assert _still_signed_in(owner)
    assert pushes == ["maxi: new passkey 'Owner key'"]


def test_a_first_passkey_registered_after_the_check_stops_a_second_registration(
    client, factors, clock, sessions, pushes, monkeypatch
):
    owner = _other_browser(factors, clock)
    _signed_in(client, factors, clock)
    options = _post(client, "/factors/passkeys/options")
    answer = Device().create(options.text, ORIGIN)
    count = passkeys.count
    fired: list[None] = []

    def the_owner_registers_after_the_check(db, account_id):
        seen = count(db, account_id)
        if not fired:
            fired.append(None)
            done = _register(owner, Device(), "Owner key")
            assert done.headers["location"] == "/factors?note=passkey-added"
        return seen

    monkeypatch.setattr(passkeys, "count", the_owner_registers_after_the_check)
    refused = _post(client, "/factors/passkeys", name="Thief", passkey=answer)
    assert refused.headers["location"] == "/factors?note=passkey-refused"
    assert [row.name for row in _rows(sessions)] == ["Owner key"]
    assert pushes == ["maxi: new passkey 'Owner key'"]


@pytest.mark.parametrize("path", ["/factors/passkeys/remove", "/factors/totp/remove"])
def test_a_first_passkey_registered_while_a_removal_waited_stops_it(
    client, factors, clock, sessions, pushes, monkeypatch, path
):
    owner = _other_browser(factors, clock)
    _signed_in(client, factors, clock)
    locked = access._locked
    fired: list[None] = []

    def the_owner_registers_first(db, operator, now=None):
        if not fired:
            fired.append(None)
            answer = _register(owner, Device(), "Owner key")
            assert answer.headers["location"] == "/factors?note=passkey-added"
        return locked(db, operator, now)

    monkeypatch.setattr(access, "_locked", the_owner_registers_first)
    answer = _post(client, path, id=str(uuid.uuid4()))
    assert answer.headers["location"] == TO_PASSKEY_STEP_UP and fired
    assert _factors_now(sessions) == (["Owner key"], factors.totp_secret)
    assert _still_signed_in(owner)


def test_a_pending_secret_is_hidden_once_only_a_totp_step_up_is_fresh(
    client, factors, clock, sessions
):
    device = _with_passkey(client, factors, clock)
    clock.at += timedelta(seconds=30)
    _signed_in(client, factors, clock)
    _passkey_step_up(client, device)
    assert _post(client, "/factors/totp/new").headers["location"] == "/factors?note="
    pending = _pending(sessions)
    assert pending and pending in client.get("/factors").text
    clock.at += timedelta(minutes=4)
    assert _step_up_with_code(client, factors, clock).status_code == 303
    clock.at += timedelta(minutes=1)
    # The passkey step-up is five minutes old, the TOTP one one minute.
    assert pending not in client.get("/factors").text


def test_a_passkey_step_up_opens_the_other_dangerous_actions_too(
    client, factors, clock, guarded
):
    device = _with_passkey(client, factors, clock)
    assert _passkey_sign_in(client, device).status_code == 303
    clock.at += timedelta(minutes=5)
    assert _call(client, "POST", "/guarded").status_code == 303
    _passkey_step_up(client, device)
    assert _call(client, "POST", "/guarded").text == "done"


# --- #888 A2, A3, A4, B1 ---------------------------------------------------------

KNOWN_PASSWORD = f"maxi: {web.KNOWN_PASSWORD}"
STEP_UP_GUESSES = f"maxi: {web.STEP_UP_GUESSES}"


def _wrong_step_up(client, factors, clock) -> None:
    wrong = _wrong(factors.totp_secret, clock.at)
    assert _post(client, "/step-up", code=wrong).status_code == 401


def test_a_right_password_with_a_wrong_code_is_pushed_once_an_hour(
    client, factors, clock, pushes
):
    """#888 A2: it was audited and never pushed, so a thief who had the
    password guessed codes unseen."""
    for _ in range(2):
        _refused(client, _sign_in(client, _wrong(factors.totp_secret, clock.at)))
    assert pushes == [KNOWN_PASSWORD]
    clock.at += timedelta(hours=1, seconds=1)
    _refused(client, _sign_in(client, _wrong(factors.totp_secret, clock.at)))
    assert pushes == [KNOWN_PASSWORD, KNOWN_PASSWORD]


def test_a_guess_every_ten_minutes_is_pushed_again_every_hour(
    client, factors, clock, pushes
):
    """Codex on #933: the hour was measured from the last failure, so a guess
    every ten minutes kept every alarm after the first quiet."""
    for _ in range(13):  # minute 0 to 120
        _refused(client, _sign_in(client, _wrong(factors.totp_secret, clock.at)))
        clock.at += timedelta(minutes=10)
    assert pushes == [KNOWN_PASSWORD] * 3


def test_three_wrong_step_ups_at_once_still_push_the_third(
    sessions, factors, clock, monkeypatch
):
    """Codex on #933: the rollback of the factor check let go of the account
    before the count, so three failures at once each counted none before them
    and none pushed. They wait for each other on the account's row now."""
    with sessions() as db:
        signed = access.sign_in(
            db,
            "maxi",
            PASSWORD,
            _code(factors.totp_secret, clock.at),
            "1.2.3.4",
            clock.at,
        )
        operator = access.resolve(db, signed.token, clock.at)
    assert operator is not None
    barrier = threading.Barrier(3)
    hold = access._hold

    def met(db, account_id):
        barrier.wait(timeout=30)
        hold(db, account_id)

    monkeypatch.setattr(access, "_hold", met)
    later = clock.at + timedelta(minutes=5)
    alarms: list[bool] = []

    def guess() -> None:
        with sessions() as db:
            try:
                access.step_up(
                    db,
                    operator,
                    _wrong(factors.totp_secret, later),
                    "1.2.3.4",
                    later,
                    device_token=signed.device_token,
                )
            except access.SignInRefused as refused:
                alarms.append(refused.alarm)

    threads = [threading.Thread(target=guess) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert sorted(alarms) == [False, False, True]


def test_a_wrong_password_an_unknown_name_or_a_double_tap_is_not_pushed(
    client, factors, clock, pushes
):
    _refused(client, _sign_in(client, "123456", password="falsch-falsch-falsch"))
    _refused(client, _sign_in(client, "123456", name="nobody"))
    code = _code(factors.totp_secret, clock.at)
    assert _sign_in(client, code).status_code == 303
    client.cookies.clear()
    # The same form sent twice: a `repeat` (#688, #914), no known password.
    _refused(client, _sign_in(client, code))
    assert pushes == []


def test_each_account_gets_its_own_known_password_push(
    client, sessions, factors, clock, pushes
):
    """#931: the hour is counted per account, so a push for one account does
    not hide a known password of another."""
    with sessions() as db:
        anna = access.create_account(db, "anna", PASSWORD)
    _refused(client, _sign_in(client, _wrong(factors.totp_secret, clock.at)))
    _refused(client, _sign_in(client, _wrong(anna.totp_secret, clock.at), name="anna"))
    assert pushes == [KNOWN_PASSWORD, f"anna: {web.KNOWN_PASSWORD}"]


def test_a_right_code_after_wrong_ones_adds_no_alarm_and_no_push_names_a_secret(
    client, factors, clock, monkeypatch
):
    """#931: the right code after the guesses sends only the sign-in's own
    push (#888 A1), and no push carries the password or a code."""
    sent: list[str] = []
    monkeypatch.setattr(push, "queue", lambda db, text, at: sent.append(text))
    wrong = _wrong(factors.totp_secret, clock.at)
    _refused(client, _sign_in(client, wrong))
    clock.at += timedelta(seconds=1)
    right = _code(factors.totp_secret, clock.at)
    assert _sign_in(client, right).status_code == 303
    assert sent == [KNOWN_PASSWORD, f"maxi: {web.NEW_BROWSER} (totp)"]
    for secret in (PASSWORD, wrong, right, factors.totp_secret):
        assert not any(secret in text for text in sent)


def test_every_third_wrong_step_up_in_a_row_is_pushed(client, factors, clock, pushes):
    """#888 A2: wrong step-up codes were audited only, so a stolen session
    guessed them unseen. A step-up that holds starts the count again."""
    _signed_in(client, factors, clock)
    clock.at += timedelta(minutes=5)
    for _ in range(2):
        _wrong_step_up(client, factors, clock)
    assert _step_up_with_code(client, factors, clock).status_code == 303
    _wrong_step_up(client, factors, clock)
    assert pushes == []
    for _ in range(2):
        _wrong_step_up(client, factors, clock)
    assert pushes == [STEP_UP_GUESSES]


def test_the_device_cookie_gives_the_step_up_no_second_wait(client, factors, clock):
    """#888 A3: with the device cookie the step-up counted the device alone,
    without it the account and the address, so whoever held the cookie had two
    wait ladders. Now the account counts either way."""
    _signed_in(client, factors, clock)
    clock.at += timedelta(minutes=5)
    for _ in range(3):
        _wrong_step_up(client, factors, clock)
    client.cookies.delete("ops_device")
    answer = _step_up_with_code(client, factors, clock)
    assert answer.status_code == 401 and web.WAIT.format(minutes=1) in answer.text
    clock.at += timedelta(minutes=1, seconds=1)
    assert _step_up_with_code(client, factors, clock).status_code == 303


@pytest.mark.parametrize(
    ("site", "alive"),
    [
        ("same-origin", True),
        ("none", True),
        ("same-site", False),
        ("cross-site", False),
    ],
)
def test_only_the_operators_own_requests_keep_the_session_alive(
    client, factors, clock, site, alive
):
    """#888 A4: a taken-over customer stack on the same site could load
    `ops.` pages as images every few minutes, and each GET moved the idle
    clock."""
    _signed_in(client, factors, clock)
    for _ in range(2):
        clock.at += timedelta(minutes=20)
        client.get("/", headers={"Sec-Fetch-Site": site}, follow_redirects=False)
    answer = client.get("/", follow_redirects=False)
    assert (answer.status_code == 200) is alive
    if not alive:
        assert answer.headers["location"] == "/login"


def test_a_factor_change_forgets_every_other_browsers_device_cookie(
    client, factors, clock, sessions
):
    """#888 B1, S12: D39 ended the thief's session but kept the device row, so
    the thief went on guessing codes for the new secret on a wait ladder of its
    own. The browser making the change keeps its cookie."""
    _signed_in(client, factors, clock)
    clock.at += timedelta(seconds=30)
    thief = TestClient(web.app)
    assert _sign_in(thief, _code(factors.totp_secret, clock.at)).status_code == 303
    _post(client, "/factors/totp/new")
    pending = _pending(sessions)
    assert pending
    answer = _post(client, "/factors/totp", code=_code(pending, clock.at))
    assert answer.headers["location"] == "/factors?note=totp-set"
    with sessions() as db:
        kept = list(db.scalars(select(OperatorDevice.token_hash)))
    assert kept == [token_hash(client.cookies.get("ops_device", ""))]


def test_a_passkey_sign_in_from_a_known_browser_is_not_pushed(
    client, factors, clock, monkeypatch
):
    device = _with_passkey(client, factors, clock)
    assert _passkey_sign_in(client, device).status_code == 303
    sent: list[str] = []
    monkeypatch.setattr(push, "queue", lambda db, text, at: sent.append(text))
    clock.at += timedelta(seconds=30)
    assert _passkey_sign_in(client, device).status_code == 303
    assert sent == []


def test_a_step_up_with_a_code_is_pushed_and_one_with_a_passkey_is_not(
    client, factors, clock, monkeypatch
):
    device = _with_passkey(client, factors, clock)
    assert _passkey_sign_in(client, device).status_code == 303
    sent: list[str] = []
    monkeypatch.setattr(push, "queue", lambda db, text, at: sent.append(text))
    clock.at += timedelta(seconds=30)
    assert _step_up_with_code(client, factors, clock).status_code == 303
    assert sent == [f"maxi: stepped up {web.BY_CODE}"]
    _passkey_step_up(client, device)
    assert len(sent) == 1


def test_a_refused_step_up_with_a_code_is_not_pushed_as_one(
    client, factors, clock, pushes
):
    _signed_in(client, factors, clock)
    assert _post(client, "/step-up", code="000000").status_code == 401
    assert pushes == []
