"""The dev tools and the "New customer" page of the operator dashboard (#571,
docs/23 §5): the recorded eval and measurement results, the log of one call
behind the step-up, a reason and both audits, the stack as a hostile source,
and the rule that nothing under /dev writes outside dev and test. Against a
scratch operator database and a mock transport, as
`test_dashboard_customer_detail.py`."""

import logging
import uuid
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from api.dashboard import app as web
from api.dashboard import customers, results
from api.dashboard.config import settings
from api.tests import test_dashboard_customers as base
from api.tests.test_dashboard_customer_detail import (
    REASON,
    STALE,
    Handler,
    Mutate,
    _csrf,
    _error,
    _says,
)
from api.tests.test_dashboard_customers import (
    TOKEN_A,
    TOKEN_B,
    _audit,
    _customer,
    _factory,
    _signed_in,
    _stacks,
    _two_customers,
)

# Fixtures of the list tests, taken by assignment (as the detail tests do).
operator_url = base.operator_url
sessions = base.sessions
clock = base.clock
client = base.client
factors = base.factors

CALL = uuid.UUID("3f2b8c1e-7a4d-4e0b-9c55-0d1e2f3a4b5c")
OTHER_CALL = uuid.UUID("11111111-2222-4333-8444-555555555555")
ASKED = "customer.call_log_asked"
SHOWN = "customer.call_log_shown"
NOT_SHOWN = "customer.call_log_not_shown"


# --- what a healthy stack answers ---------------------------------------------


def _tool(**over: Any) -> dict:
    return {
        "name": "lookup_menu",
        "duration_ms": 41.5,
        "ok": True,
        "error_code": None,
    } | over


def _log(key: str = "alpha", call_id: uuid.UUID = CALL, **over: Any) -> dict:
    """What `GET /v1/ops/calls/<call_id>` answers (api/ops/router.py)."""
    call = {
        "started_at": "2026-11-10T09:00:00+00:00",
        "ended_at": "2026-11-10T09:02:05+00:00",
        "duration_seconds": 125,
        "intent": "pickup",
        "outcome": "transferred",
        "transfer_reason": "human_requested",
        "line_note": None,
        "cost_cents": 1234,
        "model": "qwen3.6",
        "is_test": False,
        "tool_calls": [
            _tool(),
            _tool(name="draft_order", duration_ms=12.0, ok=False, error_code="gone"),
        ],
        "tool_calls_total": 2,
    } | over
    return {
        "tenant": {"key": key, "name": f"Restaurant {key}"},
        "call_id": str(call_id),
        "call": call,
    }


def _ans(mutate: Mutate | None = None, code: int = 200, key: str = "alpha") -> Handler:
    return _says(_log(key), mutate, code)


def _result(handler: Handler, **over: Any) -> customers.StackCall | str:
    args: dict[str, Any] = {
        "call_id": CALL,
        "actor": "maxi",
        "reason": REASON,
        "client": _factory(handler),
    } | over
    return customers.call_log(_customer("alpha"), **args)


def _call(d: dict) -> dict:
    return d["call"]


# --- the question to the stack --------------------------------------------------


def test_the_question_is_a_get_with_the_id_reason_actor_and_token():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _ans()(request)

    found = _result(handler)

    assert isinstance(found, customers.StackCall)
    (request,) = seen
    assert request.method == "GET"
    assert request.url.path == f"/v1/ops/calls/{CALL}"
    assert request.url.query == b""
    assert request.content == b""
    assert request.headers["authorization"] == f"Bearer {TOKEN_A}"
    assert request.headers["x-ops-reason"] == REASON
    assert request.headers["x-ops-actor"] == "maxi"
    assert found.call is not None
    assert [t.name for t in found.call.tool_calls] == ["lookup_menu", "draft_order"]


def _never() -> httpx.Client:
    raise AssertionError("nobody is asked")


@pytest.mark.parametrize("actor", ["Maxi", "ma\nxi", "", "x" * 41])
def test_a_call_without_a_valid_account_asks_nobody(actor):
    with pytest.raises(ValueError, match="nothing is asked"):
        customers.call_log(_customer("alpha"), CALL, actor, REASON, client=_never)


@pytest.mark.parametrize("reason", ["", "curious", "restaurant_report", "x #1\r\nY: z"])
def test_a_call_without_a_listed_reason_asks_nobody(reason):
    with pytest.raises(ValueError, match="nothing is asked"):
        customers.call_log(_customer("alpha"), CALL, "maxi", reason, client=_never)


def test_no_call_of_that_id_is_an_answer_without_a_call():
    found = _result(_says(_log() | {"call": None}))

    assert isinstance(found, customers.StackCall)
    assert found.call is None


def _set(key: str, value: Any) -> Mutate:
    return lambda d: _call(d).__setitem__(key, value)


def _tools(value: Any) -> Mutate:
    return lambda d: _call(d).__setitem__("tool_calls", value)


HOSTILE = [
    # Another call than the one asked for, another customer, no customer.
    (_says(_log(call_id=OTHER_CALL)), customers.NOT_AN_ANSWER),
    (_ans(key="beta"), customers.OTHER_CUSTOMER),
    (_ans(lambda d: d["tenant"].__setitem__("key", None)), customers.NO_KEY),
    (_ans(lambda d: d.__setitem__("call_id", "nope")), customers.NOT_AN_ANSWER),
    (_ans(lambda d: d.pop("call_id")), customers.NOT_AN_ANSWER),
    # Times and numbers a page cannot show.
    (_ans(_set("started_at", "9999-12-31T23:59:59+00:00")), customers.NOT_AN_ANSWER),
    (_ans(_set("ended_at", "0001-01-01T00:00:00+00:00")), customers.NOT_AN_ANSWER),
    (_ans(_set("started_at", "2026-11-10T09:00:00")), customers.NOT_AN_ANSWER),
    (_ans(_set("duration_seconds", 10**12)), customers.NOT_AN_ANSWER),
    (_ans(_set("cost_cents", 10**30)), customers.NOT_AN_ANSWER),
    (_ans(_set("cost_cents", -1)), customers.NOT_AN_ANSWER),
    (_ans(_set("tool_calls_total", 10**30)), customers.NOT_AN_ANSWER),
    (_ans(_set("is_test", "perhaps")), customers.NOT_AN_ANSWER),
    # Words that would fill the page.
    (_ans(_set("model", "m" * 201)), customers.NOT_AN_ANSWER),
    (_ans(_set("transfer_reason", "t" * 201)), customers.NOT_AN_ANSWER),
    (_ans(_set("intent", ["pickup"])), customers.NOT_AN_ANSWER),
    # The tool calls.
    (_ans(_tools([_tool()] * 201)), customers.NOT_AN_ANSWER),
    (_ans(_tools("many")), customers.NOT_AN_ANSWER),
    (_ans(_tools([_tool(name="n" * 201)])), customers.NOT_AN_ANSWER),
    (_ans(_tools([_tool(duration_ms=-1)])), customers.NOT_AN_ANSWER),
    (_ans(_tools([_tool(duration_ms=10**12)])), customers.NOT_AN_ANSWER),
    (_ans(_tools([_tool(error_code="e" * 201)])), customers.NOT_AN_ANSWER),
    (_ans(_tools([_tool(ok=None)])), customers.NOT_AN_ANSWER),
    (_ans(_tools([["lookup_menu"]])), customers.NOT_AN_ANSWER),
    # Not the route's answer at all.
    (_ans(code=500), customers.NOT_AN_ANSWER),
    (_ans(code=401), customers.REFUSED),
    (_ans(code=404), customers.NOT_KNOWN),
    (_error("not_found"), customers.NO_RESTAURANT),
    (_error("invalid_input", 400), customers.NOT_TAKEN),
    (
        lambda r: httpx.Response(200, content=b"x" * (65 * 1024)),
        customers.NOT_AN_ANSWER,
    ),
    (
        lambda r: httpx.Response(302, headers={"Location": "http://evil.example/"}),
        customers.NOT_AN_ANSWER,
    ),
]


@pytest.mark.parametrize(("handler", "problem"), HOSTILE)
def test_a_hostile_answer_is_one_sentence(handler, problem):
    assert _result(handler) == problem


@pytest.mark.parametrize("number", ["NaN", "Infinity", "-Infinity", "1e400"])
def test_a_duration_that_is_no_number_is_no_answer(number):
    body = (
        '{"ok": true, "data": {"tenant": {"key": "alpha", "name": "A"}, '
        f'"call_id": "{CALL}", "call": {{"started_at": "2026-11-10T09:00:00+00:00", '
        '"ended_at": null, "duration_seconds": null, "intent": null, '
        '"outcome": null, "transfer_reason": null, "line_note": null, '
        '"cost_cents": null, "model": null, "is_test": false, "tool_calls": '
        f'[{{"name": "x", "duration_ms": {number}, "ok": true, "error_code": null}}], '
        '"tool_calls_total": 1}}}'
    )
    answer = httpx.Response(200, content=body.encode())

    assert _result(lambda r: answer) == customers.NOT_AN_ANSWER


def test_a_number_or_a_sentence_a_stack_sends_anyway_is_dropped():
    def more(d: dict) -> None:
        _call(d)["caller_id"] = "+4972215550199"
        _call(d)["tool_calls"][0]["args"] = {"name": "Mueller"}

    found = _result(_ans(more))

    assert isinstance(found, customers.StackCall)
    assert "+4972215550199" not in repr(found)
    assert "Mueller" not in repr(found)


def test_a_stack_that_raises_is_not_asked_and_leaks_no_token(caplog):
    def handler(request: httpx.Request) -> httpx.Response:
        raise RuntimeError(f"boom {request.headers['authorization']}")

    with caplog.at_level(logging.DEBUG):
        found = _result(handler)

    assert found == customers.NOT_ASKED
    assert TOKEN_A not in caplog.text


# --- the recorded results ---------------------------------------------------------


EVAL_HEAD = (
    "date,run_at,report,model,suite,total,passed,crashed,pending,guessed,"
    "unconfirmed,missed_esc,false_esc_rate,refused,tokens_per_case,source,note\n"
)


def _eval_row(day: str, note: str = "ok", model: str = "scripted") -> str:
    return f"{day},,,{model},cases,41,40,0,1,0,0,0,0.0,,900,make-eval,{note}\n"


@pytest.fixture
def metrics(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "operator_metrics_dir", str(tmp_path))
    (tmp_path / "eval_history.csv").write_text(
        EVAL_HEAD
        + _eval_row("2026-11-01", "first")
        + _eval_row("2026-11-02", "second"),
        encoding="utf-8",
    )
    (tmp_path / "voice_history.csv").write_text(
        "date,package,metric,value,unit,limit,verdict,note,run_at\n"
        '2026-10-13,T-10.3,Silent wrong numbers,2.0,%,2 %,at limit,"1,464 sentences",\n',
        encoding="utf-8",
    )
    return tmp_path


def test_a_table_holds_the_named_columns_the_newest_row_first(metrics):
    found = results.table(str(metrics), results.EVALS)

    assert found.problem == ""
    assert found.columns[0] == "Date" and found.columns[-1] == "Note"
    assert found.total == 2
    assert [row[0] for row in found.rows] == ["2026-11-02", "2026-11-01"]
    assert found.rows[0][-1] == "second"
    # `source` and `report` are columns of the file and none of the page.
    assert "make-eval" not in repr(found)

    voice = results.table(str(metrics), results.MEASUREMENTS)
    assert voice.rows == [
        (
            "2026-10-13",
            "T-10.3",
            "Silent wrong numbers",
            "2.0",
            "%",
            "2 %",
            "at limit",
            "1,464 sentences",
        )
    ]


def test_only_the_newest_rows_are_shown_and_a_long_cell_is_cut(metrics):
    rows = [_eval_row(f"2026-{n:04d}", "n" * 1000) for n in range(120)]
    (metrics / "eval_history.csv").write_text(EVAL_HEAD + "".join(rows), "utf-8")

    found = results.table(str(metrics), results.EVALS)

    assert found.total == 120
    assert len(found.rows) == results.ROWS_SHOWN
    assert found.rows[0][0] == "2026-0119"
    assert all(len(cell) <= results.CELL_LENGTH for row in found.rows for cell in row)


def test_a_short_row_and_a_missing_column_are_empty_cells(metrics):
    (metrics / "eval_history.csv").write_text(
        "date,model\n2026-11-03\n", encoding="utf-8"
    )

    found = results.table(str(metrics), results.EVALS)

    assert found.rows == [("2026-11-03",) + ("",) * 12]


def _large(folder) -> None:
    (folder / "eval_history.csv").write_bytes(
        EVAL_HEAD.encode() + b"x" * results.MAX_BYTES
    )


def _no_columns(folder) -> None:
    (folder / "eval_history.csv").write_text("a;b;c\n1;2;3\n", encoding="utf-8")


def _binary(folder) -> None:
    (folder / "eval_history.csv").write_bytes(bytes(range(256)) * 40)


def _a_folder(folder) -> None:
    (folder / "eval_history.csv").unlink()
    (folder / "eval_history.csv").mkdir()


def _gone(folder) -> None:
    (folder / "eval_history.csv").unlink()


def _empty(folder) -> None:
    (folder / "eval_history.csv").write_bytes(b"")


def _one_huge_cell(folder) -> None:
    (folder / "eval_history.csv").write_text(
        EVAL_HEAD + '2026-11-01,"' + "y" * 200_000 + '"\n', encoding="utf-8"
    )


@pytest.mark.parametrize(
    ("spoil", "problem"),
    [
        (_large, results.TOO_LARGE),
        (_no_columns, results.NO_COLUMNS),
        (_binary, results.NO_COLUMNS),
        (_a_folder, results.UNREADABLE),
        (_gone, results.MISSING),
        (_one_huge_cell, results.UNREADABLE),
    ],
)
def test_a_file_that_cannot_be_shown_is_one_sentence(metrics, spoil, problem):
    spoil(metrics)

    found = results.table(str(metrics), results.EVALS)

    assert found.problem == problem
    assert found.rows == []


def test_an_empty_file_is_a_table_without_rows(metrics):
    _empty(metrics)

    found = results.table(str(metrics), results.EVALS)

    assert (found.problem, found.rows, found.total) == ("", [], 0)


def test_a_folder_that_is_not_there_is_one_sentence(tmp_path):
    found = results.table(str(tmp_path / "nowhere"), results.EVALS)

    assert found.problem == results.MISSING


def test_reading_writes_nothing(metrics):
    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in metrics.iterdir()}

    results.table(str(metrics), results.EVALS)
    results.table(str(metrics), results.MEASUREMENTS)

    after = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in metrics.iterdir()}
    assert after == before


# --- the pages --------------------------------------------------------------------


def _stack(
    key: str, seen: list[httpx.Request], answer: Handler | None = None
) -> Handler:
    """One stack that answers the call log and the status, and notes what it saw."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.startswith(customers.CALLS_PATH + "/"):
            return (answer or _says(_log(key)))(request)
        return _says(base._status(key))(request)

    return handler


def _world(sessions, monkeypatch, alpha: Handler | None = None) -> list[httpx.Request]:
    _two_customers(sessions)
    seen: list[httpx.Request] = []
    _stacks(
        monkeypatch,
        {"alpha-api": _stack("alpha", seen, alpha), "beta-api": _stack("beta", seen)},
    )
    return seen


def _post(client, headers=None, **fields):
    data = {
        "csrf_token": _csrf(client),
        "back": "/dev",
        "customer": "alpha",
        "call_id": str(CALL),
        "reason": "restaurant_report",
        "reference": "812",
    } | fields
    data = {k: v for k, v in data.items() if v is not None}
    return client.post(
        "/dev/call", data=data, headers=headers or {}, follow_redirects=False
    )


def _rows(sessions, action: str) -> list:
    return [r for r in _audit(sessions) if r.action == action]


def _log_rows(sessions) -> list:
    return [r for r in _audit(sessions) if r.action.startswith("customer.call_log")]


@pytest.mark.parametrize("path", ["/dev", "/new-customer"])
def test_the_pages_need_a_session(client, sessions, monkeypatch, metrics, path):
    seen = _world(sessions, monkeypatch)

    page = client.get(path, follow_redirects=False)

    assert page.status_code == 303 and page.headers["location"] == "/login"
    assert "eval" not in page.text.lower()
    assert seen == []


def test_a_post_without_a_session_is_refused_and_asks_nobody(
    client, sessions, monkeypatch
):
    seen = _world(sessions, monkeypatch)
    client.get("/login")

    answer = _post(client)

    assert answer.status_code == 401
    assert seen == [] and _log_rows(sessions) == []


def test_the_dev_page_shows_the_results_and_asks_no_stack(
    client, factors, clock, sessions, monkeypatch, metrics
):
    seen = _world(sessions, monkeypatch)
    _signed_in(client, factors, clock)

    page = client.get("/dev")

    assert page.status_code == 200
    assert page.headers["cache-control"] == "no-store"
    for expected in (
        "Eval runs",
        "2026-11-02",
        "second",
        "Missed escalations",
        "Measurements",
        "Silent wrong numbers",
        "at limit",
        "1,464 sentences",
    ):
        assert expected in page.text, expected
    assert page.text.index("2026-11-02") < page.text.index("2026-11-01")
    assert seen == []
    assert TOKEN_A not in page.text and TOKEN_B not in page.text


def test_what_a_file_says_is_markup_shown_as_text(
    client, factors, clock, sessions, monkeypatch, metrics
):
    (metrics / "eval_history.csv").write_text(
        EVAL_HEAD + _eval_row("2026-11-03", "<script>alert(1)</script>"),
        encoding="utf-8",
    )
    _signed_in(client, factors, clock)

    page = client.get("/dev")

    assert "<script>alert(1)</script>" not in page.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page.text


def test_the_page_stands_without_the_files(
    client, factors, clock, sessions, monkeypatch, tmp_path
):
    monkeypatch.setattr(settings, "operator_metrics_dir", str(tmp_path / "nowhere"))
    _signed_in(client, factors, clock)

    page = client.get("/dev")

    assert page.status_code == 200
    assert page.text.count(results.MISSING) == 2
    assert str(tmp_path) not in page.text


def test_a_fresh_session_gets_the_form_with_the_registered_customers(
    client, factors, clock, sessions, monkeypatch, metrics
):
    _world(sessions, monkeypatch)
    _signed_in(client, factors, clock)

    page = client.get("/dev")

    assert 'action="/dev/call"' in page.text
    assert '<option value="alpha">alpha</option>' in page.text
    assert '<option value="beta">beta</option>' in page.text
    # No customer is chosen for the operator.
    assert "selected" not in page.text


def test_a_stale_second_factor_gets_a_link_to_the_step_up_instead(
    client, factors, clock, sessions, monkeypatch, metrics
):
    _world(sessions, monkeypatch)
    _signed_in(client, factors, clock)
    clock.at += STALE

    page = client.get("/dev")

    assert 'action="/dev/call"' not in page.text
    assert 'href="/step-up?next=/dev"' in page.text
    # The results hold nothing of a guest and need no fresh factor.
    assert "2026-11-02" in page.text


def test_without_a_customer_there_is_no_form(client, factors, clock, metrics):
    _signed_in(client, factors, clock)

    page = client.get("/dev")

    assert 'action="/dev/call"' not in page.text
    assert "No customer is registered yet." in page.text


def test_a_stale_factor_sends_the_post_to_the_step_up_and_asks_nobody(
    client, factors, clock, sessions, monkeypatch
):
    seen = _world(sessions, monkeypatch)
    _signed_in(client, factors, clock)
    client.get("/dev")
    clock.at += STALE

    answer = _post(client, back="https://evil.example/")

    assert answer.status_code == 303
    assert answer.headers["location"] == "/step-up?next=%2F"
    assert _post(client).headers["location"] == "/step-up?next=%2Fdev"
    assert seen == [] and _log_rows(sessions) == []


@pytest.mark.parametrize(
    ("headers", "token"),
    [
        ({"Origin": "https://evil.example"}, None),
        ({}, "wrong"),
        ({}, ""),
    ],
)
def test_a_post_that_is_not_the_pages_own_is_refused(
    client, factors, clock, sessions, monkeypatch, headers, token
):
    seen = _world(sessions, monkeypatch)
    _signed_in(client, factors, clock)
    client.get("/dev")
    fields = {} if token is None else {"csrf_token": token}

    answer = _post(client, headers=headers, **fields)

    assert answer.status_code == 403
    assert seen == [] and _log_rows(sessions) == []


def test_a_good_post_shows_the_log_and_writes_both_audits(
    client, factors, clock, sessions, monkeypatch
):
    seen = _world(sessions, monkeypatch)
    _signed_in(client, factors, clock)
    client.get("/dev")

    answer = _post(client, call_id=f"  {str(CALL).upper()} ", reference="#812")

    assert answer.status_code == 200
    assert answer.headers["cache-control"] == "no-store"
    for expected in (
        "Restaurant alpha",
        str(CALL),
        "10:00",
        "2:05",
        "pickup",
        "handed to the team",
        "human_requested",
        "12.34 EUR",
        "qwen3.6",
        "lookup_menu",
        "42 ms",
        "draft_order",
        "failed",
        "gone",
        "2 tool calls",
    ):
        assert expected in answer.text, expected
    (request,) = seen
    assert request.method == "GET"
    assert request.url.path == f"/v1/ops/calls/{CALL}"
    assert request.headers["x-ops-actor"] == "maxi"
    assert request.headers["x-ops-reason"] == REASON
    (asked,) = _rows(sessions, ASKED)
    assert (asked.actor, asked.target) == ("maxi", "alpha")
    assert asked.detail == {
        "reason": "restaurant_report",
        "reference": "#812",
        "call": str(CALL),
    }
    (shown,) = _rows(sessions, SHOWN)
    assert (shown.actor, shown.target) == ("maxi", "alpha")
    assert shown.detail == {"call": str(CALL), "tool_calls": 2}
    assert TOKEN_A not in answer.text


def test_the_actor_is_the_session_never_the_input(
    client, factors, clock, sessions, monkeypatch
):
    seen = _world(sessions, monkeypatch)
    _signed_in(client, factors, clock)
    client.get("/dev")

    _post(client, headers={"X-Ops-Actor": "eve"}, actor="eve", **{"X-Ops-Actor": "eve"})

    assert [r.headers["x-ops-actor"] for r in seen] == ["maxi"]
    assert [r.actor for r in _log_rows(sessions)] == ["maxi", "maxi"]


@pytest.mark.parametrize(
    "fields",
    [
        {"call_id": ""},
        {"call_id": "nope"},
        {"call_id": str(CALL) + "/../status"},
        {"call_id": str(CALL).replace("-", "")},
        {"call_id": f"{CALL}\n"[:-1] + "\r\nX: y"},
        {"call_id": "{" + str(CALL) + "}"},
        {"reason": ""},
        {"reason": "curious"},
        {"reference": ""},
        {"reference": "ticket of Mr Mueller"},
        {"customer": ""},
        {"customer": None},
    ],
)
def test_bad_input_goes_back_to_the_page_and_asks_nobody(
    client, factors, clock, sessions, monkeypatch, fields
):
    seen = _world(sessions, monkeypatch)
    _signed_in(client, factors, clock)
    client.get("/dev")

    answer = _post(client, **fields)

    assert answer.status_code == 303
    assert answer.headers["location"] == "/dev?note=call-needed"
    assert seen == [] and _log_rows(sessions) == []
    assert "No call shown" in client.get("/dev?note=call-needed").text


@pytest.mark.parametrize("note", ["nope", "passkey-added", "reason-needed"])
def test_a_note_that_is_not_this_pages_shows_nothing(
    client, factors, clock, metrics, note
):
    _signed_in(client, factors, clock)

    page = client.get(f"/dev?note={note}")

    assert page.status_code == 200
    assert 'role="status"' not in page.text


@pytest.mark.parametrize("key", ["gamma", "rows", "alpha/../beta", "ALPHA"])
def test_a_customer_the_registry_does_not_hold_is_a_404_and_no_audit_row(
    client, factors, clock, sessions, monkeypatch, key
):
    seen = _world(sessions, monkeypatch)
    _signed_in(client, factors, clock)
    client.get("/dev")

    answer = _post(client, customer=key)

    assert answer.status_code == 404
    assert seen == [] and _log_rows(sessions) == []


def test_the_operator_audit_is_there_before_the_stack_is_asked(
    client, factors, clock, sessions, monkeypatch
):
    rows_then: list[int] = []

    def alpha(request: httpx.Request) -> httpx.Response:
        rows_then.append(len(_rows(sessions, ASKED)))
        return _says(_log())(request)

    _world(sessions, monkeypatch, alpha=alpha)
    _signed_in(client, factors, clock)
    client.get("/dev")

    _post(client)

    assert rows_then == [1]


def test_no_call_of_that_id_is_said_and_audited_as_not_shown(
    client, factors, clock, sessions, monkeypatch
):
    _world(sessions, monkeypatch, alpha=_says(_log() | {"call": None}))
    _signed_in(client, factors, clock)
    client.get("/dev")

    answer = _post(client)

    assert answer.status_code == 200
    assert customers.NO_SUCH_CALL in answer.text
    assert "Restaurant alpha" in answer.text
    assert "lookup_menu" not in answer.text
    (row,) = _rows(sessions, NOT_SHOWN)
    assert row.detail == {"call": str(CALL), "problem": customers.NO_SUCH_CALL}
    assert _rows(sessions, SHOWN) == []


@pytest.mark.parametrize(
    ("alpha", "problem"),
    [
        (lambda r: (_ for _ in ()).throw(httpx.ConnectError("down")), "not reachable"),
        (_says(_log("beta")), customers.OTHER_CUSTOMER),
        (_says(_log(call_id=OTHER_CALL)), customers.NOT_AN_ANSWER),
        (lambda r: httpx.Response(404), customers.NOT_KNOWN),
    ],
)
def test_a_stack_that_cannot_answer_is_said_in_words_with_both_rows(
    client, factors, clock, sessions, monkeypatch, alpha, problem
):
    _world(sessions, monkeypatch, alpha=alpha)
    _signed_in(client, factors, clock)
    client.get("/dev")

    answer = _post(client)

    assert answer.status_code == 200
    assert problem in answer.text
    assert "lookup_menu" not in answer.text and "Restaurant beta" not in answer.text
    assert len(_rows(sessions, ASKED)) == 1
    (row,) = _rows(sessions, NOT_SHOWN)
    assert row.detail == {"call": str(CALL), "problem": problem}


def test_what_the_stack_says_is_markup_shown_as_text(
    client, factors, clock, sessions, monkeypatch
):
    evil = "<img src=x onerror=alert(1)>"

    def mutate(d: dict) -> None:
        d["tenant"]["name"] = evil
        _call(d).update(model=evil, intent=evil, outcome=evil, transfer_reason=evil)
        _call(d)["tool_calls"] = [_tool(name=evil, ok=False, error_code=evil)]

    _world(sessions, monkeypatch, alpha=_ans(mutate))
    _signed_in(client, factors, clock)
    client.get("/dev")

    answer = _post(client)

    assert answer.status_code == 200
    assert evil not in answer.text
    assert answer.text.count("&lt;img src=x onerror=alert(1)&gt;") == 7


def test_a_call_that_has_not_ended_says_so(
    client, factors, clock, sessions, monkeypatch
):
    def running(d: dict) -> None:
        _call(d).update(ended_at=None, duration_seconds=None, outcome=None)
        _call(d)["tool_calls"] = []

    _world(sessions, monkeypatch, alpha=_ans(running))
    _signed_in(client, factors, clock)
    client.get("/dev")

    answer = _post(client)

    for expected in ("not ended", "running", "The call made no tool call."):
        assert expected in answer.text, expected


def test_more_tool_calls_than_were_sent_are_said(
    client, factors, clock, sessions, monkeypatch
):
    _world(sessions, monkeypatch, alpha=_ans(_set("tool_calls_total", 250)))
    _signed_in(client, factors, clock)
    client.get("/dev")

    assert "The first 2 of 250 tool calls" in _post(client).text


def test_a_signed_out_session_cannot_post(
    client, factors, clock, sessions, monkeypatch
):
    seen = _world(sessions, monkeypatch)
    _signed_in(client, factors, clock)
    client.get("/dev")
    token = _csrf(client)
    client.post("/logout", data={"csrf_token": token})
    client.get("/login")

    assert _post(client).status_code == 401
    assert seen == [] and _log_rows(sessions) == []


def test_no_token_reaches_a_log_line(
    client, factors, clock, sessions, monkeypatch, caplog
):
    _world(sessions, monkeypatch)
    _signed_in(client, factors, clock)
    client.get("/dev")

    with caplog.at_level(logging.DEBUG):
        _post(client)

    assert TOKEN_A not in caplog.text and TOKEN_B not in caplog.text


# --- read-only in production --------------------------------------------------------


PROXY_SECRET = "p" * 40
PROXY = {"X-Maex-Proxy-Secret": PROXY_SECRET}


@pytest.fixture
def production(monkeypatch, client):
    monkeypatch.setattr(settings, "env", "prod")
    monkeypatch.setattr(settings, "operator_proxy_secret", PROXY_SECRET)
    return TestClient(web.app, base_url="https://testserver")


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "OPTIONS", "X"])
@pytest.mark.parametrize(
    "path", ["/dev", "/dev/", "/dev/evals/run", "/dev/sim", "/dev/call/again"]
)
def test_in_production_nothing_under_dev_takes_a_write(production, method, path):
    answer = production.request(method, path, headers=PROXY)

    assert answer.status_code == 405
    assert answer.text == "the dev tools only read"


def test_in_production_the_proxy_check_comes_first(production):
    assert production.post("/dev/evals/run").status_code == 403


def test_in_production_the_one_read_post_still_needs_everything(
    production, sessions, monkeypatch
):
    seen = _world(sessions, monkeypatch)

    answer = production.post(
        "/dev/call",
        data={"customer": "alpha", "call_id": str(CALL)},
        headers=PROXY,
        follow_redirects=False,
    )

    # Past the rule, and then no session: nothing is asked.
    assert answer.status_code == 401
    assert seen == [] and _log_rows(sessions) == []


def test_in_production_the_pages_are_read(production):
    page = production.get("/dev", headers=PROXY, follow_redirects=False)

    assert page.status_code == 303 and page.headers["location"] == "/login"


def test_outside_production_the_rule_stands_aside(client):
    # No such route: the router's own answer, not the rule's.
    assert client.post("/dev/evals/run").status_code == 404


def test_every_route_under_dev_reads():
    """The route table itself: a route under /dev takes GET, or it is one of
    the named reads. A new tool that writes must be named here and in docs/23."""
    found = {
        (getattr(route, "path", ""), method)
        for route in web.app.routes
        if getattr(route, "path", "").startswith("/dev")
        for method in getattr(route, "methods", set()) - {"HEAD"}
    }
    assert found == {("/dev", "GET"), ("/dev/call", "POST")}
    assert {path for path, method in found if method != "GET"} == set(web.DEV_READS)


def test_no_other_path_can_be_taken_for_the_dev_tools():
    """The rule goes by the path's start; a customer's page must not fall
    under it, nor a dev tool beside it."""
    others = [
        getattr(route, "path", "")
        for route in web.app.routes
        if not getattr(route, "path", "").startswith(("/dev/", "/dev{"))
        and getattr(route, "path", "") != "/dev"
    ]
    assert not [path for path in others if path.startswith("/dev")]


# --- "New customer" ---------------------------------------------------------------


def test_the_new_customer_page_names_the_commands_and_runs_nothing(
    client, factors, clock, sessions, monkeypatch
):
    seen = _world(sessions, monkeypatch)
    _signed_in(client, factors, clock)

    page = client.get("/new-customer")

    assert page.status_code == 200
    assert page.headers["cache-control"] == "no-store"
    for expected in (
        "scripts/new_stack.py customer.json --domain &lt;domain&gt; --dry-run",
        "python -m api.dashboard.cli customer-add &lt;key&gt; http://&lt;api container&gt;:8000",
        "at most 32 characters",
        "<code>rows</code>",
        "Never give it as an argument",
    ):
        assert expected in page.text, expected
    # The sign-out is the page's only form, and it has no field to fill in.
    assert page.text.count("<form") == 1
    assert "<input" not in page.text.replace(
        '<input type="hidden" name="csrf_token"', ""
    )
    assert "<select" not in page.text and "<textarea" not in page.text
    assert seen == []
    assert TOKEN_A not in page.text and TOKEN_B not in page.text
    assert not [r for r in _audit(sessions) if r.action.startswith("customer.")]


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE"])
def test_the_new_customer_page_takes_no_write(client, factors, clock, sessions, method):
    _signed_in(client, factors, clock)
    before = len(_audit(sessions))

    answer = client.request(
        method,
        "/new-customer",
        data={"csrf_token": _csrf(client), "key": "gamma", "token": "t" * 40},
    )

    assert answer.status_code == 405
    with sessions() as db:
        assert [c.key for c in customers.list_customers(db)] == []
    assert len(_audit(sessions)) == before


def test_the_navigation_names_both_pages(client, factors, clock):
    _signed_in(client, factors, clock)

    page = client.get("/")

    assert 'href="/dev"' in page.text and 'href="/new-customer"' in page.text
