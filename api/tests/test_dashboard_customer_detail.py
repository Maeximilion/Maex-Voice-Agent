"""The customer page of the operator dashboard (#571, docs/23 §8): the detail of
one stack, the call list behind the step-up, a reason and both audits, and the
stack as a hostile source. Against a scratch operator database and a mock
transport, as `test_dashboard_customers.py`."""

import copy
import gzip
import json
import logging
import re
import threading
import time
from collections.abc import Callable
from datetime import date, timedelta
from typing import Any

import httpx
import pytest

from api.core import ops_reason
from api.dashboard import access, customers
from api.dashboard import app as web
from api.dashboard.customers import BadCustomer, add_customer
from api.tests import test_dashboard_customers as base
from api.tests.test_dashboard_customers import (
    TOKEN_A,
    TOKEN_B,
    _audit,
    _customer,
    _factory,
    _hull,
    _raising,
    _signed_in,
    _stacks,
    _status,
    _two_customers,
    _url,
)

# Fixtures of the list tests, taken by assignment: an import is flagged as
# unused, and then as redefined by every parameter of the same name.
operator_url = base.operator_url
sessions = base.sessions
clock = base.clock
client = base.client
factors = base.factors

DAY = date(2026, 11, 10)
REASON = "restaurant_report #812"
CALLS_READ = "customer.calls_asked"
STALE = timedelta(minutes=6)


# --- what a healthy stack answers ---------------------------------------------


def _figures_one() -> dict:
    return {
        "calls": 12,
        "orders": 7,
        "missed": 1,
        "unknown_cost_calls": 0,
        "accuracy_percent": 93,
        "cost_per_order_cents": 12,
        "handed_over_percent": 20,
    }


def _figures(key: str, days: int = 7) -> dict:
    """What `GET /v1/ops/figures` answers (api/ops/router.py)."""
    return {
        "tenant": {"key": key, "name": f"Restaurant {key}"},
        "business_day": DAY.isoformat(),
        "week": _figures_one(),
        "days": [
            {"day": (DAY - timedelta(days=back)).isoformat(), **_figures_one()}
            for back in range(days)
        ],
    }


def _call_row(**over: Any) -> dict:
    return {
        "started_at": "2026-11-10T09:00:00+00:00",
        "caller": 1,
        "intent": "pickup",
        "outcome": "completed",
        "duration_seconds": 125,
        "cost_cents": 1234,
        "tool_calls": 3,
        "line_note": None,
    } | over


def _calls(key: str, calls: list[dict] | None = None, day: str = "2026-11-10") -> dict:
    """What `GET /v1/ops/calls` answers."""
    rows = [_call_row()] if calls is None else calls
    return {
        "tenant": {"key": key, "name": f"Restaurant {key}"},
        "business_day": day,
        "calls": rows,
    }


Handler = Callable[[httpx.Request], httpx.Response]
Mutate = Callable[[dict], object]


def _says(data: dict, mutate: Mutate | None = None, code: int = 200) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        found = copy.deepcopy(data)
        if mutate:
            mutate(found)
        return httpx.Response(code, json=_hull(found))

    return handler


def _fig(mutate: Mutate | None = None, code: int = 200) -> Handler:
    return _says(_figures("alpha"), mutate, code)


def _cal(mutate: Mutate | None = None, code: int = 200, key: str = "alpha") -> Handler:
    return _says(_calls(key), mutate, code)


def _error(code: str, status: int = 200, message: str = "x") -> Handler:
    return lambda r: httpx.Response(
        status, json={"ok": False, "error": {"code": code, "message": message}}
    )


def _stack(
    key: str = "alpha",
    seen: list[httpx.Request] | None = None,
    *,
    status: Handler | None = None,
    figures: Handler | None = None,
    calls: Handler | None = None,
    name: str | None = None,
    menu: dict | None = None,
) -> Handler:
    """One stack: status, figures and calls by path, and a record of what it saw."""

    def named(data: dict, menu: dict | None = None) -> Handler:
        def mutate(d: dict) -> None:
            if name:
                d["tenant"]["name"] = name
            if menu:
                d["menu"].update(menu)

        return _says(data, mutate)

    own = {
        customers.STATUS_PATH: status or named(_status(key), menu),
        customers.FIGURES_PATH: figures or named(_figures(key)),
        customers.CALLS_PATH: calls or named(_calls(key)),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return own[request.url.path](request)

    return handler


def _paths(seen: list[httpx.Request]) -> list[str]:
    return sorted(r.url.path for r in seen)


def _detail(handler: Handler) -> customers.Detail:
    return customers.detail(_customer("alpha"), client=_factory(handler))


def _result(handler: Handler, **over: Any) -> customers.StackCalls | str:
    args: dict[str, Any] = {
        "day": DAY,
        "actor": "maxi",
        "reason": REASON,
        "client": _factory(handler),
    } | over
    return customers.calls(_customer("alpha"), **args)


class AsItArrives(httpx.SyncByteStream):
    """A body the client has not read yet, as from a network."""

    def __init__(self, body: bytes) -> None:
        self.body = body

    def __iter__(self):
        yield self.body


def _packed(data: dict) -> Handler:
    body = gzip.compress(json.dumps(_hull(data)).encode())
    return lambda r: httpx.Response(
        200,
        stream=AsItArrives(body),
        headers={"Content-Encoding": "gzip", "Content-Type": "application/json"},
    )


def _digits(data: dict) -> Handler:
    """A count written with 2000 digits, built by hand: valid JSON."""
    found = copy.deepcopy(data)
    found["week"]["calls"] = 31337
    body = json.dumps(_hull(found)).replace("31337", "9" * 2000, 1).encode()
    return lambda r: httpx.Response(200, content=body)


# --- customers.detail ---------------------------------------------------------


def test_a_healthy_stack_gives_status_and_figures_without_a_reason():
    seen: list[httpx.Request] = []

    found = _detail(_stack(seen=seen))

    assert found.row.tone == "ok" and found.row.status is not None
    assert found.figures is not None and found.figures_problem == ""
    assert found.figures.week.accuracy_percent == 93
    assert len(found.figures.days) == 7
    assert _paths(seen) == [customers.FIGURES_PATH, customers.STATUS_PATH]
    for request in seen:
        assert request.headers["authorization"] == f"Bearer {TOKEN_A}"
        assert "x-ops-reason" not in request.headers
        assert "x-ops-actor" not in request.headers


def _fig_data(mutate: Mutate) -> Handler:
    return _fig(mutate)


FIGURES_PROBLEMS = [
    pytest.param(
        _raising(httpx.ConnectError("x")), customers.UNREACHABLE, id="connect"
    ),
    pytest.param(
        lambda r: httpx.Response(401, json={"ok": False}), customers.REFUSED, id="401"
    ),
    pytest.param(
        lambda r: httpx.Response(403, json={"ok": False}), customers.REFUSED, id="403"
    ),
    pytest.param(
        _error("not_found", message=ops_reason.NO_RESTAURANT),
        customers.NO_RESTAURANT,
        id="not-found",
    ),
    pytest.param(_error("not_found"), customers.NO_SETTINGS, id="no-settings"),
    pytest.param(_error("invalid_input"), customers.NOT_TAKEN, id="invalid-input"),
    pytest.param(
        lambda r: httpx.Response(200, content=b"<html>x</html>"),
        customers.NOT_AN_ANSWER,
        id="not-json",
    ),
    pytest.param(
        lambda r: httpx.Response(200, json=[1, 2]),
        customers.NOT_AN_ANSWER,
        id="json-list",
    ),
    pytest.param(
        _fig(lambda d: d.pop("week")), customers.NOT_AN_ANSWER, id="missing-field"
    ),
    pytest.param(
        _fig(lambda d: d["week"].update(calls="many")),
        customers.NOT_AN_ANSWER,
        id="string-for-number",
    ),
    pytest.param(
        _fig(lambda d: d["week"].update(calls=-1)),
        customers.NOT_AN_ANSWER,
        id="negative",
    ),
    pytest.param(
        _fig(lambda d: d["week"].update(accuracy_percent=101)),
        customers.NOT_AN_ANSWER,
        id="percent-101",
    ),
    pytest.param(
        _fig(lambda d: d["week"].update(calls=10**10)),
        customers.NOT_AN_ANSWER,
        id="count-10-pow-10",
    ),
    pytest.param(_digits(_figures("alpha")), customers.NOT_AN_ANSWER, id="2000-digits"),
    pytest.param(
        _fig(lambda d: d["days"][0].update(day="0001-01-01")),
        customers.NOT_AN_ANSWER,
        id="day-year-1",
    ),
    pytest.param(
        _fig(lambda d: d["days"][0].update(day="9999-12-31")),
        customers.NOT_AN_ANSWER,
        id="day-year-9999",
    ),
    pytest.param(
        _fig(lambda d: d.update(business_day="0001-01-01")),
        customers.NOT_AN_ANSWER,
        id="business-day-year-1",
    ),
    pytest.param(
        _fig(lambda d: d.update(days=_figures("alpha", 32)["days"])),
        customers.NOT_AN_ANSWER,
        id="32-days",
    ),
    pytest.param(
        lambda r: httpx.Response(200, content=b"x" * 70_000),
        customers.NOT_AN_ANSWER,
        id="over-64k",
    ),
    pytest.param(_fig(code=500), customers.NOT_AN_ANSWER, id="500-valid-body"),
    pytest.param(_packed(_figures("alpha")), customers.NOT_AN_ANSWER, id="gzip"),
    pytest.param(
        _fig(lambda d: d["tenant"].update(key="beta")),
        customers.OTHER_CUSTOMER,
        id="other-customer",
    ),
]


@pytest.mark.parametrize(("figures", "problem"), FIGURES_PROBLEMS)
def test_a_figures_answer_that_is_none_is_one_sentence_beside_a_good_row(
    figures, problem
):
    found = _detail(_stack(figures=figures))

    assert found.figures is None
    assert found.figures_problem == problem
    assert found.row.tone == "ok" and found.row.status is not None


def test_a_redirect_of_the_figures_to_another_host_is_not_followed():
    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        if request.url.path == customers.FIGURES_PATH:
            return httpx.Response(302, headers={"location": "http://evil:8000/x"})
        return _stack()(request)

    found = _detail(handler)

    assert "evil" not in hosts
    assert found.figures is None and found.figures_problem == customers.NOT_AN_ANSWER


def test_the_figures_are_there_while_the_status_is_down():
    found = _detail(_stack(status=_raising(httpx.ConnectError("down"))))

    assert found.row.tone == "danger" and found.row.problem == customers.UNREACHABLE
    assert found.figures is not None


@pytest.mark.parametrize("day", ["0001-01-01", "9999-12-31"])
def test_a_status_day_the_page_cannot_count_back_from_is_not_a_status(day):
    found = _detail(
        _stack(status=_says(_status("alpha"), lambda d: d.update(business_day=day)))
    )

    assert found.row.status is None
    assert found.row.problem == customers.NOT_A_STATUS


def test_figures_that_keep_sending_are_too_slow_and_the_status_stays(monkeypatch):
    monkeypatch.setattr(customers, "DEADLINE_SECONDS", 0.3)
    held = threading.Event()

    def slow(request: httpx.Request) -> httpx.Response:
        held.wait(10)
        return httpx.Response(200, json=_hull(_figures("alpha")))

    started = time.monotonic()
    try:
        found = _detail(_stack(figures=slow))
    finally:
        held.set()
    assert time.monotonic() - started < 2
    assert found.figures_problem == customers.TOO_SLOW
    assert found.row.tone == "ok" and found.row.status is not None


def test_a_figures_handler_that_raises_is_not_asked_and_leaks_no_token(caplog):
    found = _detail(_stack(figures=_raising(ValueError("boom"))))

    assert found.figures_problem == customers.NOT_ASKED
    assert found.row.tone == "ok"
    assert TOKEN_A not in caplog.text


# --- customers.calls ----------------------------------------------------------


def test_the_calls_request_carries_day_reason_actor_and_token():
    seen: list[httpx.Request] = []

    found = _result(_stack(seen=seen))

    assert isinstance(found, customers.StackCalls)
    assert found.calls[0].caller == 1
    (request,) = seen
    assert request.url.path == customers.CALLS_PATH
    assert request.url.query == b"day=2026-11-10"
    assert request.headers["x-ops-reason"] == REASON
    assert request.headers["x-ops-actor"] == "maxi"
    assert request.headers["authorization"] == f"Bearer {TOKEN_A}"
    assert request.headers["accept-encoding"] == "identity"


def _never() -> httpx.Client:
    raise AssertionError("no client may be made")


@pytest.mark.parametrize("actor", ["Maxi", "ma\nxi", "", "x" * 41])
def test_calls_without_a_valid_account_ask_nobody(actor):
    with pytest.raises(ValueError):
        customers.calls(_customer("alpha"), DAY, actor, REASON, client=_never)


@pytest.mark.parametrize(
    "reason",
    [
        "free text",
        "restaurant_report",
        "curiosity #1",
        "restaurant_report #812\r\nX-Evil: 1",
    ],
)
def test_calls_without_a_listed_reason_ask_nobody(reason):
    with pytest.raises(ValueError):
        customers.calls(_customer("alpha"), DAY, "maxi", reason, client=_never)


def _many(d: dict) -> None:
    d["calls"] = [_call_row() for _ in range(2001)]


CALLS_PROBLEMS = [
    pytest.param(_cal(_many), customers.NOT_AN_ANSWER, id="2001-calls"),
    pytest.param(
        lambda r: httpx.Response(200, content=b"x" * 600_000),
        customers.NOT_AN_ANSWER,
        id="over-512k",
    ),
    pytest.param(
        _cal(lambda d: d.update(business_day="2026-11-09")),
        customers.NOT_AN_ANSWER,
        id="another-day",
    ),
    pytest.param(
        _cal(lambda d: d["calls"][0].update(caller=0)),
        customers.NOT_AN_ANSWER,
        id="caller-0",
    ),
    pytest.param(
        _cal(lambda d: d["calls"][0].update(caller=-1)),
        customers.NOT_AN_ANSWER,
        id="caller-minus-1",
    ),
    pytest.param(
        _cal(lambda d: d["calls"][0].update(caller=2001)),
        customers.NOT_AN_ANSWER,
        id="caller-2001",
    ),
    pytest.param(
        _cal(lambda d: d["calls"][0].update(caller="+4917655512345")),
        customers.NOT_AN_ANSWER,
        id="caller-a-number",
    ),
    pytest.param(
        _cal(lambda d: d["calls"][0].update(started_at="2026-11-10T09:00:00")),
        customers.NOT_AN_ANSWER,
        id="naive-time",
    ),
    pytest.param(
        _cal(lambda d: d["calls"][0].update(started_at="9999-12-31T23:59:59+00:00")),
        customers.NOT_AN_ANSWER,
        id="year-9999",
    ),
    pytest.param(
        _cal(lambda d: d["calls"][0].update(duration_seconds=-1)),
        customers.NOT_AN_ANSWER,
        id="negative-duration",
    ),
    pytest.param(
        _cal(lambda d: d["calls"][0].update(cost_cents=10**10)),
        customers.NOT_AN_ANSWER,
        id="huge-cost",
    ),
    pytest.param(
        _cal(lambda d: d["calls"][0].update(intent="x" * 201)),
        customers.NOT_AN_ANSWER,
        id="long-intent",
    ),
    pytest.param(_packed(_calls("alpha")), customers.NOT_AN_ANSWER, id="gzip"),
    pytest.param(_cal(code=500), customers.NOT_AN_ANSWER, id="500-valid-body"),
    pytest.param(
        lambda r: httpx.Response(401, json={"ok": False}), customers.REFUSED, id="401"
    ),
    pytest.param(_error("invalid_input", 400), customers.NOT_TAKEN, id="invalid-input"),
    pytest.param(_cal(key="beta"), customers.OTHER_CUSTOMER, id="other-customer"),
]


@pytest.mark.parametrize(("handler", "problem"), CALLS_PROBLEMS)
def test_a_hostile_calls_answer_is_one_sentence(handler, problem):
    assert _result(handler) == problem


def test_a_phone_number_a_stack_sends_anyway_is_dropped():
    number = "+4917655512345"
    found = _result(_cal(lambda d: d["calls"][0].update(caller_id=number)))

    assert isinstance(found, customers.StackCalls)
    assert not hasattr(found.calls[0], "caller_id")
    assert number not in repr(found)


# --- the registry -------------------------------------------------------------


def test_a_customer_named_rows_is_refused(sessions):
    with sessions() as db, pytest.raises(BadCustomer):
        add_customer(db, "rows", _url("rows"), TOKEN_A)


def test_stack_gives_plain_values_and_leaves_no_transaction(sessions):
    _two_customers(sessions)
    with sessions() as db:
        found = customers.stack(db, "alpha")
        assert not db.in_transaction()
        for key in ("gamma", "Alpha", "../alpha", ""):
            assert customers.stack(db, key) is None
    assert isinstance(found, customers.Stack)
    assert (found.key, found.api_url, found.ops_token) == (
        "alpha",
        _url("alpha"),
        TOKEN_A,
    )
    assert TOKEN_A not in repr(found)


# --- the pages ----------------------------------------------------------------


def _world(sessions, monkeypatch, alpha: Handler | None = None, **beta):
    """Both customers registered, their stacks mocked; what they saw is returned."""
    _two_customers(sessions)
    seen: list[httpx.Request] = []
    _stacks(
        monkeypatch,
        {
            "alpha-api": alpha or _stack("alpha", seen),
            "beta-api": _stack("beta", seen, **beta),
        },
    )
    return seen


def _csrf(client) -> str:
    return client.cookies.get("ops_csrf", "")


def _post(client, key: str = "alpha", headers=None, **fields):
    data = {
        "csrf_token": _csrf(client),
        "back": f"/customers/{key}",
        "reason": "restaurant_report",
        "reference": "812",
        "day": "2026-11-10",
    } | fields
    data = {k: v for k, v in data.items() if v is not None}
    return client.post(
        f"/customers/{key}/calls",
        data=data,
        headers=headers or {},
        follow_redirects=False,
    )


def _reads(sessions) -> list:
    return [r for r in _audit(sessions) if r.action == CALLS_READ]


def test_the_page_needs_a_session(client, sessions, monkeypatch):
    seen = _world(sessions, monkeypatch)

    page = client.get("/customers/alpha", follow_redirects=False)
    assert page.status_code == 303 and page.headers["location"] == "/login"
    assert client.post("/customers/alpha/calls", data={}).status_code == 401
    assert seen == []


def test_the_detail_page_shows_the_week_and_no_token(
    client, factors, clock, sessions, monkeypatch
):
    menu = {"items": 80, "price_differences": 3, "without_allergen_information": 5}
    _world(sessions, monkeypatch, alpha=_stack("alpha", menu=menu))
    _signed_in(client, factors, clock)

    page = client.get("/customers/alpha")

    assert page.status_code == 200
    text = page.text
    for expected in ("Restaurant alpha", "93 %", "0.12 EUR", "20 %", "Tue 10.11.2026"):
        assert expected in text, expected
    assert len(re.findall(r'data-label="Business day"', text)) == 7
    assert "80 dishes, 3 price differences to the register, 5 dishes without" in text
    assert 'href="/"' in text
    assert TOKEN_A not in text
    for path in ("/", "/customers/rows"):
        assert 'href="/customers/alpha"' in client.get(path).text


def test_a_customer_the_registry_does_not_hold_is_a_404_and_asks_nobody(
    client, factors, clock, sessions, monkeypatch
):
    seen = _world(sessions, monkeypatch)
    _signed_in(client, factors, clock)

    for path in ("/customers/gamma", "/customers/Alpha", "/customers/al%20pha"):
        assert client.get(path).status_code == 404, path
    assert seen == []
    assert client.get("/customers/rows").status_code == 200


def test_another_customer_at_the_address_is_named_and_not_shown(
    client, factors, clock, sessions, monkeypatch
):
    other = "Other Restaurant Name"
    _two_customers(sessions)
    _stacks(
        monkeypatch,
        {"alpha-api": _stack("beta", name=other), "beta-api": _stack("beta")},
    )
    _signed_in(client, factors, clock)

    page = client.get("/customers/alpha")

    assert page.status_code == 200
    assert customers.OTHER_CUSTOMER in page.text
    assert other not in page.text


def test_what_the_stack_says_is_markup_shown_as_text(
    client, factors, clock, sessions, monkeypatch
):
    name = "<script>alert(1)</script>"
    _world(sessions, monkeypatch, alpha=_stack("alpha", name=name))
    _signed_in(client, factors, clock)
    page = client.get("/customers/alpha")
    assert page.status_code == 200
    assert name not in page.text and "&lt;script&gt;" in page.text

    hostile = _call_row(
        intent="<img src=x onerror=1>", outcome="<b>x</b>", line_note="<i>y</i>"
    )
    _stacks(
        monkeypatch,
        {
            "alpha-api": _stack("alpha", calls=_says(_calls("alpha", [hostile]))),
            "beta-api": _stack("beta"),
        },
    )
    shown = _post(client)
    assert shown.status_code == 200
    for raw, escaped in (
        ("<img src=x onerror=1>", "&lt;img src=x onerror=1&gt;"),
        ("<b>x</b>", "&lt;b&gt;x&lt;/b&gt;"),
        ("<i>y</i>", "&lt;i&gt;y&lt;/i&gt;"),
    ):
        assert raw not in shown.text
        assert escaped in shown.text


def test_figures_nobody_could_ask_for_leave_the_page_standing(
    client, factors, clock, sessions, monkeypatch
):
    _world(
        sessions, monkeypatch, alpha=_stack("alpha", figures=_raising(ValueError("x")))
    )
    _signed_in(client, factors, clock)

    page = client.get("/customers/alpha")

    assert page.status_code == 200
    assert customers.NOT_ASKED in page.text
    assert "ai_first" in page.text


def test_a_fresh_session_gets_the_call_form_with_fourteen_days(
    client, factors, clock, sessions, monkeypatch
):
    _world(sessions, monkeypatch)
    _signed_in(client, factors, clock)

    text = client.get("/customers/alpha").text

    assert 'action="/customers/alpha/calls"' in text
    assert 'name="csrf_token"' in text
    assert 'name="back" value="/customers/alpha"' in text
    for code in ops_reason.REASONS:
        assert f'<option value="{code}">' in text
    days = re.findall(r'<option value="(\d{4}-\d\d-\d\d)">', text)
    assert days == [(DAY - timedelta(days=n)).isoformat() for n in range(14)]
    assert days[0] == "2026-11-10" and days[-1] == "2026-10-28"


def test_a_stale_second_factor_gets_a_link_to_the_step_up_instead(
    client, factors, clock, sessions, monkeypatch
):
    _world(sessions, monkeypatch)
    _signed_in(client, factors, clock)
    clock.at += STALE

    text = client.get("/customers/alpha").text

    assert 'action="/customers/alpha/calls"' not in text
    assert 'href="/step-up?next=/customers/alpha"' in text


def test_a_stack_without_a_status_offers_no_form_and_no_day(
    client, factors, clock, sessions, monkeypatch
):
    _world(
        sessions,
        monkeypatch,
        alpha=_stack("alpha", status=_raising(httpx.ConnectError("down"))),
    )
    _signed_in(client, factors, clock)

    text = client.get("/customers/alpha").text

    assert 'action="/customers/alpha/calls"' not in text
    assert not re.findall(r'<option value="\d{4}-', text)


def test_a_stale_factor_sends_the_post_to_the_step_up_and_asks_nobody(
    client, factors, clock, sessions, monkeypatch
):
    seen = _world(sessions, monkeypatch)
    _signed_in(client, factors, clock)
    clock.at += STALE

    answer = _post(client)

    assert answer.status_code == 303
    assert answer.headers["location"] == "/step-up?next=%2Fcustomers%2Falpha"
    assert seen == [] and _reads(sessions) == []


@pytest.mark.parametrize(
    ("fields", "headers"),
    [
        ({"csrf_token": None}, None),
        ({"csrf_token": "wrong"}, None),
        ({}, {"Origin": "https://evil.example"}),
    ],
    ids=["no-token", "wrong-token", "other-origin"],
)
def test_a_post_that_is_not_the_pages_own_is_refused(
    client, factors, clock, sessions, monkeypatch, fields, headers
):
    seen = _world(sessions, monkeypatch)
    _signed_in(client, factors, clock)

    assert _post(client, headers=headers, **fields).status_code == 403
    assert seen == [] and _reads(sessions) == []


def _calls_stack(seen, calls: list[dict] | None = None) -> Handler:
    return _stack("alpha", seen, calls=_says(_calls("alpha", calls)))


def test_a_good_post_shows_the_calls_and_writes_both_audits(
    client, factors, clock, sessions, monkeypatch
):
    seen: list[httpx.Request] = []
    rows = [
        _call_row(),
        _call_row(
            started_at="2026-11-10T09:30:00+00:00",
            caller=None,
            intent="complaint",
            outcome="transferred",
            duration_seconds=None,
            cost_cents=None,
        ),
    ]
    _world(sessions, monkeypatch, alpha=_calls_stack(seen, rows))
    _signed_in(client, factors, clock)

    answer = _post(client)

    assert answer.status_code == 200
    assert answer.headers["cache-control"] == "no-store"
    for expected in (
        "10:00",
        "10:30",
        "caller 1",
        "withheld",
        "pickup",
        "completed",
        "complaint",
        "handed to the team",
        "2:05",
        "12.34 EUR",
    ):
        assert expected in answer.text, expected
    (request,) = seen
    assert request.headers["x-ops-actor"] == "maxi"
    assert request.headers["x-ops-reason"] == REASON
    (row,) = _reads(sessions)
    assert (row.actor, row.target) == ("maxi", "alpha")
    assert row.detail == {
        "reason": "restaurant_report",
        "reference": "#812",
        "day": "2026-11-10",
    }
    (shown,) = [r for r in _audit(sessions) if r.action == "customer.calls_shown"]
    assert (shown.actor, shown.target) == ("maxi", "alpha")
    assert shown.detail == {"day": "2026-11-10", "calls": len(rows)}


def test_the_actor_is_the_session_never_the_input(
    client, factors, clock, sessions, monkeypatch
):
    seen: list[httpx.Request] = []
    _world(sessions, monkeypatch, alpha=_calls_stack(seen))
    _signed_in(client, factors, clock)

    _post(
        client,
        headers={"X-Ops-Actor": "eve"},
        actor="eve",
        **{"X-Ops-Actor": "eve"},
    )

    assert [r.headers["x-ops-actor"] for r in seen] == ["maxi"]


@pytest.mark.parametrize(
    "fields",
    [
        {"reason": "curiosity"},
        {"reference": "Mueller"},
        {"reference": ""},
        {"reference": "0"},
        {"reference": "12 Mueller"},
        {"day": "garbage"},
        {"day": "2026-13-45"},
        {"day": "10.11.2026"},
        {"day": ""},
        {"day": "0001-01-01"},
        {"day": "9999-12-31"},
    ],
)
def test_bad_input_goes_back_to_the_page_and_asks_nobody(
    client, factors, clock, sessions, monkeypatch, fields
):
    seen = _world(sessions, monkeypatch)
    _signed_in(client, factors, clock)

    answer = _post(client, **fields)

    assert answer.status_code == 303
    assert answer.headers["location"] == "/customers/alpha?note=reason-needed"
    assert seen == [] and _reads(sessions) == []
    assert web.NOTES["reason-needed"][1] in client.get(answer.headers["location"]).text


def test_a_note_the_page_does_not_know_shows_nothing(
    client, factors, clock, sessions, monkeypatch
):
    _world(sessions, monkeypatch)
    _signed_in(client, factors, clock)

    text = client.get("/customers/alpha?note=%3Cscript%3E").text

    assert "<script>" not in text and "&lt;script&gt;" not in text
    assert "banner danger" not in text


def test_another_pages_note_is_not_said_here(
    client, factors, clock, sessions, monkeypatch
):
    """A link must not make a customer's page say that a factor changed."""
    _world(sessions, monkeypatch)
    _signed_in(client, factors, clock)

    text = client.get("/customers/alpha?note=totp-removed").text

    assert web.NOTES["totp-removed"][1] not in text
    assert "banner danger" not in text and "banner ok" not in text


def test_the_operator_audit_is_there_before_the_stack_is_asked(
    client, factors, clock, sessions, monkeypatch
):
    existed: list[bool] = []

    def calls(request: httpx.Request) -> httpx.Response:
        existed.append(bool(_reads(sessions)))
        return _says(_calls("alpha"))(request)

    _world(sessions, monkeypatch, alpha=_stack("alpha", calls=calls))
    _signed_in(client, factors, clock)

    assert _post(client).status_code == 200
    assert existed == [True]


def test_an_unreachable_stack_still_leaves_the_audit_row(
    client, factors, clock, sessions, monkeypatch
):
    _world(
        sessions,
        monkeypatch,
        alpha=_stack("alpha", calls=_raising(httpx.ConnectError("down"))),
    )
    _signed_in(client, factors, clock)

    answer = _post(client)

    assert answer.status_code == 200
    assert customers.UNREACHABLE in answer.text
    assert len(_reads(sessions)) == 1
    # The audit tells a read that was tried from one that was shown (security
    # review of this pull request).
    actions = {r.action: r.detail for r in _audit(sessions)}
    assert "customer.calls_shown" not in actions
    assert actions["customer.calls_not_shown"] == {
        "day": "2026-11-10",
        "problem": customers.UNREACHABLE,
    }


def test_a_stack_without_the_route_is_an_older_version_not_a_missing_restaurant():
    """A 404 of the framework carries `not_found` too (`api/main.py`); only a
    200 with that code says the stack has no restaurant."""

    def older(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/ops/status":
            return _says(_status("alpha"))(request)
        body = {"ok": False, "error": {"code": "not_found", "message": "Not Found"}}
        return httpx.Response(404, json=body)

    found = customers.detail(_customer("alpha"), client=_factory(older))

    assert found.row.tone == "ok"
    assert found.figures is None
    assert found.figures_problem == customers.NOT_KNOWN


@pytest.mark.parametrize(
    ("calls", "words"),
    [
        (_cal(key="beta"), "another customer answers at this address"),
        (_error("invalid_input", 400), "does not take this request"),
    ],
    ids=["other-customer", "invalid-input"],
)
def test_a_stack_that_cannot_answer_is_said_in_words_with_no_call_row(
    client, factors, clock, sessions, monkeypatch, calls, words
):
    _world(sessions, monkeypatch, alpha=_stack("alpha", calls=calls))
    _signed_in(client, factors, clock)

    answer = _post(client)

    assert answer.status_code == 200
    assert words in answer.text
    assert "caller 1" not in answer.text


def test_a_customer_not_in_the_registry_gets_a_404_and_no_audit_row(
    client, factors, clock, sessions, monkeypatch
):
    seen = _world(sessions, monkeypatch)
    _signed_in(client, factors, clock)

    assert _post(client, "gamma").status_code == 404
    assert seen == [] and _reads(sessions) == []


def test_a_signed_out_session_cannot_post(
    client, factors, clock, sessions, monkeypatch
):
    seen = _world(sessions, monkeypatch)
    _signed_in(client, factors, clock)
    old = list(client.cookies.jar)
    out = client.post(
        "/logout", data={"csrf_token": _csrf(client)}, follow_redirects=False
    )
    assert out.status_code == 303
    for cookie in old:
        client.cookies.jar.set_cookie(cookie)

    assert _post(client).status_code == 401
    assert seen == []


def test_a_fresh_session_ends_with_the_break_glass(
    client, factors, clock, sessions, monkeypatch
):
    seen = _world(sessions, monkeypatch)
    _signed_in(client, factors, clock)
    with sessions() as db:
        access.reset_account(db, "maxi", "eine-andere-lange-phrase")

    assert _post(client).status_code == 401
    assert seen == []


def test_no_token_reaches_a_log_line(
    client, factors, clock, sessions, monkeypatch, caplog
):
    _world(sessions, monkeypatch)
    _signed_in(client, factors, clock)
    with caplog.at_level(logging.DEBUG):
        assert _post(client).status_code == 200
        client.get("/customers/alpha")

    assert TOKEN_A not in caplog.text and TOKEN_B not in caplog.text
