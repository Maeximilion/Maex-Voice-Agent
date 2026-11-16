"""The customer list of the operator dashboard (#571, docs/23 §8): the registry
and its command line, the answers of the stacks read as untrusted input, the
list page and its refresh, against a scratch operator database and a mock
transport instead of a network."""

import copy
import gzip
import io
import re
import sys
import threading
import time
import tracemalloc
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import sessionmaker

from api.dashboard import access, cli, customers, totp
from api.dashboard import app as web
from api.dashboard.customers import BadCustomer, add_customer, remove_customer
from api.dashboard.db import get_db
from api.dashboard.models import Customer, OperatorAudit
from evals.scratch_db import create_scratch_db, drop_scratch_db

ROOT = Path(__file__).resolve().parents[2]
INI = ROOT / "db" / "dashboard" / "alembic.ini"
TEMPLATES = ROOT / "api" / "dashboard" / "templates"
PASSWORD = "lange-geheime-phrase"
START = datetime(2026, 11, 10, 9, 0, tzinfo=UTC)
TOKEN_A = "a1" * 20
TOKEN_B = "b2" * 20
TOKEN_C = "c3" * 20
TOKEN_D = "d4" * 20


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


def _signed_in(client, factors, clock) -> None:
    answer = _sign_in(client, _code(factors.totp_secret, clock.at))
    assert answer.status_code == 303


# --- the stacks' side ---------------------------------------------------------


def _stamp(at: datetime) -> str:
    return at.isoformat()


def _status(key: str) -> dict:
    """What a healthy stack answers (docs/04 "Ops API")."""
    return {
        "tenant": {"key": key, "name": f"Restaurant {key}"},
        "call_mode": "ai_first",
        "last_call_at": _stamp(START - timedelta(minutes=5)),
        "business_day": "2026-11-10",
        "today": {"calls": 12, "orders": 7},
        "print_bridge": {
            "configured": True,
            "last_poll_at": _stamp(START - timedelta(seconds=20)),
            "silent": False,
        },
        "backup_at": _stamp(START - timedelta(hours=3)),
        "alarms": [],
        "menu": {
            "items": 80,
            "price_differences": 0,
            "without_allergen_information": 0,
        },
    }


def _hull(data: dict) -> dict:
    return {"ok": True, "data": data, "say": None}


def _serving(
    key: str, mutate: Callable[[dict], object] | None = None, code: int = 200
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        data = copy.deepcopy(_status(key))
        if mutate:
            mutate(data)
        return httpx.Response(code, json=_hull(data))

    return handler


def _raising(error: Exception) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        raise error

    return handler


def _factory(handler: Callable[[httpx.Request], httpx.Response]):
    def make() -> httpx.Client:
        return httpx.Client(
            transport=httpx.MockTransport(handler),
            follow_redirects=False,
            trust_env=False,
        )

    return make


def _by_host(handlers: dict[str, Callable[[httpx.Request], httpx.Response]]):
    def handler(request: httpx.Request) -> httpx.Response:
        return handlers[request.url.host](request)

    return handler


def _url(key: str) -> str:
    return f"http://{key}-api:8000"


def _customer(key: str, token: str = TOKEN_A) -> customers.Stack:
    return customers.Stack(key, _url(key), token)


def _one(handler, key: str = "alpha") -> customers.Row:
    rows = customers.overview([_customer(key)], client=_factory(handler))
    assert len(rows) == 1
    return rows[0]


# --- the registry -------------------------------------------------------------


def test_add_customer_stores_and_a_second_run_replaces_address_and_token(sessions):
    with sessions() as db:
        add_customer(db, "alpha", "http://alpha-api:8000", TOKEN_A)
        row = db.get(Customer, "alpha")
        assert (row.api_url, row.ops_token) == ("http://alpha-api:8000", TOKEN_A)
        add_customer(db, "alpha", "http://alpha-two:8001/", TOKEN_B)
        db.expire_all()
        rows = customers.list_customers(db)
        assert [(r.key, r.api_url, r.ops_token) for r in rows] == [
            ("alpha", "http://alpha-two:8001", TOKEN_B)
        ]


@pytest.mark.parametrize("key", ["Alpha", "1alpha", "al--pha", "alpha-", "al pha", ""])
def test_a_bad_key_is_refused(sessions, key):
    with sessions() as db, pytest.raises(BadCustomer):
        add_customer(db, key, "http://alpha-api:8000", TOKEN_A)


@pytest.mark.parametrize("token", ["a" * 31, "a" * 20 + " " + "b" * 20, ""])
def test_a_short_token_or_one_with_whitespace_is_refused(sessions, token):
    with sessions() as db, pytest.raises(BadCustomer):
        add_customer(db, "alpha", "http://alpha-api:8000", token)


@pytest.mark.parametrize(
    "url",
    [
        "ftp://x:1",
        "http://host",
        "http://host:8000/path",
        "http://user:pw@host:8000",
        "http://host:8000?x=1",
        "http://ho st:8000",
    ],
)
def test_an_address_that_is_not_scheme_host_port_is_refused(sessions, url):
    with sessions() as db, pytest.raises(BadCustomer):
        add_customer(db, "alpha", url, TOKEN_A)
    with sessions() as db:
        assert customers.list_customers(db) == []


def test_two_customers_cannot_share_an_address_or_a_token(sessions):
    with sessions() as db:
        add_customer(db, "alpha", "http://alpha-api:8000", TOKEN_A)
        with pytest.raises(BadCustomer):
            add_customer(db, "beta", "http://alpha-api:8000", TOKEN_B)
        with pytest.raises(BadCustomer):
            add_customer(db, "beta", "http://beta-api:8000", TOKEN_A)
        # The refused attempts left the session usable and nothing behind.
        add_customer(db, "beta", "http://beta-api:8000", TOKEN_B)
        assert [c.key for c in customers.list_customers(db)] == ["alpha", "beta"]


def test_remove_customer_says_true_once_then_false(sessions):
    with sessions() as db:
        add_customer(db, "alpha", "http://alpha-api:8000", TOKEN_A)
        assert remove_customer(db, "alpha") is True
        assert remove_customer(db, "alpha") is False
        assert customers.list_customers(db) == []


# --- the command line ---------------------------------------------------------


def _audit(sessions) -> list[OperatorAudit]:
    with sessions() as db:
        return list(db.scalars(select(OperatorAudit).order_by(OperatorAudit.id)))


def test_cli_adds_lists_and_removes_without_ever_printing_the_token(
    sessions, monkeypatch, capsys
):
    monkeypatch.setattr(cli, "SessionLocal", sessions)
    monkeypatch.setattr(sys, "stdin", io.StringIO(TOKEN_A + "\n"))
    assert cli.main(["customer-add", "alpha", "http://maex-alpha-api-1:8000"]) == 0
    added = capsys.readouterr()
    assert "alpha" in added.out and "http://maex-alpha-api-1:8000" in added.out
    assert TOKEN_A not in added.out + added.err

    assert cli.main(["customers"]) == 0
    listed = capsys.readouterr()
    assert "alpha" in listed.out and "http://maex-alpha-api-1:8000" in listed.out
    assert TOKEN_A not in listed.out + listed.err

    assert cli.main(["customer-remove", "alpha"]) == 0
    assert cli.main(["customer-remove", "alpha"]) == 1
    capsys.readouterr()

    rows = [r for r in _audit(sessions) if r.action.startswith("customer.")]
    assert [(r.action, r.target, r.actor) for r in rows] == [
        ("customer.added", "alpha", "cli"),
        ("customer.removed", "alpha", "cli"),
    ]
    assert all(TOKEN_A not in str(r.detail) for r in _audit(sessions))


def test_cli_refuses_a_bad_key_with_exit_code_one(sessions, monkeypatch, capsys):
    monkeypatch.setattr(cli, "SessionLocal", sessions)
    monkeypatch.setattr(sys, "stdin", io.StringIO(TOKEN_A + "\n"))
    assert cli.main(["customer-add", "Alpha", "http://maex-alpha-api-1:8000"]) == 1
    assert TOKEN_A not in capsys.readouterr().err
    with sessions() as db:
        assert customers.list_customers(db) == []


# --- the migration ------------------------------------------------------------


def test_op002_creates_the_table_and_its_downgrade_keeps_the_accounts(operator_url):
    cfg = _config(operator_url)
    engine = create_engine(operator_url)
    try:
        command.upgrade(cfg, "head")
        assert "operator_customers" in inspect(engine).get_table_names()
        command.downgrade(cfg, "op001")
        names = inspect(engine).get_table_names()
        assert "operator_customers" not in names
        assert "operator_accounts" in names
        command.upgrade(cfg, "head")
        assert "operator_customers" in inspect(engine).get_table_names()
    finally:
        engine.dispose()


# --- overview: a good answer --------------------------------------------------


def test_a_good_answer_is_an_ok_row_and_the_request_carries_the_token():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _serving("alpha")(request)

    row = _one(handler)
    assert row.tone == "ok" and row.notes == [] and row.problem == ""
    assert row.status is not None
    assert row.status.tenant.name == "Restaurant alpha"
    assert len(seen) == 1
    assert str(seen[0].url) == "http://alpha-api:8000/v1/ops/status"
    assert seen[0].headers["authorization"] == f"Bearer {TOKEN_A}"


def test_each_customer_gets_only_its_own_token():
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        seen[host] = request.headers["authorization"]
        return _serving(host.removesuffix("-api"))(request)

    rows = customers.overview(
        [_customer("alpha", TOKEN_A), _customer("beta", TOKEN_B)],
        client=_factory(handler),
    )
    assert [r.tone for r in rows] == ["ok", "ok"]
    assert seen == {
        "alpha-api": f"Bearer {TOKEN_A}",
        "beta-api": f"Bearer {TOKEN_B}",
    }


@pytest.mark.parametrize(
    ("mutate", "note"),
    [
        (
            lambda d: d.update(
                alarms=[{"kind": "print_failed", "last_at": _stamp(START), "count": 3}]
            ),
            "print_failed (3)",
        ),
        (
            lambda d: d["print_bridge"].update(configured=True, silent=True),
            "print bridge silent",
        ),
        (lambda d: d.update(backup_at=None), "no backup yet"),
    ],
    ids=["alarm", "silent-bridge", "no-backup"],
)
def test_what_to_look_at_makes_a_warn_row_with_a_note(mutate, note):
    row = _one(_serving("alpha", mutate))
    assert row.tone == "warn"
    assert row.notes == [note]
    assert row.status is not None


def test_a_bridge_that_is_not_configured_is_not_a_warning():
    row = _one(
        _serving(
            "alpha",
            lambda d: d["print_bridge"].update(configured=False, silent=True),
        )
    )
    assert row.tone == "ok"


# --- overview: problems -------------------------------------------------------


def _naive(d: dict) -> None:
    d["backup_at"] = "2026-11-10T09:00:00"


PROBLEMS = [
    (_raising(httpx.ConnectError("refused")), customers.UNREACHABLE, "connect"),
    (_raising(httpx.ReadTimeout("slow")), customers.UNREACHABLE, "timeout"),
    (
        lambda r: httpx.Response(401, json={"ok": False}),
        customers.REFUSED,
        "401",
    ),
    (
        lambda r: httpx.Response(403, json={"ok": False}),
        customers.REFUSED,
        "403",
    ),
    (
        lambda r: httpx.Response(
            200,
            json={
                "ok": False,
                "error": {"code": "not_found", "message": "no restaurant"},
            },
        ),
        customers.NO_RESTAURANT,
        "not-found",
    ),
    (
        lambda r: httpx.Response(200, content=b"<html>hello</html>"),
        customers.NOT_A_STATUS,
        "not-json",
    ),
    (
        lambda r: httpx.Response(200, json=[1, 2]),
        customers.NOT_A_STATUS,
        "json-list",
    ),
    (
        _serving("alpha", lambda d: d.pop("menu")),
        customers.NOT_A_STATUS,
        "missing-field",
    ),
    (
        _serving("alpha", lambda d: d["today"].update(calls="many")),
        customers.NOT_A_STATUS,
        "string-for-number",
    ),
    (_serving("alpha", _naive), customers.NOT_A_STATUS, "naive-datetime"),
    (
        _serving(
            "alpha",
            lambda d: d.update(
                alarms=[{"kind": "k", "last_at": _stamp(START), "count": 1}] * 51
            ),
        ),
        customers.NOT_A_STATUS,
        "51-alarms",
    ),
    (
        _serving("alpha", lambda d: d["tenant"].update(name="x" * 201)),
        customers.NOT_A_STATUS,
        "long-name",
    ),
    (
        lambda r: httpx.Response(200, content=b"x" * 70_000),
        customers.NOT_A_STATUS,
        "over-64k",
    ),
    (_serving("alpha", code=500), customers.NOT_A_STATUS, "500-valid-body"),
    (
        _serving("alpha", lambda d: d["tenant"].update(key="beta")),
        customers.OTHER_CUSTOMER,
        "other-customer",
    ),
]


@pytest.mark.parametrize(
    ("handler", "problem"),
    [pytest.param(h, p, id=i) for h, p, i in PROBLEMS],
)
def test_a_stack_that_cannot_give_a_status_is_a_danger_row_with_its_reason(
    handler, problem
):
    row = _one(handler)
    assert row.tone == "danger"
    assert row.problem == problem
    assert row.status is None


def test_a_redirect_to_another_host_is_not_followed():
    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        if request.url.host == "alpha-api":
            return httpx.Response(
                302, headers={"location": "http://evil:8000/v1/ops/status"}
            )
        return _serving("alpha")(request)

    row = _one(handler)
    assert hosts == ["alpha-api"]
    assert row.tone == "danger"


def test_rows_are_sorted_danger_then_warn_then_ok_by_key():
    handlers = {
        "b-ok-api": _serving("b-ok"),
        "a-ok-api": _serving("a-ok"),
        "c-warn-api": _serving("c-warn", lambda d: d.update(backup_at=None)),
        "e-down-api": _raising(httpx.ConnectError("x")),
        "d-down-api": _raising(httpx.ConnectError("x")),
    }
    rows = customers.overview(
        [_customer(k.removesuffix("-api"), f"{k[0]}9" * 20) for k in handlers],
        client=_factory(_by_host(handlers)),
    )
    assert [(r.key, r.tone) for r in rows] == [
        ("d-down", "danger"),
        ("e-down", "danger"),
        ("c-warn", "warn"),
        ("a-ok", "ok"),
        ("b-ok", "ok"),
    ]


def test_no_customers_gives_an_empty_list_and_asks_nobody():
    def never() -> httpx.Client:
        raise AssertionError("no client may be made")

    assert customers.overview([], client=never) == []


def test_one_failing_stack_does_not_hide_the_others():
    handlers = {
        "alpha-api": _serving("alpha"),
        "beta-api": _raising(httpx.ConnectError("down")),
        "gamma-api": _serving("gamma"),
    }
    rows = customers.overview(
        [
            _customer("alpha", TOKEN_A),
            _customer("beta", TOKEN_B),
            _customer("gamma", TOKEN_C),
        ],
        client=_factory(_by_host(handlers)),
    )
    assert [(r.key, r.tone) for r in rows] == [
        ("beta", "danger"),
        ("alpha", "ok"),
        ("gamma", "ok"),
    ]
    assert rows[0].problem == customers.UNREACHABLE


def test_a_stack_that_keeps_sending_is_too_slow_and_delays_nobody(monkeypatch):
    """Review of #801: the read timeout ends a wait without a byte, not an
    answer that trickles in. A handler that does not come back stands for such
    a stack; the healthy one beside it is still shown, and promptly."""
    monkeypatch.setattr(customers, "DEADLINE_SECONDS", 0.3)
    held = threading.Event()

    def slow(request: httpx.Request) -> httpx.Response:
        held.wait(10)
        return httpx.Response(200, json=_hull(_status("beta")))

    handlers = {"alpha-api": _serving("alpha"), "beta-api": slow}
    started = time.monotonic()
    try:
        rows = customers.overview(
            [_customer("alpha"), _customer("beta", TOKEN_B)],
            client=_factory(_by_host(handlers)),
        )
    finally:
        held.set()
    assert time.monotonic() - started < 3
    assert [(r.key, r.tone, r.problem) for r in rows] == [
        ("beta", "danger", customers.TOO_SLOW),
        ("alpha", "ok", ""),
    ]


def test_a_packed_answer_is_refused_before_it_is_unpacked():
    """Review of #801: the size limit counted bytes after HTTPX had unpacked
    them, so 32 kB of gzip became 32 MB first. No packed answer is asked for,
    and one that comes anyway is not read."""
    packed = gzip.compress(b" " * (32 * 1024 * 1024))
    assert len(packed) < customers.MAX_ANSWER_BYTES
    seen: list[httpx.Request] = []

    class AsItArrives(httpx.SyncByteStream):
        """A body the client has not read yet, as from a network: built with
        `content=`, HTTPX would unpack it here in the test already."""

        def __iter__(self):
            yield packed

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            stream=AsItArrives(),
            headers={"Content-Encoding": "gzip", "Content-Type": "application/json"},
        )

    tracemalloc.start()
    try:
        row = _one(handler)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert (row.tone, row.problem) == ("danger", customers.NOT_A_STATUS)
    assert seen[0].headers["accept-encoding"] == "identity"
    assert peak < 4 * 1024 * 1024


@pytest.mark.parametrize(
    "stamp",
    [
        "9999-12-31T23:59:59+00:00",
        "0001-01-01T00:00:00+00:00",
        "1999-12-31T23:59:59+00:00",
        "2100-01-01T00:00:00+00:00",
    ],
)
@pytest.mark.parametrize("where", ["backup_at", "last_call_at", "bridge", "alarm"])
def test_a_time_the_page_cannot_show_is_not_a_status(stamp, where):
    """Review of #801: the year 9999 is a valid time with an offset and
    overflows when it is turned into the restaurants' time."""

    def mutate(data: dict) -> None:
        if where == "bridge":
            data["print_bridge"]["last_poll_at"] = stamp
        elif where == "alarm":
            data["alarms"] = [{"kind": "cost", "last_at": stamp, "count": 1}]
        else:
            data[where] = stamp

    row = _one(_serving("alpha", mutate))
    assert (row.tone, row.problem) == ("danger", customers.NOT_A_STATUS)


def test_one_stack_with_an_impossible_time_does_not_take_the_page_down(
    client, factors, clock, sessions, monkeypatch
):
    _two_customers(sessions)

    def far(data: dict) -> None:
        data["backup_at"] = "9999-12-31T23:59:59+00:00"

    _stacks(
        monkeypatch,
        {"alpha-api": _serving("alpha"), "beta-api": _serving("beta", far)},
    )
    _signed_in(client, factors, clock)
    for path in ("/", "/customers/rows"):
        page = client.get(path)
        assert page.status_code == 200, path
        assert "Restaurant alpha" in page.text
        assert customers.NOT_A_STATUS in page.text


# --- the pages ----------------------------------------------------------------


def _stacks(monkeypatch, handlers) -> None:
    monkeypatch.setattr(customers, "_client", _factory(_by_host(handlers)))


def test_the_stacks_are_plain_values_and_the_read_leaves_nothing_open(sessions):
    """The stacks are asked from several threads for up to three seconds: a
    database row there broke the page (16.11.2026, a rollback expires rows and
    two threads then load them on one session), and an open transaction would
    wait with them. The token is not in a row's printed form."""
    _two_customers(sessions)
    with sessions() as db:
        found = customers.stacks(db)
        assert not db.in_transaction()
    assert [(s.key, s.api_url, s.ops_token) for s in found] == [
        ("alpha", _url("alpha"), TOKEN_A),
        ("beta", _url("beta"), TOKEN_B),
    ]
    assert TOKEN_A not in repr(found)


def _two_customers(sessions) -> None:
    with sessions() as db:
        add_customer(db, "alpha", _url("alpha"), TOKEN_A)
        add_customer(db, "beta", _url("beta"), TOKEN_B)


def test_the_list_needs_a_session(client):
    page = client.get("/", follow_redirects=False)
    assert page.status_code == 303 and page.headers["location"] == "/login"
    rows = client.get("/customers/rows", follow_redirects=False)
    assert rows.status_code == 401
    assert rows.headers["hx-redirect"] == "/login"


def test_the_page_shows_every_customer_and_the_fragment_refreshes_it(
    client, factors, clock, sessions, monkeypatch
):
    _two_customers(sessions)
    _stacks(
        monkeypatch,
        {
            "alpha-api": _serving(
                "alpha", lambda d: d["tenant"].update(name="Pizza A")
            ),
            "beta-api": _serving("beta", lambda d: d["tenant"].update(name="Burger B")),
        },
    )
    _signed_in(client, factors, clock)
    page = client.get("/")
    assert page.status_code == 200
    for expected in ("Pizza A", "Burger B", "alpha", "beta", "ok", "ai_first", "calls"):
        assert expected in page.text, expected
    assert 'hx-get="/customers/rows"' in page.text
    assert 'hx-trigger="every 30s"' in page.text
    fragment = client.get("/customers/rows")
    assert fragment.status_code == 200
    assert "Pizza A" in fragment.text and "Burger B" in fragment.text
    for text in (page.text, fragment.text):
        assert TOKEN_A not in text and TOKEN_B not in text


def test_what_a_stack_says_is_shown_as_text(
    client, factors, clock, sessions, monkeypatch
):
    name = "<script>alert(1)</script>"
    kind = "<img src=x onerror=1>"
    with sessions() as db:
        add_customer(db, "alpha", _url("alpha"), TOKEN_A)

    def hostile(d: dict) -> None:
        d["tenant"]["name"] = name
        d["alarms"] = [{"kind": kind, "last_at": _stamp(START), "count": 2}]

    _stacks(monkeypatch, {"alpha-api": _serving("alpha", hostile)})
    _signed_in(client, factors, clock)
    for answer in (client.get("/"), client.get("/customers/rows")):
        assert answer.status_code == 200
        assert "&lt;script&gt;" in answer.text
        assert name not in answer.text
        assert kind not in answer.text
        assert "&lt;img" in answer.text


def test_the_fragment_sets_no_cookie(client, factors, clock, sessions, monkeypatch):
    _two_customers(sessions)
    _stacks(
        monkeypatch,
        {"alpha-api": _serving("alpha"), "beta-api": _serving("beta")},
    )
    _signed_in(client, factors, clock)
    answer = client.get("/customers/rows")
    assert answer.status_code == 200
    assert "set-cookie" not in answer.headers


def test_the_refresh_does_not_keep_the_session_alive(client, factors, clock):
    _signed_in(client, factors, clock)
    clock.at = START + timedelta(minutes=29)
    assert client.get("/customers/rows").status_code == 200
    clock.at = START + timedelta(minutes=31)
    page = client.get("/", follow_redirects=False)
    assert page.status_code == 303 and page.headers["location"] == "/login"


def test_a_page_load_does_keep_the_session_alive(client, factors, clock):
    """Control for the test above: the same times with the page itself."""
    _signed_in(client, factors, clock)
    clock.at = START + timedelta(minutes=29)
    assert client.get("/").status_code == 200
    clock.at = START + timedelta(minutes=31)
    assert client.get("/", follow_redirects=False).status_code == 200


# --- the filters --------------------------------------------------------------


@pytest.mark.parametrize(
    ("at", "expected"),
    [
        (None, "never"),
        (START - timedelta(seconds=20), "just now"),
        (START - timedelta(minutes=5), "5 min ago"),
        (START - timedelta(hours=3), "3 h ago"),
        (START - timedelta(hours=50), "2 d ago"),
        (START + timedelta(minutes=10), "just now"),
    ],
)
def test_ago_names_the_one_unit_that_says_enough(at, expected):
    assert web._ago(at, START) == expected


def test_shown_is_the_wall_time_of_the_restaurants():
    winter = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
    summer = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
    assert web._shown(winter) == "15.01.2026 13:00"
    assert web._shown(summer) == "15.07.2026 14:00"
    assert web._shown(None) == ""


# --- the templates ------------------------------------------------------------


def test_the_dashboard_templates_need_nothing_the_policy_forbids():
    """`default-src 'self'` blocks inline code and styles and every other host
    (the checks of test_deploy_exposure.py, for the dashboard's own templates)."""
    templates = sorted(TEMPLATES.rglob("*.html"))
    assert len(templates) >= 3
    for template in templates:
        text = template.read_text(encoding="utf-8")
        assert not re.search(r"<script(?![^>]*\bsrc=)", text), template
        assert not re.search(r"<style\b|\sstyle=|\son[a-z]+=", text), template
        assert not re.search(r"(src|href)=\"(https?:)?//", text), template
