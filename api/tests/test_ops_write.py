"""The stack's side of the operator's write steps (#571, docs/23 §8): the access
read and the six steps behind it (both PINs shown and set, devices signed out,
a setup link, the call mode, the team number). Each needs the ops token, a
reason, the operator's account and the customer's key, is in the audit log
before it happens, and never puts a PIN or a link into a log or an audit row.
"""

import json
import logging
import uuid
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import delete, func, select

from api.config import settings
from api.domain import operator
from api.domain.access.sessions import DEVICE_CHARS, locked_account, open_session
from api.domain.access.setup import issue_link, use_link
from api.models import (
    AuditLog,
    GuiAccount,
    GuiSession,
    GuiSetupLink,
    OutboxEvent,
    ServiceConfig,
    StaffChange,
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
STEPS: list[tuple[str, str, dict[str, Any] | None]] = [
    ("GET", ACCESS, None),
    ("POST", PIN_SHOW, {"which": "team"}),
    ("POST", PIN_SET, {"which": "team", "pin": NEW}),
    ("POST", SIGN_OUT, {"all": True}),
    ("POST", LINK, None),
    ("POST", CALL_MODE, {"mode": "primary"}),
    ("POST", TEAM_PHONE, {"number": NUMBER}),
]


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


def _ask(http, method: str, path: str, body=None, **headers: str | None):
    sent = {k: v for k, v in (ASK | headers).items() if v is not None}
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


def _audit(session, prefix: str = "") -> list[AuditLog]:
    session.expire_all()
    rows = session.scalars(select(AuditLog).order_by(AuditLog.id)).all()
    return [row for row in rows if row.action.startswith(prefix)]


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
