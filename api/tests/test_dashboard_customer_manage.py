"""The write side of a customer's page in the operator dashboard (#571,
docs/23 §5 and §8): both PINs, devices, a setup link, the call mode and the
team number. Every step stands behind the step-up, the page's CSRF token, a
reason and both audits; no PIN and no link reaches an audit row, a log line or
an address; and the stack is a hostile source, as in the read tests. Against a
scratch operator database and a mock transport."""

import json
import logging
import re
from typing import Any

import httpx
import pytest

from api.core import ops_reason
from api.dashboard import app as web
from api.dashboard import customers
from api.tests import test_dashboard_customers as base
from api.tests.test_dashboard_customer_detail import (
    STALE,
    Handler,
    _csrf,
    _error,
    _says,
    _stack,
    _world,
)
from api.tests.test_dashboard_customers import (
    TOKEN_A,
    _audit,
    _customer,
    _factory,
    _raising,
    _signed_in,
)

# Fixtures of the list tests, taken by assignment (see the detail tests).
operator_url = base.operator_url
sessions = base.sessions
clock = base.clock
client = base.client
factors = base.factors

REASON = "restaurant_report #812"
PIN, NEW = "471123", "573921"
NUMBER = "+497215551234"
TABLET = "0b6f0c0e-52f1-4d0a-9d55-6f2f8f3f1a01"
TOKEN = "kT3vZ0yJq1mXw8Rr5uN2aQ7cB6dE9fGhLpOiUsVtYxA"
LINK = f"https://agent.example.com/gui/einrichten#{TOKEN}"
ACTIONS = sorted(customers.STEPS)
# A form each step takes.
GOOD: dict[str, dict[str, str]] = {
    "open": {},
    "pin-show": {"which": "team"},
    "pin-set": {"which": "admin", "pin": NEW},
    "sign-out": {"device": TABLET},
    "setup-link": {},
    "call-mode": {"mode": "primary"},
    "team-phone": {"number": NUMBER},
}
# What reaches the stack for it: path and body.
SENT: dict[str, tuple[str, dict[str, Any] | None]] = {
    "open": ("/v1/ops/access", None),
    "pin-show": ("/v1/ops/access/pin/show", {"which": "team"}),
    "pin-set": ("/v1/ops/access/pin", {"which": "admin", "pin": NEW}),
    "sign-out": ("/v1/ops/access/sign-out", {"device": TABLET}),
    "setup-link": ("/v1/ops/access/setup-link", None),
    "call-mode": ("/v1/ops/call-mode", {"mode": "primary"}),
    "team-phone": ("/v1/ops/team-phone", {"number": NUMBER}),
}
AUDIT_NAMES = {
    "open": "access",
    "pin-show": "pin_show",
    "pin-set": "pin_set",
    "sign-out": "sign_out",
    "setup-link": "setup_link",
    "call-mode": "call_mode",
    "team-phone": "team_phone",
}


def _access(key: str = "alpha", **over: Any) -> dict:
    """What a stack answers to the access read and after every step
    (api/ops/router.py)."""
    return {
        "tenant": {"key": key, "name": f"Restaurant {key}"},
        "login": True,
        "team_pin_set": True,
        "admin_pin_set": False,
        "devices": [
            {
                "id": TABLET,
                "device": "Mozilla/5.0 (iPad) Tablet",
                "created_at": "2026-11-01T08:00:00+00:00",
                "last_seen_at": "2026-11-10T08:55:00+00:00",
            }
        ],
        "call_mode": "shadow",
        "call_modes": ["shadow", "overflow", "primary", "paused"],
        "team_phone": "+4972215550100",
        "team_phones": ["+4972215550100", NUMBER],
    } | over


def _managed(
    seen: list[httpx.Request] | None = None, answer: Handler | None = None, **over: Any
) -> Handler:
    """Alpha's stack: the read side of the detail tests, and one answer for
    every step of the write side."""
    read = _stack("alpha")
    step = answer or _says(_access(**over))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path in (customers.STATUS_PATH, customers.FIGURES_PATH):
            return read(request)
        if seen is not None:
            seen.append(request)
        return step(request)

    return handler


def _post(client, action: str = "open", key: str = "alpha", headers=None, **fields):
    data: dict[str, Any] = (
        {
            "csrf_token": _csrf(client),
            "back": f"/customers/{key}",
            "reason": "restaurant_report",
            "reference": "812",
        }
        | GOOD.get(action, {})
        | fields
    )
    return client.post(
        f"/customers/{key}/manage/{action}",
        data={k: v for k, v in data.items() if v is not None},
        headers=headers or {},
        follow_redirects=False,
    )


def _rows(sessions, start: str = "customer.") -> list:
    return [r for r in _audit(sessions) if r.action.startswith(start)]


def _step(action: str, **form: str) -> customers.Step | None:
    return customers.step(action, GOOD.get(action, {}) | form)


def _result(handler: Handler, action: str = "open", **over: Any):
    step = _step(action)
    assert step is not None
    args: dict[str, Any] = {
        "actor": "maxi",
        "reason": REASON,
        "client": _factory(handler),
    } | over
    return customers.manage(_customer("alpha"), step, **args)


# --- customers.step: what a form may ask for ------------------------------------


@pytest.mark.parametrize("action", ACTIONS)
def test_a_good_form_is_one_request_to_the_stack(action):
    step = _step(action)

    assert step is not None
    path, body = SENT[action]
    assert (step.path, step.body) == (path, body)
    assert step.name == AUDIT_NAMES[action]
    assert step.method == ("GET" if action == "open" else "POST")


def test_all_devices_are_asked_for_in_words():
    step = _step("sign-out", device="all")
    assert step is not None
    assert step.body == {"all": True} and step.detail == {"device": "all"}


BAD_FORMS = [
    ("pin-show", {"which": "owner"}),
    ("pin-show", {"which": ""}),
    ("pin-set", {"which": "team", "pin": "123"}),
    ("pin-set", {"which": "team", "pin": "1234567"}),
    ("pin-set", {"which": "team", "pin": "12a4"}),
    ("pin-set", {"which": "team", "pin": "١٢٣٤"}),
    ("pin-set", {"which": "team", "pin": "1234\n"}),
    ("pin-set", {"which": "both", "pin": NEW}),
    ("sign-out", {"device": ""}),
    ("sign-out", {"device": "tablet"}),
    ("sign-out", {"device": "ALL"}),
    ("sign-out", {"device": TABLET + "0"}),
    ("call-mode", {"mode": ""}),
    ("call-mode", {"mode": "Primary"}),
    ("call-mode", {"mode": "<b>"}),
    ("call-mode", {"mode": "x" * 21}),
    ("team-phone", {"number": ""}),
    ("team-phone", {"number": "0721 5551234"}),
    ("team-phone", {"number": "+49 721 5551234"}),
    ("team-phone", {"number": "+4972155512341234567"}),
    ("delete-everything", {}),
    ("", {}),
]


@pytest.mark.parametrize(("action", "form"), BAD_FORMS)
def test_a_form_that_is_no_step_is_none(action, form):
    assert customers.step(action, GOOD.get(action, {}) | form) is None


def test_a_steps_printed_form_and_audit_detail_hold_no_pin():
    step = _step("pin-set")
    assert step is not None
    assert NEW not in repr(step) and NEW not in json.dumps(step.detail)
    assert step.detail == {"which": "admin"}
    phone = _step("team-phone")
    assert phone is not None and NUMBER not in json.dumps(phone.detail)


# --- customers.manage: one step, and the stack as a hostile source --------------


@pytest.mark.parametrize("action", ACTIONS)
def test_a_step_carries_token_actor_reason_customer_and_nothing_in_the_address(
    action,
):
    seen: list[httpx.Request] = []

    found = _result(_managed(seen), action)

    assert isinstance(found, customers.StackAccess)
    (request,) = seen
    path, body = SENT[action]
    assert request.method == ("GET" if action == "open" else "POST")
    assert str(request.url) == f"http://alpha-api:8000{path}"
    assert request.headers["authorization"] == f"Bearer {TOKEN_A}"
    assert request.headers["x-ops-actor"] == "maxi"
    assert request.headers["x-ops-reason"] == REASON
    assert request.headers["x-ops-customer"] == "alpha"
    assert (json.loads(request.content) if request.content else None) == body


def _never() -> httpx.Client:
    raise AssertionError("nobody may be asked")


@pytest.mark.parametrize(
    ("actor", "reason"),
    [
        ("Maxi", REASON),
        ("ma\nxi", REASON),
        ("", REASON),
        ("maxi", "curiosity #1"),
        ("maxi", "restaurant_report"),
        ("maxi", ""),
    ],
)
def test_a_step_without_an_account_or_a_listed_reason_asks_nobody(actor, reason):
    with pytest.raises(ValueError):
        _result(
            _raising(AssertionError("asked")),
            "pin-set",
            actor=actor,
            reason=reason,
            client=_never,
        )


def _with(**over: Any) -> Handler:
    return _says(_access(**over))


def _device(**over: Any) -> dict:
    return _access()["devices"][0] | over


HOSTILE = [
    (_with(pin="12ab"), customers.NOT_AN_ANSWER),
    (_with(pin="1234567"), customers.NOT_AN_ANSWER),
    (_with(pin="<b>1234</b>"), customers.NOT_AN_ANSWER),
    (_with(pin=1234), customers.NOT_AN_ANSWER),
    (_with(link="javascript:alert(1)"), customers.NOT_AN_ANSWER),
    (_with(link=LINK.replace("https://", "http://")), customers.NOT_AN_ANSWER),
    (_with(link=f"https://evil.example/login#{TOKEN}"), customers.NOT_AN_ANSWER),
    (
        _with(link=f"https://a.example/gui/einrichten?x=1#{TOKEN}"),
        customers.NOT_AN_ANSWER,
    ),
    (
        _with(link=f"https://user@a.example/gui/einrichten#{TOKEN}"),
        customers.NOT_AN_ANSWER,
    ),
    (_with(link=LINK + '"><script>'), customers.NOT_AN_ANSWER),
    (_with(link=LINK + "\n"), customers.NOT_AN_ANSWER),
    (_with(link=LINK + "x" * 300), customers.NOT_AN_ANSWER),
    (_with(link_expires_at="9999-12-31T23:59:59+00:00"), customers.NOT_AN_ANSWER),
    (_with(devices=[_device()] * 201), customers.NOT_AN_ANSWER),
    (_with(devices=[_device(id="tablet")]), customers.NOT_AN_ANSWER),
    (_with(devices=[_device(device="x" * 201)]), customers.NOT_AN_ANSWER),
    (
        _with(devices=[_device(last_seen_at="9999-12-31T23:59:59+00:00")]),
        customers.NOT_AN_ANSWER,
    ),
    (_with(devices="none"), customers.NOT_AN_ANSWER),
    (_with(call_mode="<script>"), customers.NOT_AN_ANSWER),
    (_with(call_modes=["shadow"] * 11), customers.NOT_AN_ANSWER),
    (_with(call_modes=["Shadow Mode!"]), customers.NOT_AN_ANSWER),
    (_with(team_phone="x" * 41), customers.NOT_AN_ANSWER),
    (_with(team_phones=["Mueller"]), customers.NOT_AN_ANSWER),
    (_with(team_phones=[NUMBER] * 21), customers.NOT_AN_ANSWER),
    (_with(signed_out=10**10), customers.NOT_AN_ANSWER),
    (_with(signed_out=-1), customers.NOT_AN_ANSWER),
    (_with(login="yes, certainly"), customers.NOT_AN_ANSWER),
    (_says(_access("beta")), customers.OTHER_CUSTOMER),
    (_says(_access(), lambda d: d["tenant"].update(key=None)), customers.NO_KEY),
    (_error("conflict", 409), customers.WRONG_STACK),
    (_error("invalid_input"), customers.NOT_TAKEN),
    (_error("invalid_input", 400), customers.NOT_TAKEN),
    (_error("invalid_input", 401), customers.REFUSED),
    (_error("not_found", 404), customers.NOT_KNOWN),
    (_error("not_found"), customers.NO_RESTAURANT),
    (_error("service_unavailable", 500), customers.NOT_AN_ANSWER),
    (lambda r: httpx.Response(200, content=b"x" * 70_000), customers.NOT_AN_ANSWER),
    (
        lambda r: httpx.Response(302, headers={"Location": "http://evil.example/"}),
        customers.NOT_AN_ANSWER,
    ),
    (_raising(httpx.ConnectError("down")), customers.UNREACHABLE),
    (_raising(ValueError("boom")), customers.NOT_ASKED),
]


@pytest.mark.parametrize(("handler", "problem"), HOSTILE)
def test_a_hostile_answer_to_a_step_is_one_sentence(handler, problem):
    assert _result(handler, "pin-show") == problem


def test_a_conflict_that_is_not_the_stacks_refusal_is_no_wrong_stack():
    """`conflict` with 200 is some other code's answer, not `_customer`'s."""
    assert _result(_error("conflict"), "pin-set") == customers.NOT_AN_ANSWER


def test_a_good_answer_keeps_the_secret_of_its_step_and_drops_what_it_does_not_know():
    found = _result(_with(pin=PIN, link=LINK, password_hash="scrypt$x", token="t"))

    assert isinstance(found, customers.StackAccess)
    assert (found.pin, found.link) == (PIN, LINK)
    assert not hasattr(found, "password_hash") and not hasattr(found, "token")
    # The path alone, as a stack without AGENT_DOMAIN answers it.
    bare = _result(_with(link=f"/gui/einrichten#{TOKEN}"))
    assert isinstance(bare, customers.StackAccess)


def test_a_step_is_asked_once_and_never_again():
    seen: list[httpx.Request] = []

    def broken(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        raise httpx.ReadTimeout("no answer")

    assert _result(broken, "setup-link") == customers.UNREACHABLE
    assert len(seen) == 1


def test_a_step_that_fails_leaks_neither_token_nor_pin_into_the_log(caplog):
    caplog.set_level(logging.DEBUG)

    assert (
        _result(_raising(ValueError(f"{TOKEN_A} {NEW}")), "pin-set")
        == customers.NOT_ASKED
    )

    assert TOKEN_A not in caplog.text and NEW not in caplog.text


# --- the customer's page offers the way in --------------------------------------


def test_a_fresh_session_gets_the_form_that_opens_access(
    client, factors, clock, sessions, monkeypatch
):
    _world(sessions, monkeypatch)
    _signed_in(client, factors, clock)

    text = client.get("/customers/alpha").text

    assert 'action="/customers/alpha/manage/open"' in text
    for code in ops_reason.REASONS:
        assert text.count(f'<option value="{code}">') == 2  # the calls form too
    assert text.count('name="back" value="/customers/alpha"') == 2


def test_a_stale_factor_gets_no_form_for_access(
    client, factors, clock, sessions, monkeypatch
):
    _world(sessions, monkeypatch)
    _signed_in(client, factors, clock)
    clock.at += STALE

    text = client.get("/customers/alpha").text

    assert "/manage/" not in text
    assert text.count('href="/step-up?next=/customers/alpha"') == 2


def test_the_access_form_is_there_while_the_stack_gives_no_status(
    client, factors, clock, sessions, monkeypatch
):
    """A stack that is down is when the operator needs the page; whether the
    step works is the step's own answer."""
    down = _stack("alpha", status=_raising(httpx.ConnectError("down")))
    _world(sessions, monkeypatch, alpha=down)
    _signed_in(client, factors, clock)

    assert (
        'action="/customers/alpha/manage/open"' in client.get("/customers/alpha").text
    )


# --- who may take a step --------------------------------------------------------


@pytest.mark.parametrize("action", ACTIONS)
def test_no_step_without_a_session(client, sessions, monkeypatch, action):
    seen: list[httpx.Request] = []
    _world(sessions, monkeypatch, alpha=_managed(seen))

    assert client.post(f"/customers/alpha/manage/{action}", data={}).status_code == 401
    assert client.get(f"/customers/alpha/manage/{action}").status_code in (404, 405)
    assert seen == [] and _rows(sessions) == []


@pytest.mark.parametrize("action", ACTIONS)
def test_a_stale_factor_sends_a_step_to_the_step_up_and_drops_it(
    client, factors, clock, sessions, monkeypatch, action
):
    seen: list[httpx.Request] = []
    _world(sessions, monkeypatch, alpha=_managed(seen))
    _signed_in(client, factors, clock)
    clock.at += STALE

    answer = _post(client, action)

    assert answer.status_code == 303
    assert answer.headers["location"] == "/step-up?next=%2Fcustomers%2Falpha"
    assert seen == [] and _rows(sessions) == []


@pytest.mark.parametrize("action", ACTIONS)
@pytest.mark.parametrize(
    ("fields", "headers"),
    [
        ({"csrf_token": None}, None),
        ({"csrf_token": "wrong"}, None),
        ({}, {"Origin": "https://evil.example"}),
        ({}, {"Origin": "null"}),
        ({}, {"Sec-Fetch-Site": "same-site"}),
    ],
    ids=["no-token", "wrong-token", "other-origin", "null-origin", "same-site"],
)
def test_a_step_that_is_not_the_pages_own_post_is_refused(
    client, factors, clock, sessions, monkeypatch, action, fields, headers
):
    """`same-site` is a customer's host under the same domain (docs/23 §5)."""
    seen: list[httpx.Request] = []
    _world(sessions, monkeypatch, alpha=_managed(seen))
    _signed_in(client, factors, clock)

    assert _post(client, action, headers=headers, **fields).status_code == 403
    assert seen == [] and _rows(sessions) == []


def test_a_customer_or_a_step_nobody_knows_is_a_404_and_no_audit_row(
    client, factors, clock, sessions, monkeypatch
):
    seen: list[httpx.Request] = []
    _world(sessions, monkeypatch, alpha=_managed(seen))
    _signed_in(client, factors, clock)

    assert _post(client, "open", key="gamma").status_code == 404
    for action in ("delete", "pin", "open/again", "PIN-SET"):
        assert _post(client, action).status_code == 404, action
    assert seen == [] and _rows(sessions) == []


@pytest.mark.parametrize("action", ACTIONS)
@pytest.mark.parametrize(
    "fields",
    [
        {"reason": "curiosity"},
        {"reason": None},
        {"reference": ""},
        {"reference": "Mueller"},
        {"reference": "0"},
    ],
)
def test_a_step_without_a_listed_reason_goes_back_and_asks_nobody(
    client, factors, clock, sessions, monkeypatch, action, fields
):
    seen: list[httpx.Request] = []
    _world(sessions, monkeypatch, alpha=_managed(seen))
    _signed_in(client, factors, clock)

    answer = _post(client, action, **fields)

    assert answer.status_code == 303
    assert answer.headers["location"] == "/customers/alpha?note=step-reason-needed"
    assert seen == [] and _rows(sessions) == []
    assert "Nothing opened or changed" in client.get(answer.headers["location"]).text


@pytest.mark.parametrize(("action", "form"), [b for b in BAD_FORMS if b[0] in ACTIONS])
def test_a_form_that_is_no_step_is_said_and_sent_nowhere(
    client, factors, clock, sessions, monkeypatch, action, form
):
    seen: list[httpx.Request] = []
    _world(sessions, monkeypatch, alpha=_managed(seen))
    _signed_in(client, factors, clock)

    answer = _post(client, action, **form)

    assert answer.status_code == 400
    assert "Nothing was sent" in answer.text
    assert seen == [] and _rows(sessions) == []
    # What was typed is not repeated, and the way back is one button.
    assert form.get("pin", NEW) not in answer.text
    assert 'action="/customers/alpha/manage/open"' in answer.text


# --- the steps ------------------------------------------------------------------


def _took(sessions, action: str, done: bool = True) -> tuple[Any, Any]:
    """The two rows of one step in the operator's audit."""
    name = AUDIT_NAMES[action]
    asked, after = _rows(sessions)[-2:]
    assert asked.action == f"customer.{name}_asked"
    assert after.action == f"customer.{name}_{'done' if done else 'not_done'}"
    for row in (asked, after):
        assert (row.actor, row.target) == ("maxi", "alpha")
    assert asked.detail["reason"] == "restaurant_report"
    assert asked.detail["reference"] == "#812"
    return asked, after


def test_open_shows_the_state_and_every_form_carries_the_reason(
    client, factors, clock, sessions, monkeypatch
):
    seen: list[httpx.Request] = []
    _world(sessions, monkeypatch, alpha=_managed(seen))
    _signed_in(client, factors, clock)

    answer = _post(client)

    assert answer.status_code == 200
    assert answer.headers["cache-control"] == "no-store"
    text = answer.text
    for expected in (
        "Restaurant alpha",
        "The restaurant reported a problem",
        "#812",
        "Mozilla/5.0 (iPad) Tablet",
        "01.11.2026 09:00",
        "10.11.2026 09:55",
        "+4972215550100",
        f'<option value="{NUMBER}">',
        '<option value="primary">',
        '<option value="paused">',
        f'name="device" value="{TABLET}"',
        'name="device" value="all"',
    ):
        assert expected in text, expected
    for action in ACTIONS:
        assert f'action="/customers/alpha/manage/{action}"' in text, action
    forms = text.count("<form")
    assert forms >= len(ACTIONS)
    # Every form posts the page's token, the way back and the reason. The
    # sign-out in the page's bar is the one form without a reason.
    assert text.count('name="csrf_token"') == forms
    assert text.count('name="reason" value="restaurant_report"') == forms - 1
    assert text.count('name="reference" value="#812"') == forms - 1
    assert text.count('name="back" value="/customers/alpha"') == forms - 1
    assert PIN not in text and TOKEN_A not in text
    assert len(seen) == 1
    _took(sessions, "open")


def test_a_stack_without_a_login_offers_the_link_and_no_pin_form(
    client, factors, clock, sessions, monkeypatch
):
    empty = _managed(login=False, team_pin_set=False, devices=[], call_mode=None)
    _world(sessions, monkeypatch, alpha=empty)
    _signed_in(client, factors, clock)

    text = _post(client).text

    assert 'action="/customers/alpha/manage/setup-link"' in text
    assert 'action="/customers/alpha/manage/pin-show"' not in text
    assert "No device is signed in" in text


def test_a_paused_agent_can_only_be_switched_on_again(
    client, factors, clock, sessions, monkeypatch
):
    paused = _managed(call_mode="paused", call_modes=["resume"])
    _world(sessions, monkeypatch, alpha=paused)
    _signed_in(client, factors, clock)

    text = _post(client).text

    assert '<option value="resume">' in text and '<option value="primary">' not in text


@pytest.mark.parametrize("action", ACTIONS)
def test_every_step_reaches_the_stack_once_with_the_sessions_account(
    client, factors, clock, sessions, monkeypatch, action
):
    seen: list[httpx.Request] = []
    _world(
        sessions, monkeypatch, alpha=_managed(seen, signed_out=1, pin=PIN, link=LINK)
    )
    _signed_in(client, factors, clock)

    answer = _post(
        client, action, headers={"X-Ops-Actor": "eve"}, actor="eve", account="eve"
    )

    assert answer.status_code == 200
    assert answer.headers["cache-control"] == "no-store"
    (request,) = seen
    path, body = SENT[action]
    assert (request.url.path, request.url.query) == (path, b"")
    assert (json.loads(request.content) if request.content else None) == body
    assert request.headers["x-ops-actor"] == "maxi"
    assert request.headers["x-ops-customer"] == "alpha"
    asked, done = _took(sessions, action)
    assert set(asked.detail) <= {"reason", "reference", "which", "device", "mode"}
    for secret in (NEW, PIN, TOKEN, NUMBER):
        assert secret not in json.dumps([asked.detail, done.detail])


def test_a_pin_is_shown_once_on_a_page_nobody_keeps(
    client, factors, clock, sessions, monkeypatch, caplog
):
    caplog.set_level(logging.DEBUG)
    _world(sessions, monkeypatch, alpha=_managed(pin=PIN))
    _signed_in(client, factors, clock)

    answer = _post(client, "pin-show")

    assert answer.text.count(PIN) == 1
    assert "The team PIN, shown once" in answer.text
    assert answer.headers["cache-control"] == "no-store"
    assert PIN not in caplog.text
    asked, _ = _took(sessions, "pin-show")
    assert asked.detail["which"] == "team"


def test_a_pin_that_is_not_set_is_said(client, factors, clock, sessions, monkeypatch):
    _world(sessions, monkeypatch, alpha=_managed(pin=None, team_pin_set=False))
    _signed_in(client, factors, clock)

    assert "No team PIN is set" in _post(client, "pin-show").text


def test_a_new_pin_goes_in_the_body_and_is_not_repeated_or_logged(
    client, factors, clock, sessions, monkeypatch, caplog
):
    caplog.set_level(logging.DEBUG)
    seen: list[httpx.Request] = []
    _world(sessions, monkeypatch, alpha=_managed(seen, admin_pin_set=True))
    _signed_in(client, factors, clock)

    answer = _post(client, "pin-set")

    assert "The admin PIN is changed" in answer.text
    assert NEW not in answer.text and NEW not in caplog.text
    (request,) = seen
    assert NEW not in str(request.url) and NEW not in str(request.headers)
    assert json.loads(request.content) == {"which": "admin", "pin": NEW}


@pytest.mark.parametrize(
    ("device", "body", "ended", "words"),
    [
        (TABLET, {"device": TABLET}, 1, "Signed out: 1 device."),
        (TABLET, {"device": TABLET}, 0, "No device was signed out"),
        ("all", {"all": True}, 3, "Signed out: 3 devices."),
    ],
)
def test_devices_are_signed_out_one_or_all(
    client, factors, clock, sessions, monkeypatch, device, body, ended, words
):
    seen: list[httpx.Request] = []
    _world(sessions, monkeypatch, alpha=_managed(seen, signed_out=ended, devices=[]))
    _signed_in(client, factors, clock)

    answer = _post(client, "sign-out", device=device)

    assert words in answer.text
    assert json.loads(seen[0].content) == body
    asked, _ = _took(sessions, "sign-out")
    assert asked.detail["device"] == device


def test_a_setup_link_is_shown_once_as_text_and_recorded_without_it(
    client, factors, clock, sessions, monkeypatch, caplog
):
    caplog.set_level(logging.DEBUG)
    answer_of_stack = _managed(link=LINK, link_expires_at="2026-11-11T09:00:00+00:00")
    _world(sessions, monkeypatch, alpha=answer_of_stack)
    _signed_in(client, factors, clock)

    answer = _post(client, "setup-link")

    assert answer.text.count(TOKEN) == 1
    assert "11.11.2026 10:00" in answer.text
    # Text to copy, never a link the operator's browser would follow.
    assert f'href="{LINK}' not in answer.text and 'href="https://' not in answer.text
    assert TOKEN not in caplog.text
    assert TOKEN not in json.dumps([r.detail for r in _rows(sessions)])


def test_a_stack_that_answers_a_link_step_without_a_link_is_said(
    client, factors, clock, sessions, monkeypatch
):
    _world(sessions, monkeypatch, alpha=_managed())
    _signed_in(client, factors, clock)

    answer = _post(client, "setup-link")

    assert "answered without a link" in answer.text
    _took(sessions, "setup-link", done=False)


def test_the_call_mode_and_the_team_number_are_read_from_the_answer(
    client, factors, clock, sessions, monkeypatch
):
    """The page says what the stack says holds now, not what was asked for."""
    _world(
        sessions, monkeypatch, alpha=_managed(call_mode="overflow", team_phone=NUMBER)
    )
    _signed_in(client, factors, clock)

    assert "The call mode is now overflow" in _post(client, "call-mode").text
    assert f"go to {NUMBER} today" in _post(client, "team-phone").text


# --- a stack that cannot or will not ---------------------------------------------

DEFINITE = [
    (_error("invalid_input", 401), customers.REFUSED),
    (_error("not_found", 404), customers.NOT_KNOWN),
    (_error("conflict", 409), customers.WRONG_STACK),
    (_error("not_found"), customers.NO_RESTAURANT),
]
UNSURE = [
    (_raising(httpx.ReadTimeout("slow")), customers.UNREACHABLE),
    (_raising(ValueError("boom")), customers.NOT_ASKED),
    (lambda r: httpx.Response(200, content=b"<html>"), customers.NOT_AN_ANSWER),
    (_says(_access("beta")), customers.OTHER_CUSTOMER),
]


@pytest.mark.parametrize(("handler", "problem"), DEFINITE + UNSURE)
@pytest.mark.parametrize("action", ["pin-set", "call-mode", "open"])
def test_a_step_that_gave_no_answer_is_said_in_words_and_in_the_audit(
    client, factors, clock, sessions, monkeypatch, action, handler, problem
):
    _world(sessions, monkeypatch, alpha=_managed(answer=handler))
    _signed_in(client, factors, clock)

    answer = _post(client, action)

    assert answer.status_code == 200
    assert problem in answer.text.replace("&#39;", "'")
    # Only a change the stack may have made after all gets the warning.
    unsure = action != "open" and (handler, problem) in UNSURE
    assert ("may have been made" in answer.text) is unsure
    _, after = _took(sessions, action, done=False)
    assert after.detail["problem"] == problem
    assert 'action="/customers/alpha/manage/open"' in answer.text
    assert "Restaurant beta" not in answer.text


@pytest.mark.parametrize(
    ("action", "words"),
    [
        ("pin-set", "refused this PIN"),
        ("call-mode", "refused this mode"),
        ("team-phone", "refused this number"),
        ("sign-out", customers.NOT_TAKEN),
    ],
)
def test_a_refusal_of_the_stack_names_what_it_can_be(
    client, factors, clock, sessions, monkeypatch, action, words
):
    _world(sessions, monkeypatch, alpha=_managed(answer=_error("invalid_input")))
    _signed_in(client, factors, clock)

    answer = _post(client, action)

    assert words in answer.text and "may have been made" not in answer.text
    _took(sessions, action, done=False)


def test_the_operators_audit_is_there_before_the_stack_is_asked(
    client, factors, clock, sessions, monkeypatch
):
    at_request: list[list[str]] = []

    def stack(request: httpx.Request) -> httpx.Response:
        at_request.append([r.action for r in _rows(sessions)])
        return httpx.Response(200, json={"ok": True, "data": _access(), "say": None})

    _world(sessions, monkeypatch, alpha=_managed(answer=stack))
    _signed_in(client, factors, clock)

    _post(client, "pin-set")

    assert at_request == [["customer.pin_set_asked"]]


def test_what_a_stack_says_about_a_device_is_shown_as_text(
    client, factors, clock, sessions, monkeypatch
):
    name = '<img src=x onerror=alert(1)>"'
    hostile = _managed(devices=[_device(device=name)], team_phone="<b>+49</b>")
    _world(sessions, monkeypatch, alpha=hostile)
    _signed_in(client, factors, clock)

    text = _post(client).text

    assert "<img" not in text and "&lt;img src=x onerror=alert(1)&gt;" in text
    assert "<b>+49</b>" not in text


def test_a_step_on_one_customer_never_reaches_another(
    client, factors, clock, sessions, monkeypatch
):
    seen: list[httpx.Request] = []
    _world(sessions, monkeypatch, alpha=_managed(seen))
    _signed_in(client, factors, clock)

    for action in ACTIONS:
        _post(client, action)

    assert {r.url.host for r in seen} == {"alpha-api"}
    assert {r.headers["authorization"] for r in seen} == {f"Bearer {TOKEN_A}"}


def test_a_signed_out_session_takes_no_step(
    client, factors, clock, sessions, monkeypatch
):
    seen: list[httpx.Request] = []
    _world(sessions, monkeypatch, alpha=_managed(seen))
    _signed_in(client, factors, clock)
    token = _csrf(client)
    client.post("/logout", data={"csrf_token": token})

    assert _post(client, "pin-show", csrf_token=token).status_code == 401
    assert seen == []


def test_the_page_has_no_script_and_no_secret_in_a_field(
    client, factors, clock, sessions, monkeypatch
):
    """The CSP of ops.<domain> forbids inline script, and a shown PIN is text:
    a field's value would be kept by the browser's form history."""
    _world(sessions, monkeypatch, alpha=_managed(pin=PIN, link=LINK))
    _signed_in(client, factors, clock)

    for action in ("pin-show", "setup-link"):
        text = _post(client, action).text
        assert not re.search(r"<script(?![^>]*\bsrc=)", text)
        assert " on" + "click=" not in text and "javascript:" not in text
        assert f'value="{PIN}"' not in text and f'value="{LINK}"' not in text
        assert 'autocomplete="off"' in text


def test_the_words_of_the_modes_cover_what_a_stack_offers():
    for mode in ("shadow", "overflow", "primary", "paused", "resume"):
        assert mode in web.MODES
