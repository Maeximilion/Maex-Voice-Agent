"""The stack's side of the operator's write steps (#571, docs/23 §8): the access
read and the six steps behind it (both PINs shown and set, devices signed out,
a setup link, the call mode, the team number). Each needs the ops token, a
reason, the operator's account and the customer's key, is in the audit log
before it happens, and never puts a PIN or a link into a log or an audit row.
"""

import json
import logging
import re
import uuid
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import delete, func, select, update

from api.config import settings
from api.core import ops_reason
from api.domain import operator
from api.domain.access.sessions import DEVICE_CHARS, locked_account, open_session
from api.domain.access.setup import issue_link, set_team_pin, use_link
from api.domain.status.config import pause_ai, resume_ai
from api.domain.status.staff_changes import change_team_phone
from api.models import (
    AuditLog,
    GuiAccount,
    GuiSession,
    GuiSetupLink,
    OutboxEvent,
    ServiceConfig,
    StaffChange,
    Tenant,
)
from api.tests import test_ops_status as base
from api.tests.test_ops_status import AUTH, NOW

# Fixtures of the status tests, taken by assignment (see test_ops_detail.py).
bridge_settings = base.bridge_settings
http = base.http
session = base.session
tenant = base.tenant

KEY = "testbetrieb"
ASK = AUTH | {
    "X-Ops-Reason": "restaurant_report #812",
    "X-Ops-Actor": "maxi",
    "X-Ops-Customer": KEY,
}
ACTOR = "operator:maxi"
OUTCOME = ".outcome"
PASSWORD = "lange-geheime-phrase"
TEAM, ADMIN, NEW = "471123", "080815", "573921"
NUMBER = "+497215551234"
ACCESS = "/v1/ops/access"
PIN_SHOW = "/v1/ops/access/pin/show"
PIN_SET = "/v1/ops/access/pin"
SIGN_OUT = "/v1/ops/access/sign-out"
LINK = "/v1/ops/access/setup-link"
CALL_MODE = "/v1/ops/call-mode"
TEAM_PHONE = "/v1/ops/team-phone"
TEAM_PHONE_STANDARD = "/v1/ops/team-phone/standard"
# The steps that change the restaurant: each sends the version of the page it
# came from (`seen`, #811).
CHANGING = {PIN_SET, SIGN_OUT, LINK, CALL_MODE, TEAM_PHONE, TEAM_PHONE_STANDARD}
STEPS: list[tuple[str, str, dict[str, Any] | None]] = [
    ("GET", ACCESS, None),
    ("POST", PIN_SHOW, {"which": "team"}),
    ("POST", PIN_SET, {"which": "team", "pin": NEW}),
    ("POST", SIGN_OUT, {"all": True}),
    ("POST", LINK, None),
    ("POST", CALL_MODE, {"mode": "primary"}),
    ("POST", TEAM_PHONE, {"number": NUMBER}),
    ("POST", TEAM_PHONE_STANDARD, {"number": NUMBER}),
]
# The real read: a test may replace `operator.access` to break a step.
_FRESH = operator.access
_PAGE: dict[str, Any] = {}


@pytest.fixture
def stack(session, tenant, monkeypatch):
    """The seed's restaurant as provisioning leaves it: with its customer key
    and a second number the telephone system dials."""
    tenant.customer_key = KEY
    session.commit()
    monkeypatch.setattr(settings, "transfer_targets", f"{NUMBER}, 0721 5550000")
    monkeypatch.setattr(settings, "agent_domain", "")
    return tenant


@pytest.fixture
def login(session, stack) -> GuiAccount:
    """The restaurant set its password and both PINs; two devices signed in."""
    code, _ = issue_link(session, stack.id, "operator:test", NOW)
    use_link(session, stack.id, code, PASSWORD, TEAM, "Tablet", NOW, admin_pin=ADMIN)
    account = locked_account(session, stack.id)
    assert account is not None
    open_session(session, account, "Kasse", NOW)
    session.commit()
    return account


@pytest.fixture(autouse=True)
def _page(session):
    """The session `_ask` reads the page's version with."""
    _PAGE["session"] = session
    yield
    _PAGE.clear()


def _version() -> str:
    """The version a page read now shows, as the dashboard sends it back."""
    session = _PAGE["session"]
    session.expire_all()
    tenant = session.scalars(select(Tenant)).first()
    return _FRESH(session, tenant, NOW).version if tenant else "0" * 16


def _ask(http, method: str, path: str, body=None, **headers: str | None):
    """A request as the dashboard sends it: a step that changes something
    carries the version of a page read just before, unless the test says."""
    sent = {k: v for k, v in (ASK | headers).items() if v is not None}
    if path in CHANGING and (body is None or isinstance(body, dict)):
        body = {"seen": _version()} | (body or {})
    return http.request(method, path, headers=sent, json=body)


def _data(answer) -> dict[str, Any]:
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["ok"] is True, body
    return body["data"]


def _refused(answer, code: str = "invalid_input") -> None:
    body = answer.json()
    assert body["ok"] is False and body["error"]["code"] == code, body


def _count(session, model) -> int:
    return session.scalar(select(func.count()).select_from(model)) or 0


def _state(session, tenant) -> tuple:
    """Everything a step could change, and the audit log's length."""
    session.expire_all()
    account = session.scalars(select(GuiAccount)).first()
    config = session.scalars(select(ServiceConfig)).one()
    return (
        (account.team_pin, account.admin_pin) if account else None,
        _count(session, GuiSession),
        _count(session, GuiSetupLink),
        (config.call_mode, config.team_phone, config.team_phone_today),
        _count(session, AuditLog),
        _count(session, OutboxEvent),
    )


def _audit(session, prefix: str = "", outcomes: bool = False) -> list[AuditLog]:
    """The audit rows in order; a step's outcome row only when asked for."""
    session.expire_all()
    rows = session.scalars(select(AuditLog).order_by(AuditLog.id)).all()
    return [
        row
        for row in rows
        if row.action.startswith(prefix)
        and (outcomes or not row.action.endswith(OUTCOME))
    ]


def _payload(row: AuditLog) -> dict[str, Any]:
    assert row.payload is not None
    return row.payload


def _no_secret(session, caplog, *secrets: str) -> None:
    written = json.dumps([row.payload for row in _audit(session)])
    for secret in secrets:
        assert secret not in written
        assert secret not in caplog.text


# --- who may ask --------------------------------------------------------------

REFUSALS = [
    ({"Authorization": None}, 401),
    ({"Authorization": "Bearer " + "e" * 64}, 401),
    ({"X-Ops-Reason": None}, 400),
    ({"X-Ops-Reason": "curiosity #1"}, 400),
    ({"X-Ops-Reason": "restaurant_report Mueller"}, 400),
    ({"X-Ops-Actor": None}, 400),
    ({"X-Ops-Actor": "Erika Muster"}, 400),
    ({"X-Ops-Customer": None}, 409),
    ({"X-Ops-Customer": "pizzeria-sued"}, 409),
    ({"X-Ops-Customer": ""}, 409),
]


@pytest.mark.parametrize(("method", "path", "body"), STEPS)
@pytest.mark.parametrize(("headers", "status"), REFUSALS)
def test_a_step_that_may_not_be_asked_changes_and_records_nothing(
    http, session, login, stack, method, path, body, headers, status
):
    before = _state(session, stack)

    answer = _ask(http, method, path, body, **headers)

    assert answer.status_code == status
    assert answer.json()["ok"] is False
    assert _state(session, stack) == before


@pytest.mark.parametrize(("method", "path", "body"), STEPS)
def test_a_restaurant_without_a_key_takes_no_step(
    http, session, login, stack, method, path, body
):
    """Nothing ties such a stack to the customer the dashboard means."""
    stack.customer_key = None
    session.commit()
    before = _state(session, stack)

    for key in (KEY, "", None):
        answer = _ask(http, method, path, body, **{"X-Ops-Customer": key})
        assert answer.status_code == 409
        _refused(answer, "conflict")
    assert _state(session, stack) == before


@pytest.mark.parametrize(("method", "path", "body"), STEPS)
def test_every_step_is_in_the_audit_log_before_it_happens(
    http, session, login, stack, monkeypatch, method, path, body
):
    """The row with the reason is committed first: a step that then fails is
    still a step that was asked for."""
    seen: list[list[str]] = []

    def failing(*args: object, **kwargs: object) -> None:
        seen.append([row.action for row in _audit(session, "ops.")])
        raise RuntimeError("after the audit row")

    for name in (
        "access",
        "shown_pin",
        "set_team_pin",
        "end_sessions",
        "issue_link",
        "set_mode",
        "change_team_phone",
        "set_standard",
    ):
        monkeypatch.setattr(operator, name, failing)

    with pytest.raises(RuntimeError):
        _ask(http, method, path, body)

    (actions,) = seen
    assert len(actions) == 1 and actions[0].startswith("ops.")
    (row,) = _audit(session, "ops.")
    assert row.actor == ACTOR and row.tenant_id == stack.id
    assert _payload(row)["reason"] == "restaurant_report"
    assert _payload(row)["reference"] == "#812"


# --- the access read ----------------------------------------------------------


def test_a_stack_without_a_login_says_so(http, session, stack):
    data = _data(_ask(http, "GET", ACCESS))

    assert data["tenant"] == {"key": KEY, "name": "Testbetrieb"}
    assert (data["login"], data["team_pin_set"], data["admin_pin_set"]) == (
        False,
        False,
        False,
    )
    assert data["devices"] == []
    assert data["call_mode"] == "shadow"
    assert data["call_modes"] == ["shadow", "overflow", "primary", "paused"]
    config = session.scalars(select(ServiceConfig)).one()
    assert data["team_phone"] == data["team_phone_standard"] == config.team_phone
    assert data["team_phone_until"] is None
    # Only numbers written in E.164 can be chosen (`sheet_numbers`).
    assert NUMBER in data["team_phones"] and "0721 5550000" not in data["team_phones"]


def test_the_access_read_lists_devices_and_holds_no_secret(http, session, login, stack):
    answer = _ask(http, "GET", ACCESS)
    data = _data(answer)

    assert (data["login"], data["team_pin_set"], data["admin_pin_set"]) == (
        True,
        True,
        True,
    )
    assert sorted(d["device"] for d in data["devices"]) == ["Kasse", "Tablet"]
    rows = {str(r.id): r for r in session.scalars(select(GuiSession))}
    for device in data["devices"]:
        assert set(device) == {"id", "device", "created_at", "last_seen_at"}
        assert rows[device["id"]].token_hash not in answer.text
    for secret in (TEAM, ADMIN, login.password_hash):
        assert secret not in answer.text
    (row,) = _audit(session, "ops.")
    assert (row.action, row.actor) == ("ops.access_read", ACTOR)


def test_only_the_newest_devices_are_listed(http, session, login, stack):
    """A restaurant has a handful of devices, and the dashboard reads no more
    than this many: one stack's oddity must not cost its page."""
    for n in range(operator.MAX_DEVICES + 5):
        open_session(session, login, f"{n} " + "x" * 500, NOW + timedelta(seconds=n))
    session.commit()

    data = _data(_ask(http, "GET", ACCESS))

    assert len(data["devices"]) == operator.MAX_DEVICES
    # A browser's own name is cut when the device signs in (`DEVICE_CHARS`).
    assert max(len(d["device"]) for d in data["devices"]) == DEVICE_CHARS
    assert data["devices"][0]["device"].startswith(f"{operator.MAX_DEVICES + 4} ")


# --- the PINs -----------------------------------------------------------------


@pytest.mark.parametrize(("which", "pin"), [("team", TEAM), ("admin", ADMIN)])
def test_a_pin_is_shown_and_the_look_is_recorded_without_it(
    http, session, login, stack, caplog, which, pin
):
    caplog.set_level(logging.DEBUG)

    data = _data(_ask(http, "POST", PIN_SHOW, {"which": which}))

    assert data["pin"] == pin
    assert [(r.action, r.actor) for r in _audit(session)[-2:]] == [
        ("ops.pin_show", ACTOR),
        (f"gui.{which}_pin_shown", ACTOR),
    ]
    assert _payload(_audit(session, "ops.")[-1])["which"] == which
    _no_secret(session, caplog, TEAM, ADMIN)


def test_a_pin_that_is_not_set_is_shown_as_none(http, session, stack):
    data = _data(_ask(http, "POST", PIN_SHOW, {"which": "team"}))

    assert data["pin"] is None and data["team_pin_set"] is False


@pytest.mark.parametrize(
    ("which", "column", "set_action"),
    [
        ("team", "team_pin", "gui.team_pin_set"),
        ("admin", "admin_pin", "gui.admin_pin_set"),
    ],
)
def test_a_pin_is_set_and_no_audit_row_or_log_line_holds_it(
    http, session, login, stack, caplog, which, column, set_action
):
    caplog.set_level(logging.DEBUG)

    answer = _ask(http, "POST", PIN_SET, {"which": which, "pin": NEW})

    data = _data(answer)
    assert NEW not in answer.text and "pin" not in data
    session.expire_all()
    assert getattr(session.scalars(select(GuiAccount)).one(), column) == NEW
    assert [(r.action, r.actor) for r in _audit(session)[-2:]] == [
        ("ops.pin_set", ACTOR),
        (set_action, ACTOR),
    ]
    # Devices stay signed in when a PIN changes (docs/23 §6).
    assert len(data["devices"]) == 2
    _no_secret(session, caplog, NEW)


@pytest.mark.parametrize(
    "body",
    [
        {"which": "team", "pin": ADMIN},
        {"which": "admin", "pin": TEAM},
        {"which": "team", "pin": "123"},
        {"which": "team", "pin": "1234567"},
        {"which": "team", "pin": "12a4"},
        {"which": "team", "pin": "١٢٣٤"},
        {"which": "owner", "pin": NEW},
        {"which": "team"},
        {"which": "team", "pin": 5739},
        {},
    ],
)
def test_a_pin_the_rules_refuse_changes_nothing_and_is_not_repeated(
    http, session, login, stack, body
):
    answer = _ask(http, "POST", PIN_SET, body)

    _refused(answer)
    assert str(body.get("pin", NEW)) not in answer.text or "pin" not in body
    session.expire_all()
    account = session.scalars(select(GuiAccount)).one()
    assert (account.team_pin, account.admin_pin) == (TEAM, ADMIN)


def test_a_pin_cannot_be_set_before_the_restaurant_has_a_login(http, session, stack):
    _refused(_ask(http, "POST", PIN_SET, {"which": "team", "pin": NEW}))
    assert _count(session, GuiAccount) == 0


# --- devices ------------------------------------------------------------------


def test_one_device_is_signed_out_and_the_other_stays(http, session, login, stack):
    tablet = str(
        session.scalars(
            select(GuiSession.id).where(GuiSession.device == "Tablet")
        ).one()
    )

    data = _data(_ask(http, "POST", SIGN_OUT, {"device": tablet}))

    assert data["signed_out"] == 1
    assert [d["device"] for d in data["devices"]] == ["Kasse"]
    ended = _audit(session, "gui.session_ended")[-1]
    assert ended.actor == ACTOR and _payload(ended)["reason"] == "operator"
    assert _payload(_audit(session, "ops.")[-1])["device"] == tablet
    # Once more: it is gone, and that is no error.
    again = _data(_ask(http, "POST", SIGN_OUT, {"device": tablet}))
    assert again["signed_out"] == 0 and len(again["devices"]) == 1


def test_all_devices_are_signed_out(http, session, login, stack):
    data = _data(_ask(http, "POST", SIGN_OUT, {"all": True}))

    assert data["signed_out"] == 2 and data["devices"] == []
    assert _payload(_audit(session, "ops.")[-1])["device"] == "all"


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"all": False},
        {"device": None},
        {"device": "tablet"},
        {"device": str(uuid.uuid4()), "all": True},
        {"all": "yes please", "device": None},
    ],
)
def test_a_sign_out_that_does_not_say_whom_signs_out_nobody(
    http, session, login, stack, body
):
    """A missing field must never mean "all of them"."""
    _refused(_ask(http, "POST", SIGN_OUT, body))
    assert _count(session, GuiSession) == 2


# --- the setup link -----------------------------------------------------------


def test_a_setup_link_is_answered_once_and_stored_nowhere_readable(
    http, session, login, stack, caplog, monkeypatch
):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(settings, "agent_domain", "agent.example.com")

    data = _data(_ask(http, "POST", LINK))

    start = "https://agent.example.com/gui/einrichten#"
    assert data["link"].startswith(start)
    token = data["link"].removeprefix(start)
    assert data["link_expires_at"] == (NOW + timedelta(hours=24)).isoformat()
    assert [(r.action, r.actor) for r in _audit(session)[-2:]] == [
        ("ops.setup_link", ACTOR),
        ("gui.setup_link_issued", ACTOR),
    ]
    _no_secret(session, caplog, token)
    # It is the link the restaurant can use: new password, every device out.
    use_link(session, stack.id, token, "noch-eine-lange-phrase", TEAM, "Neu", NOW)
    assert [s.device for s in session.scalars(select(GuiSession))] == ["Neu"]


def test_a_second_link_replaces_the_first(http, session, stack):
    first = _data(_ask(http, "POST", LINK))
    second = _data(_ask(http, "POST", LINK))

    # No AGENT_DOMAIN: the path alone, as the command line prints it.
    assert first["link"].startswith("/gui/einrichten#")
    # A link is no login yet: that begins when the restaurant used it.
    assert first["link"] != second["link"] and second["login"] is False
    assert _count(session, GuiSetupLink) == 1


# --- the call mode ------------------------------------------------------------


def _mode(session) -> str:
    session.expire_all()
    return session.scalars(select(ServiceConfig)).one().call_mode


def test_the_call_mode_is_set_with_the_operator_as_actor_and_a_push(
    http, session, stack
):
    data = _data(_ask(http, "POST", CALL_MODE, {"mode": "primary"}))

    assert data["call_mode"] == "primary" and _mode(session) == "primary"
    changed = _audit(session, "service_config.mode_changed")[-1]
    assert changed.actor == ACTOR
    assert changed.payload == {"from": "shadow", "to": "primary"}
    assert _payload(_audit(session, "ops.")[-1])["mode"] == "primary"
    (event,) = session.scalars(select(OutboxEvent)).all()
    assert event.event_type == "call_mode.changed"
    assert event.payload == {"from": "shadow", "to": "primary"}


def test_the_operator_pauses_the_agent_and_switches_it_on_again(http, session, stack):
    _data(_ask(http, "POST", CALL_MODE, {"mode": "overflow"}))

    paused = _data(_ask(http, "POST", CALL_MODE, {"mode": "paused"}))
    assert paused["call_mode"] == "paused" and paused["call_modes"] == ["resume"]
    # Out of the pause only by switching the agent on, as on the tablet.
    _refused(_ask(http, "POST", CALL_MODE, {"mode": "primary"}))
    assert _mode(session) == "paused"

    back = _data(_ask(http, "POST", CALL_MODE, {"mode": "resume"}))
    assert back["call_mode"] == "overflow" and _mode(session) == "overflow"
    assert {r.actor for r in _audit(session, "service_config.mode_changed")} == {ACTOR}
    assert _count(session, OutboxEvent) == 3


@pytest.mark.parametrize(
    "body",
    [{"mode": "ai_first"}, {"mode": ""}, {"mode": None}, {}, {"mode": ["primary"]}],
)
def test_a_mode_the_switch_does_not_have_is_refused(http, session, stack, body):
    _refused(_ask(http, "POST", CALL_MODE, body))
    assert _mode(session) == "shadow" and _count(session, OutboxEvent) == 0


def test_the_mode_that_holds_already_changes_and_pushes_nothing(http, session, stack):
    data = _data(_ask(http, "POST", CALL_MODE, {"mode": "shadow"}))

    assert data["call_mode"] == "shadow"
    assert _audit(session, "service_config.") == []
    assert _count(session, OutboxEvent) == 0


# --- the team number ----------------------------------------------------------


def test_the_team_number_is_todays_with_a_card_for_the_owner_and_a_push(
    http, session, stack
):
    standard = session.scalars(select(ServiceConfig)).one().team_phone

    data = _data(_ask(http, "POST", TEAM_PHONE, {"number": NUMBER}))

    assert data["team_phone"] == NUMBER
    # The operator sees that it is today's only, and what holds after it.
    assert data["team_phone_standard"] == standard
    assert data["team_phone_until"] is not None
    session.expire_all()
    config = session.scalars(select(ServiceConfig)).one()
    # The staff's rule holds for the operator too: today's number, the
    # standard stays, and the owner keeps or undoes it on the card.
    assert (config.team_phone, config.team_phone_today) == (standard, NUMBER)
    (card,) = session.scalars(select(StaffChange)).all()
    assert (card.kind, card.status, card.after) == (
        "team_phone",
        "open",
        {"number": NUMBER},
    )
    assert _audit(session, "staff_change.team_phone")[-1].actor == ACTOR
    (event,) = session.scalars(select(OutboxEvent)).all()
    assert event.event_type == "team_phone.changed"
    # The reason's row names no number: the step's own row does.
    assert NUMBER not in json.dumps(_payload(_audit(session, "ops.")[-1]))


@pytest.mark.parametrize(
    "body",
    [
        {"number": "+4915112345678"},
        {"number": "0721 5550000"},
        {"number": "Mueller"},
        {"number": ""},
        {"number": "+49" + "1" * 60},
        {},
    ],
)
def test_a_number_the_telephone_system_does_not_dial_is_refused(
    http, session, stack, body
):
    _refused(_ask(http, "POST", TEAM_PHONE, body))
    session.expire_all()
    assert session.scalars(select(ServiceConfig)).one().team_phone_today is None
    assert _count(session, StaffChange) == 0 and _count(session, OutboxEvent) == 0


def test_a_refused_mode_is_still_a_step_somebody_asked_for(http, session, stack):
    """As a refused PIN or number: the reason's row is written first."""
    _refused(_ask(http, "POST", CALL_MODE, {"mode": "ai_first"}))

    (row,) = _audit(session, "ops.")
    assert (row.action, row.actor) == ("ops.call_mode", ACTOR)
    assert _payload(row)["mode"] == "ai_first"
    assert _mode(session) == "shadow"


def test_a_restaurant_without_settings_offers_no_mode_and_no_number(
    http, session, stack
):
    """Both steps would end in "not found" after their reason was written."""
    session.execute(delete(ServiceConfig))
    session.commit()

    data = _data(_ask(http, "GET", ACCESS))

    assert (data["call_mode"], data["call_modes"]) == (None, [])
    assert (data["team_phone"], data["team_phone_standard"]) == (None, None)
    assert data["team_phones"] == []


def test_the_link_is_the_stacks_path_when_it_does_not_know_its_host(
    http, session, stack
):
    """`AGENT_DOMAIN` reaches the api through its settings (docker-compose.yml
    hands it over); without it the answer is the path, and the dashboard says so."""
    assert _data(_ask(http, "POST", LINK))["link"].startswith("/gui/einrichten#")


def test_a_rule_refuses_with_a_status_of_its_own(http, session, login, stack):
    """422: one of the stack's rules refused the step. A request the route
    itself does not take is `invalid_input` too, with 200 (the body's form) or
    400 (a header, a mode that does not exist): the dashboard must not explain
    that with a rule's sentence."""
    assert (
        _ask(http, "POST", PIN_SET, {"which": "team", "pin": ADMIN}).status_code == 422
    )
    assert (
        _ask(http, "POST", PIN_SET, {"which": "team", "pin": "12"}).status_code == 422
    )
    assert (
        _ask(http, "POST", TEAM_PHONE, {"number": "+4915112345678"}).status_code == 422
    )
    _data(_ask(http, "POST", CALL_MODE, {"mode": "paused"}))
    assert _ask(http, "POST", CALL_MODE, {"mode": "primary"}).status_code == 422

    assert _ask(http, "POST", CALL_MODE, {"mode": "ai_first"}).status_code == 400
    assert (
        _ask(http, "POST", PIN_SET, {"which": "owner", "pin": NEW}).status_code == 200
    )
    assert (
        _ask(http, "POST", PIN_SET, {"which": "team", "pin": NEW, "x": 1}).status_code
        == 200
    )
    assert (
        _ask(http, "POST", PIN_SET, None, **{"X-Ops-Reason": None}).status_code == 400
    )


def test_a_blank_team_number_is_no_number(http, session, stack):
    """A blank TEAM_PHONE is stored as an empty text: sent as it is, the
    dashboard would read the whole answer as "cannot be read"."""
    session.execute(update(ServiceConfig).values(team_phone=""))
    session.commit()

    data = _data(_ask(http, "GET", ACCESS))

    assert (data["team_phone"], data["team_phone_standard"]) == (None, None)


def test_no_more_numbers_are_offered_than_the_dashboard_reads(
    http, session, stack, monkeypatch
):
    many = ",".join(f"+4972155510{n:02d}" for n in range(operator.MAX_NUMBERS + 5))
    monkeypatch.setattr(settings, "transfer_targets", many)

    assert len(_data(_ask(http, "GET", ACCESS))["team_phones"]) == operator.MAX_NUMBERS


@pytest.mark.parametrize(
    "written",
    [" https://agent.example.com/ ", "agent.example.com.", "agent.example.com"],
)
def test_a_host_typed_with_its_scheme_or_dot_is_still_that_host(
    http, session, stack, monkeypatch, written
):
    monkeypatch.setattr(settings, "agent_domain", written)

    link = _data(_ask(http, "POST", LINK))["link"]

    assert link.startswith("https://agent.example.com/gui/einrichten#")


# --- the reads name their customer too (#811) -----------------------------------

READS = [
    "/v1/ops/status",
    "/v1/ops/figures",
    "/v1/ops/calls?day=2026-10-29",
    f"/v1/ops/calls/{uuid.UUID(int=1)}",
]


@pytest.mark.parametrize("path", READS)
def test_a_read_answers_only_for_its_customer(http, session, stack, path):
    """#811: `/calls` wrote its audit row and sent a day of calls before the
    dashboard noticed another customer answered. Every read now checks the
    key first, as the steps do, and writes nothing when it is not this one."""
    before = _count(session, AuditLog)
    for key in ("pizzeria-sued", "", None):
        answer = _ask(http, "GET", path, **{"X-Ops-Customer": key})
        assert answer.status_code == 409, path
        _refused(answer, "conflict")
    assert _count(session, AuditLog) == before
    assert _data(_ask(http, "GET", path))["tenant"]["key"] == KEY


@pytest.mark.parametrize("path", READS)
def test_a_read_of_a_restaurant_without_a_key_is_refused(http, session, stack, path):
    stack.customer_key = None
    session.commit()
    answer = _ask(http, "GET", path)
    assert answer.status_code == 409
    _refused(answer, "conflict")


# --- a body nobody may send is not read (#811) ----------------------------------


@pytest.mark.parametrize(
    "path", [PIN_SET, CALL_MODE, "/v1/tools/search_menu", "/v1/tools/confirm"]
)
@pytest.mark.parametrize("token", [None, "Bearer " + "e" * 64])
def test_a_body_that_cannot_be_read_is_refused_by_the_token_first(
    http, stack, path, token
):
    """FastAPI parses the body before the router's dependencies run, so an
    unauthenticated `{` got 200 `invalid_input` instead of 401."""
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = token
    answer = http.post(path, content=b"{", headers=headers)
    assert answer.status_code == 401
    assert answer.json()["error"]["message"] == "unauthorized"


def test_a_body_that_cannot_be_read_with_the_token_is_still_invalid_input(http, stack):
    answer = http.post(
        PIN_SET,
        content=b"{",
        headers=ASK | {"Content-Type": "application/json"},
    )
    assert answer.status_code == 200
    _refused(answer)


# --- a missing settings row is no missing restaurant (#811) ---------------------


def test_a_stack_without_its_settings_row_does_not_say_no_restaurant(
    http, session, stack
):
    """The dashboard reads `not_found` with the stack's own words for a
    missing restaurant as "no restaurant"; a missing settings row says
    something else."""
    session.execute(delete(ServiceConfig))
    session.commit()

    answer = _ask(http, "POST", CALL_MODE, {"mode": "primary"})

    _refused(answer, "not_found")
    assert answer.json()["error"]["message"] != ops_reason.NO_RESTAURANT


def test_a_stack_without_its_restaurant_says_so_in_the_shared_words(http):
    for method, path, body in [("GET", "/v1/ops/status", None), *STEPS]:
        answer = _ask(http, method, path, body)
        _refused(answer, "not_found")
        assert answer.json()["error"]["message"] == ops_reason.NO_RESTAURANT, path


# --- every ask has its outcome (#811) -------------------------------------------


def _pair(session, step: str) -> tuple[AuditLog, dict[str, Any]]:
    (ask,) = _audit(session, f"ops.{step}")
    (outcome,) = _audit(session, f"ops.{step}{OUTCOME}", outcomes=True)
    assert (outcome.actor, outcome.tenant_id) == (ask.actor, ask.tenant_id)
    payload = _payload(outcome)
    assert payload["ask"] == ask.id
    return ask, payload


@pytest.mark.parametrize(("method", "path", "body"), STEPS[1:])
def test_a_step_that_happened_has_the_outcome_done(
    http, session, login, stack, method, path, body
):
    _data(_ask(http, method, path, body))
    step = _audit(session, "ops.")[-1].action.removeprefix("ops.")
    assert _pair(session, step)[1]["outcome"] == "done"


@pytest.mark.parametrize(
    ("path", "body", "step"),
    [
        (PIN_SET, {"which": "team", "pin": ADMIN}, "pin_set"),
        (CALL_MODE, {"mode": "nonsense"}, "call_mode"),
        (TEAM_PHONE, {"number": "+491701234567"}, "team_phone"),
    ],
)
def test_a_refused_step_has_the_outcome_refused(
    http, session, login, stack, path, body, step
):
    _refused(_ask(http, "POST", path, body))
    assert _pair(session, step)[1]["outcome"] == "refused"


def test_a_step_that_broke_has_the_outcome_failed(
    http, session, login, stack, monkeypatch
):
    def broken(*args: object, **kwargs: object) -> None:
        raise RuntimeError("the step broke")

    monkeypatch.setattr(operator, "set_mode", broken)
    with pytest.raises(RuntimeError):
        _ask(http, "POST", CALL_MODE, {"mode": "primary"})
    assert _pair(session, "call_mode")[1]["outcome"] == "failed"


def test_a_step_that_broke_raises_its_own_error_when_its_outcome_cannot_be_written(
    http, session, login, stack, monkeypatch
):
    def broken(*args: object, **kwargs: object) -> None:
        raise RuntimeError("the step broke")

    def gone(*args: object, **kwargs: object) -> None:
        raise ConnectionError("the database is gone")

    monkeypatch.setattr(operator, "set_mode", broken)
    monkeypatch.setattr(operator, "_outcome", gone)
    with pytest.raises(RuntimeError, match="the step broke"):
        _ask(http, "POST", CALL_MODE, {"mode": "primary"})


def test_the_access_read_has_no_outcome_row(http, session, stack):
    _data(_ask(http, "GET", ACCESS))
    assert _audit(session, "ops.access_read", outcomes=True)[-1].action == (
        "ops.access_read"
    )


# --- the standard team number (#811 question 2) ---------------------------------

SECOND = "+4972215550000"


def _config(session) -> ServiceConfig:
    session.expire_all()
    return session.scalars(select(ServiceConfig)).one()


def test_the_operator_sets_the_standard_number_with_a_push_and_no_card(
    http, session, stack
):
    """A dead standard line held again every morning without a push when the
    owner did not answer the card (hard rule 5)."""
    standard = _config(session).team_phone

    data = _data(_ask(http, "POST", TEAM_PHONE_STANDARD, {"number": NUMBER}))

    assert (data["team_phone"], data["team_phone_standard"]) == (NUMBER, NUMBER)
    assert data["team_phone_until"] is None
    config = _config(session)
    assert (config.team_phone, config.team_phone_today) == (NUMBER, None)
    assert _count(session, StaffChange) == 0
    (row,) = _audit(session, "service_config.team_phone_standard")
    assert row.actor == ACTOR
    assert row.payload == {"from": standard, "to": NUMBER}
    (event,) = session.scalars(select(OutboxEvent)).all()
    assert event.event_type == "team_phone.changed"
    assert event.payload == {
        "day": "2026-10-29",
        "from": standard,
        "to": NUMBER,
        "standard": True,
        "by": "operator",
    }
    ask, outcome = _pair(session, "team_phone_standard")
    assert outcome["outcome"] == "done"
    # The reason's row names no number: the step's own row does.
    assert NUMBER not in json.dumps(_payload(ask))


@pytest.mark.parametrize(
    "number", ["+4915112345678", "0721 5550000", "Mueller", "+49" + "1" * 30]
)
def test_a_standard_number_takes_the_staffs_rule(http, session, stack, number):
    """The same check as the number for today: E.164 and one the telephone
    system dials. A rule's refusal has its own status."""
    before = _state(session, stack)

    answer = _ask(http, "POST", TEAM_PHONE_STANDARD, {"number": number})

    assert answer.status_code == 422
    _refused(answer)
    after = _state(session, stack)
    assert after[:4] == before[:4] and after[5] == before[5]
    assert _pair(session, "team_phone_standard")[1]["outcome"] == "refused"


def test_the_standard_that_holds_already_changes_and_pushes_nothing(
    http, session, stack, monkeypatch
):
    standard = _config(session).team_phone
    monkeypatch.setattr(settings, "transfer_targets", f"{NUMBER}, {standard}")

    _data(_ask(http, "POST", TEAM_PHONE_STANDARD, {"number": standard}))

    assert _audit(session, "service_config.") == []
    assert _count(session, OutboxEvent) == 0


def test_a_new_standard_leaves_todays_number_to_the_end_of_the_day(
    http, session, stack, monkeypatch
):
    """Today's number and its card stay: the staff chose it for today, and
    the owner still keeps or undoes it."""
    monkeypatch.setattr(settings, "transfer_targets", f"{NUMBER}, {SECOND}")
    change_team_phone(session, stack.id, NUMBER, NOW)

    data = _data(_ask(http, "POST", TEAM_PHONE_STANDARD, {"number": SECOND}))

    assert (data["team_phone"], data["team_phone_standard"]) == (NUMBER, SECOND)
    assert data["team_phone_until"] is not None
    config = _config(session)
    assert (config.team_phone, config.team_phone_today) == (SECOND, NUMBER)
    (card,) = session.scalars(select(StaffChange)).all()
    assert card.status == "open"


def test_the_number_for_today_names_the_operator_in_its_push(http, session, stack):
    """#811 question 1: the owner reads who changed it."""
    _data(_ask(http, "POST", TEAM_PHONE, {"number": NUMBER}))

    (event,) = session.scalars(select(OutboxEvent)).all()
    assert event.payload["by"] == "operator"
    assert "standard" not in event.payload


# --- the page the operator saw (#811) -------------------------------------------

VERSION = re.compile(r"[0-9a-f]{16}")
CHANGING_STEPS = [step for step in STEPS if step[1] in CHANGING]


def test_every_answer_carries_the_version_of_what_it_shows(http, session, login, stack):
    first = _data(_ask(http, "GET", ACCESS))["version"]
    assert VERSION.fullmatch(first)
    assert _data(_ask(http, "GET", ACCESS))["version"] == first

    after = _data(_ask(http, "POST", CALL_MODE, {"mode": "primary", "seen": first}))

    assert after["version"] != first
    # The answer is the page the next step comes from.
    _data(_ask(http, "POST", CALL_MODE, {"mode": "overflow", "seen": after["version"]}))
    assert _mode(session) == "overflow"


def test_a_device_in_use_does_not_make_the_page_old(http, session, login, stack):
    """A tablet moves its `last_seen_at` all day: that alone must not refuse
    every step."""
    first = _data(_ask(http, "GET", ACCESS))["version"]
    session.execute(
        update(GuiSession).values(
            last_seen_at=GuiSession.last_seen_at + timedelta(minutes=5)
        )
    )
    session.commit()

    assert _data(_ask(http, "GET", ACCESS))["version"] == first


def _stale(answer) -> None:
    assert answer.status_code == 412, answer.text
    _refused(answer, "conflict")


def test_a_page_from_before_the_owners_emergency_stop_does_not_end_it(
    http, session, stack
):
    """The page showed the agent paused and offered to switch it on. The owner
    switched it on and stopped it again since: the stop on the page is not the
    stop that holds."""
    pause_ai(session, stack.id, "gui:owner", NOW)
    seen = _data(_ask(http, "GET", ACCESS))["version"]
    resume_ai(session, stack.id, "gui:owner", NOW)
    pause_ai(session, stack.id, "gui:owner", NOW)
    pushes = _count(session, OutboxEvent)

    _stale(_ask(http, "POST", CALL_MODE, {"mode": "resume", "seen": seen}))

    assert _mode(session) == "paused"
    assert _count(session, OutboxEvent) == pushes
    assert _pair(session, "call_mode")[1]["outcome"] == "refused"


def test_a_page_from_before_the_owners_stop_cannot_choose_a_mode(http, session, stack):
    seen = _data(_ask(http, "GET", ACCESS))["version"]
    pause_ai(session, stack.id, "gui:owner", NOW)

    _stale(_ask(http, "POST", CALL_MODE, {"mode": "primary", "seen": seen}))

    assert _mode(session) == "paused"


CHANGES_SINCE = [
    ("a team PIN", lambda s, t: set_team_pin(s, t.id, "246813", "gui:owner")),
    ("a number for today", lambda s, t: change_team_phone(s, t.id, NUMBER, NOW)),
    ("the emergency stop", lambda s, t: pause_ai(s, t.id, "gui:owner", NOW)),
]


@pytest.mark.parametrize(("method", "path", "body"), CHANGING_STEPS)
@pytest.mark.parametrize(("what", "change"), CHANGES_SINCE)
def test_a_step_from_a_page_older_than_a_change_does_nothing(
    http, session, login, stack, method, path, body, what, change
):
    seen = _data(_ask(http, "GET", ACCESS))["version"]
    change(session, stack)
    session.commit()
    before = _state(session, stack)

    _stale(_ask(http, method, path, (body or {}) | {"seen": seen}))

    after = _state(session, stack)
    # Two audit rows more, the ask and its refusal; nothing else moved.
    assert after[:4] == before[:4] and after[5] == before[5]
    assert after[4] == before[4] + 2


@pytest.mark.parametrize(("method", "path", "body"), CHANGING_STEPS)
@pytest.mark.parametrize("seen", [None, "", "x" * 65])
def test_a_step_without_the_pages_version_is_not_taken(
    http, session, login, stack, method, path, body, seen
):
    before = _state(session, stack)
    sent = dict(body or {})
    if seen is not None:
        sent["seen"] = seen

    answer = http.request(method, path, headers=ASK, json=sent)

    assert answer.status_code == 200
    _refused(answer)
    assert _state(session, stack) == before


def test_a_pin_shown_needs_no_version(http, session, login, stack):
    """A look changes nothing: no page goes old over it."""
    assert _data(_ask(http, "POST", PIN_SHOW, {"which": "team"}))["pin"] == TEAM


def test_the_owners_answer_on_an_hours_card_does_not_make_the_page_old(
    http, session, stack
):
    """Review of #811: the page shows no opening hours."""
    from datetime import time as clock_time

    from api.domain.status import staff_changes as staff

    later = NOW.date() + timedelta(days=7)
    longer = staff.DayHours(opens=clock_time(9), closes=clock_time(23))
    staff.change_day_hours(session, stack.id, later, longer, NOW)
    (card,) = session.scalars(select(StaffChange)).all()
    seen = _data(_ask(http, "GET", ACCESS))["version"]

    staff.decide(session, stack.id, card.id, keep=True, now=NOW)

    assert _data(_ask(http, "GET", ACCESS))["version"] == seen
