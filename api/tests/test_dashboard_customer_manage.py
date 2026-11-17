"""The write side of a customer's page in the operator dashboard (#571,
docs/23 §5 and §8): both PINs, devices, a setup link, the call mode, the
team number for today and the standard number (#811). Every step stands behind the step-up, the page's CSRF token, a
reason and both audits; no PIN and no link reaches an audit row, a log line or
an address; and the stack is a hostile source, as in the read tests. Against a
scratch operator database and a mock transport."""

import json
import logging
import re
from typing import Any

import httpx
import pytest
from sqlalchemy import select, update

from api.core import ops_reason
from api.dashboard import access, customers, passkeys
from api.dashboard import app as web
from api.dashboard.db import get_db
from api.dashboard.models import OperatorSession
from api.domain import operator
from api.domain.status.mode import CAUTION, PAUSED
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
# The version of the state a page shows; a step sends it back (#811).
SEEN, NEWER = "0123456789abcdef", "fedcba9876543210"
ACTIONS = sorted(customers.STEPS)
# A form each step takes.
GOOD: dict[str, dict[str, str]] = {
    "open": {},
    "pin-show": {"which": "team"},
    "pin-set": {"which": "admin", "pin": NEW, "gesehen": SEEN},
    "sign-out": {"device": TABLET, "gesehen": SEEN},
    "setup-link": {"gesehen": SEEN},
    "call-mode": {"mode": "primary", "gesehen": SEEN},
    "team-phone": {"number": NUMBER, "gesehen": SEEN},
    "team-phone-standard": {"number": NUMBER, "gesehen": SEEN},
}
# What reaches the stack for it: path and body.
SENT: dict[str, tuple[str, dict[str, Any] | None]] = {
    "open": ("/v1/ops/access", None),
    "pin-show": ("/v1/ops/access/pin/show", {"which": "team"}),
    "pin-set": ("/v1/ops/access/pin", {"which": "admin", "pin": NEW, "seen": SEEN}),
    "sign-out": ("/v1/ops/access/sign-out", {"device": TABLET, "seen": SEEN}),
    "setup-link": ("/v1/ops/access/setup-link", {"seen": SEEN}),
    "call-mode": ("/v1/ops/call-mode", {"mode": "primary", "seen": SEEN}),
    "team-phone": ("/v1/ops/team-phone", {"number": NUMBER, "seen": SEEN}),
    "team-phone-standard": (
        "/v1/ops/team-phone/standard",
        {"number": NUMBER, "seen": SEEN},
    ),
}
AUDIT_NAMES = {
    "open": "access",
    "pin-show": "pin_show",
    "pin-set": "pin_set",
    "sign-out": "sign_out",
    "setup-link": "setup_link",
    "call-mode": "call_mode",
    "team-phone": "team_phone",
    "team-phone-standard": "team_phone_standard",
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
        "team_phone_standard": "+4972215550100",
        "team_phone_until": None,
        "team_phones": ["+4972215550100", NUMBER],
        "version": SEEN,
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


def _once(key: str = "alpha") -> str | None:
    """The token a manage page of this customer would hold for the signed-in
    session, made without asking a stack. None: nobody is signed in."""
    held = web.app.dependency_overrides[get_db]()
    db = next(held)
    try:
        session_id = db.scalar(select(OperatorSession.id))
        if session_id is None:
            return None
        holder = passkeys.for_session(session_id)
        return passkeys.issue(db, holder, f"manage:{key}", web.now()).hex()
    finally:
        held.close()


def _post(client, action: str = "open", key: str = "alpha", headers=None, **fields):
    step = customers.step(action, GOOD.get(action, {}))
    data: dict[str, Any] = (
        {
            "csrf_token": _csrf(client),
            "back": f"/customers/{key}",
            "reason": "restaurant_report",
            "reference": "812",
            # A form that changes something carries its page's token.
            "once": _once(key)
            if step and step.changes and "once" not in fields
            else None,
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
    assert step.body == {"all": True, "seen": SEEN}
    assert step.detail == {"device": "all"}


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
    ("team-phone-standard", {"number": ""}),
    ("team-phone-standard", {"number": "0721 5551234"}),
    # A step that changes something without the version of its page (#811).
    ("sign-out", {"gesehen": "0123456789ABCDEF"}),
    ("setup-link", {"gesehen": SEEN + "0"}),
    ("call-mode", {"gesehen": "<b>"}),
    ("team-phone", {"gesehen": SEEN + "\n"}),
    ("team-phone-standard", {"gesehen": "x" * 16}),
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

    found = _result(_managed(seen, link=LINK), action)

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
    (_with(version="<b>"), customers.NOT_AN_ANSWER),
    (_error("conflict", 412), customers.STALE),
    (_says(_access("beta")), customers.OTHER_CUSTOMER),
    (_says(_access(), lambda d: d["tenant"].update(key=None)), customers.NO_KEY),
    (_error("conflict", 409), customers.WRONG_STACK),
    (_error("invalid_input"), customers.NOT_TAKEN),
    (_error("invalid_input", 400), customers.NOT_TAKEN),
    (_error("invalid_input", 422), customers.REFUSED_BY_RULE),
    (_with(team_phone="Ruf 0900 an"), customers.NOT_AN_ANSWER),
    (_with(team_phone_standard="<b>07221</b>"), customers.NOT_AN_ANSWER),
    (_with(team_phone_until="9999-12-31T23:59:59+00:00"), customers.NOT_AN_ANSWER),
    (_error("invalid_input", 401), customers.REFUSED),
    (_error("not_found", 404), customers.NOT_KNOWN),
    (_error("not_found", message=ops_reason.NO_RESTAURANT), customers.NO_RESTAURANT),
    (_error("not_found"), customers.NO_SETTINGS),
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


def _took(sessions, action: str, outcome: str = "done") -> tuple[Any, Any]:
    """The two rows of one step in the operator's audit."""
    name = AUDIT_NAMES[action]
    asked, after = _rows(sessions)[-2:]
    assert asked.action == f"customer.{name}_asked"
    assert after.action == f"customer.{name}_{outcome}"
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
    # And the version of what it shows: a step from it acts on that (#811).
    assert text.count(f'name="gesehen" value="{SEEN}"') == forms - 1
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
    assert json.loads(request.content) == {"which": "admin", "pin": NEW, "seen": SEEN}


@pytest.mark.parametrize(
    ("device", "body", "ended", "words"),
    [
        (TABLET, {"device": TABLET, "seen": SEEN}, 1, "Signed out: 1 device."),
        (TABLET, {"device": TABLET, "seen": SEEN}, 0, "No device was signed out"),
        ("all", {"all": True, "seen": SEEN}, 3, "Signed out: 3 devices."),
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
    # A link may have been made and the older one voided: nobody knows.
    _took(sessions, "setup-link", "unclear")


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


def test_the_standard_number_is_read_from_the_answer(
    client, factors, clock, sessions, monkeypatch
):
    """#811 question 2: the standard number, set behind the step-up and a
    reason, as every step; the stack pushes to the owner."""
    seen: list[httpx.Request] = []
    _world(sessions, monkeypatch, alpha=_managed(seen, team_phone_standard=NUMBER))
    _signed_in(client, factors, clock)

    answer = _post(client, "team-phone-standard")

    assert f"The standard team number is now {NUMBER}" in answer.text
    (request,) = seen
    assert request.url.path == "/v1/ops/team-phone/standard"
    asked, _ = _took(sessions, "team-phone-standard")
    assert NUMBER not in json.dumps(asked.detail)


def test_the_page_offers_the_standard_number_apart_from_todays(
    client, factors, clock, sessions, monkeypatch
):
    _world(sessions, monkeypatch, alpha=_managed())
    _signed_in(client, factors, clock)

    text = _post(client).text

    assert 'action="/customers/alpha/manage/team-phone-standard"' in text
    assert "Make this the standard number" in text
    assert "Use this number today" in text


# --- a page the restaurant changed since (#811) -----------------------------------


def test_a_step_from_an_old_page_does_nothing_and_the_page_reads_the_state_again(
    client, factors, clock, sessions, monkeypatch
):
    """The owner stopped the agent after the page was read: the stack refuses
    the step, and the page shows what holds now, with its new version."""
    seen: list[httpx.Request] = []
    stale = _error("conflict", 412)
    fresh = _says(_access(call_mode="paused", call_modes=["resume"], version=NEWER))

    def stack(request: httpx.Request) -> httpx.Response:
        return (stale if request.method == "POST" else fresh)(request)

    _world(sessions, monkeypatch, alpha=_managed(seen, answer=stack))
    _signed_in(client, factors, clock)

    answer = _post(client, "call-mode", mode="resume")

    assert answer.status_code == 200
    text = answer.text
    assert "Nothing was done" in text and "changed since" in text
    assert "may have been made" not in text
    assert [(r.method, r.url.path) for r in seen] == [
        ("POST", "/v1/ops/call-mode"),
        ("GET", "/v1/ops/access"),
    ]
    assert f'name="gesehen" value="{NEWER}"' in text
    assert '<option value="resume">' in text and '<option value="primary"' not in text
    actions = [r.action for r in _rows(sessions)][-4:]
    assert actions == [
        "customer.call_mode_asked",
        "customer.call_mode_not_done",
        "customer.access_asked",
        "customer.access_done",
    ]
    assert _rows(sessions)[-3].detail["problem"] == customers.STALE
    # The new page may take the next step: it holds a new form token.
    assert 'name="once" value=""' not in text


def test_an_old_page_whose_new_read_fails_says_both(
    client, factors, clock, sessions, monkeypatch
):
    def stack(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return _error("conflict", 412)(request)
        raise httpx.ConnectError("down")

    _world(sessions, monkeypatch, alpha=_managed(answer=stack))
    _signed_in(client, factors, clock)

    text = _post(client, "team-phone").text

    assert "Nothing was done" in text and "changed since" in text
    assert f"Read again: {customers.UNREACHABLE}." in text
    assert 'action="/customers/alpha/manage/open"' in text
    assert 'action="/customers/alpha/manage/team-phone"' not in text


# --- a stack that cannot or will not ---------------------------------------------

DEFINITE = [
    (_error("invalid_input", 401), customers.REFUSED),
    (_error("not_found", 404), customers.NOT_KNOWN),
    (_error("conflict", 409), customers.WRONG_STACK),
    (_error("not_found", message=ops_reason.NO_RESTAURANT), customers.NO_RESTAURANT),
    (_error("not_found"), customers.NO_SETTINGS),
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
    # The audit says the same as the page: not done, or nobody knows.
    _, after = _took(sessions, action, "unclear" if unsure else "not_done")
    assert after.detail["problem"] == problem
    assert 'action="/customers/alpha/manage/open"' in answer.text
    assert "Restaurant beta" not in answer.text


@pytest.mark.parametrize(
    ("action", "words"),
    [
        ("pin-set", "refused this PIN"),
        ("call-mode", "refused this mode"),
        ("team-phone", "refused this number"),
        ("team-phone-standard", "refused this number"),
        ("sign-out", customers.REFUSED_BY_RULE),
    ],
)
def test_a_refusal_of_the_stack_names_what_it_can_be(
    client, factors, clock, sessions, monkeypatch, action, words
):
    _world(sessions, monkeypatch, alpha=_managed(answer=_error("invalid_input", 422)))
    _signed_in(client, factors, clock)

    answer = _post(client, action)

    assert words in answer.text and "may have been made" not in answer.text
    _took(sessions, action, "not_done")


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
    hostile = _managed(devices=[_device(device=name)])
    _world(sessions, monkeypatch, alpha=hostile)
    _signed_in(client, factors, clock)

    text = _post(client).text

    assert "<img" not in text and "&lt;img src=x onerror=alert(1)&gt;" in text


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
    for mode in (*CAUTION, PAUSED, operator.RESUME):
        assert mode in web.MODES


def test_a_request_the_stack_does_not_take_is_not_called_a_rule(
    client, factors, clock, sessions, monkeypatch
):
    """200 or 400 is the stack refusing the request itself (the body's form,
    a header), as after a deploy of one side only; 422 is one of its rules.
    The page must not explain the first with the second's sentence."""
    _signed_in(client, factors, clock)
    base._two_customers(sessions)
    for status in (200, 400):
        base._stacks(
            monkeypatch,
            {"alpha-api": _managed(answer=_error("invalid_input", status))},
        )
        for action in ("pin-set", "call-mode", "team-phone"):
            text = _post(client, action).text
            assert customers.NOT_TAKEN in text, (action, status)
            assert "refused this" not in text and "may have been made" not in text


# --- a step is taken once ---------------------------------------------------------

CHANGING = ["pin-set", "sign-out", "setup-link", "call-mode", "team-phone"]


def _token(text: str) -> str:
    found = set(re.findall(r'name="once" value="([0-9a-f]{64})"', text))
    assert len(found) == 1, found
    return found.pop()


@pytest.mark.parametrize("action", CHANGING)
def test_a_form_that_changes_something_is_taken_once(
    client, factors, clock, sessions, monkeypatch, action
):
    """A reload of the answer page, or the Back button, sends the same form
    again: the second setup link would void the one just passed on."""
    seen: list[httpx.Request] = []
    _world(sessions, monkeypatch, alpha=_managed(seen, link=LINK, signed_out=1))
    _signed_in(client, factors, clock)
    token = _token(_post(client).text)

    first = _post(client, action, once=token)
    # The answer page holds the next token: the operator goes on from there.
    onward = _post(client, action, once=_token(first.text))
    rows = len(_rows(sessions))
    again = _post(client, action, once=token)

    assert first.status_code == onward.status_code == 200 and len(seen) == 3
    assert again.status_code == 409 and "sent before" in again.text
    assert len(seen) == 3 and len(_rows(sessions)) == rows
    assert 'action="/customers/alpha/manage/open"' in again.text


@pytest.mark.parametrize("action", CHANGING)
@pytest.mark.parametrize("token", [None, "", "0" * 64, "x" * 64, "ü" * 8])
def test_a_changing_form_without_the_pages_token_sends_nothing(
    client, factors, clock, sessions, monkeypatch, action, token
):
    seen: list[httpx.Request] = []
    _world(sessions, monkeypatch, alpha=_managed(seen))
    _signed_in(client, factors, clock)
    _post(client)
    seen.clear()
    rows = len(_rows(sessions))

    answer = _post(client, action, once=token)

    assert answer.status_code == 409 and "Nothing was sent" in answer.text
    assert seen == [] and len(_rows(sessions)) == rows


def test_one_customers_token_opens_no_step_on_another(
    client, factors, clock, sessions, monkeypatch
):
    seen: list[httpx.Request] = []
    _world(sessions, monkeypatch, alpha=_managed(seen))
    _signed_in(client, factors, clock)

    assert _post(client, "call-mode", once=_once("beta")).status_code == 409
    assert seen == []


def test_a_token_is_worth_five_minutes(client, factors, clock, sessions, monkeypatch):
    seen: list[httpx.Request] = []
    _world(sessions, monkeypatch, alpha=_managed(seen))
    _signed_in(client, factors, clock)
    token = _once()
    clock.at += passkeys.CHALLENGE_LIFE
    # The second factor is stale by then as well: confirm it, the token stays old.
    with sessions() as db:
        db.execute(update(OperatorSession).values(stepped_up_at=clock.at))
        db.commit()

    answer = _post(client, "call-mode", once=token)

    assert answer.status_code == 409 and seen == []


@pytest.mark.parametrize("action", ["open", "pin-show"])
def test_a_read_needs_no_token_and_can_be_repeated(
    client, factors, clock, sessions, monkeypatch, action
):
    _world(sessions, monkeypatch, alpha=_managed(pin=PIN))
    _signed_in(client, factors, clock)

    for _ in range(2):
        assert _post(client, action, once=None).status_code == 200


# --- the lists start at what holds -------------------------------------------------


def test_the_lists_start_at_what_holds_so_a_stray_tap_changes_nothing(
    client, factors, clock, sessions, monkeypatch
):
    """The first entry of a list is what a button sends when nobody chose:
    "shadow" would switch a restaurant's agent off."""
    primary = _managed(
        call_mode="primary",
        team_phone=NUMBER,
        team_phone_until="2026-11-11T04:00:00+00:00",
    )
    _world(sessions, monkeypatch, alpha=primary)
    _signed_in(client, factors, clock)

    text = _post(client).text

    assert '<option value="primary" selected>' in text
    assert f'<option value="{NUMBER}" selected>' in text
    # The standard number's list starts at the standard (#811).
    assert '<option value="+4972215550100" selected>' in text
    assert text.count(" selected>") == 3
    # Today's number is said to be today's, with its end and the standard.
    assert "until 11.11.2026 05:00" in text and "+4972215550100 holds again" in text


def test_a_mode_or_number_that_is_not_in_its_list_starts_at_no_choice(
    client, factors, clock, sessions, monkeypatch
):
    """During the emergency stop the one entry is "switch on again": it must
    not be what an untouched list sends."""
    paused = _managed(
        call_mode="paused", call_modes=["resume"], team_phone="0721 5550000"
    )
    _world(sessions, monkeypatch, alpha=paused)
    _signed_in(client, factors, clock)

    text = _post(client).text

    assert '<option value="" selected>Leave it as it is</option>' in text
    assert '<option value="" selected>Choose a number</option>' in text
    assert '<option value="resume">' in text
    # And the empty choice is no step: nothing is sent for it.
    seen: list[httpx.Request] = []
    _world_again = _managed(seen)
    monkeypatch.setattr(
        customers,
        "_client",
        base._factory(
            base._by_host({"alpha-api": _world_again, "beta-api": _world_again})
        ),
    )
    assert _post(client, "call-mode", mode="").status_code == 400 and seen == []


def test_a_stack_without_settings_offers_no_mode_and_no_number(
    client, factors, clock, sessions, monkeypatch
):
    bare = _managed(
        call_mode=None,
        call_modes=[],
        team_phone=None,
        team_phone_standard=None,
        team_phones=[],
    )
    _world(sessions, monkeypatch, alpha=bare)
    _signed_in(client, factors, clock)

    text = _post(client).text

    assert "/manage/call-mode" not in text and "/manage/team-phone" not in text
    assert "no mode to set" in text and "lists no number" in text


# --- what the page says ------------------------------------------------------------


def test_a_link_without_a_host_is_said_to_need_one(
    client, factors, clock, sessions, monkeypatch
):
    _world(sessions, monkeypatch, alpha=_managed(link=f"/gui/einrichten#{TOKEN}"))
    _signed_in(client, factors, clock)

    text = _post(client, "setup-link").text

    assert "does not know its public address" in text and text.count(TOKEN) == 1


def test_a_host_as_a_stack_may_write_it_is_a_link():
    for host in ("Agent.Example.com", "agent.example.com:8443", "a.de"):
        found = _result(
            _with(link=f"https://{host}/gui/einrichten#{TOKEN}"), "setup-link"
        )
        assert isinstance(found, customers.StackAccess), host


def test_an_answer_without_a_number_is_not_shown_as_none(
    client, factors, clock, sessions, monkeypatch
):
    _world(sessions, monkeypatch, alpha=_managed(team_phone=None))
    _signed_in(client, factors, clock)

    text = _post(client, "team-phone").text

    assert "go to no number today" in text and "None" not in text


def test_the_stack_is_named_in_front_of_a_problem(
    client, factors, clock, sessions, monkeypatch
):
    down = _managed(answer=_raising(httpx.ConnectError("down")))
    _world(sessions, monkeypatch, alpha=down)
    _signed_in(client, factors, clock)

    assert f"The stack: {customers.UNREACHABLE}." in _post(client).text


# --- both sides, one number --------------------------------------------------------


def test_both_sides_cap_the_devices_alike():
    """A stack that sends more than the dashboard reads is "cannot be read"."""
    assert customers.MAX_DEVICES == operator.MAX_DEVICES
    assert customers.MAX_NUMBERS == operator.MAX_NUMBERS


def test_no_account_can_be_named_like_the_command_line(sessions):
    """`operator:cli` is the command line's actor in a stack's audit log."""
    with sessions() as db, pytest.raises(ValueError, match="command line"):
        access.create_account(db, "CLI ", base.PASSWORD)


def test_an_older_tabs_form_does_not_spend_the_newer_tabs_token(
    client, factors, clock, sessions, monkeypatch
):
    """Two pages of one customer: the second one's token replaces the first.
    The first page's form sends nothing, and the second page still works."""
    seen: list[httpx.Request] = []
    _world(sessions, monkeypatch, alpha=_managed(seen))
    _signed_in(client, factors, clock)
    older, newer = _once(), _once()

    assert _post(client, "call-mode", once=older).status_code == 409
    assert seen == []
    assert _post(client, "call-mode", once=newer).status_code == 200
    assert len(seen) == 1


def test_a_step_without_a_version_is_sent_as_before():
    """A page of a stack older than #811 has no version: its steps go out
    without one, and a newer stack refuses such a step itself."""
    step = _step("call-mode", gesehen="")
    assert step is not None
    assert step.body == {"mode": "primary"}


def test_an_older_stack_is_managed_and_the_page_says_it_checks_nothing(
    client, factors, clock, sessions, monkeypatch
):
    """Review of #811: a required version left the operator without the
    emergency stop for every stack not updated yet."""
    seen: list[httpx.Request] = []
    older = _managed(seen, answer=_says(_access(), lambda d: d.pop("version")))
    _world(sessions, monkeypatch, alpha=older)
    _signed_in(client, factors, clock)

    page = _post(client).text
    assert "This stack is older" in page
    assert 'name="gesehen" value=""' in page

    answer = _post(client, "call-mode", mode="paused", gesehen="")

    assert answer.status_code == 200
    assert json.loads(seen[-1].content) == {"mode": "paused"}
    _took(sessions, "call-mode")
