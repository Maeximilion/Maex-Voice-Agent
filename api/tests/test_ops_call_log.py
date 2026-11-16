"""The stack's side of the dev tools' call log (#571, docs/23 §5 and §8):
`GET /v1/ops/calls/<call_id>`, one call with its tool calls. What a guest did,
so it needs the reason and the account and is in the audit log before a row is
read; no number leaves the stack, and the route writes nothing but that row.
"""

import uuid
from datetime import UTC, timedelta

import pytest
from sqlalchemy import func, select

from api.config import settings
from api.domain.calls import log
from api.models import AuditLog, Call, Tenant
from api.tests import test_ops_status as base
from api.tests.conftest import p95_ms
from api.tests.test_ops_detail import REASON, _keys, _make
from api.tests.test_ops_status import AUTH, CALLER, NOW

# Fixtures of the status tests, taken by assignment (as test_ops_detail.py).
bridge_settings = base.bridge_settings
http = base.http
session = base.session
tenant = base.tenant

TOOLS = [
    {"name": "lookup_menu", "duration_ms": 41.5, "ok": True},
    {"name": "draft_order", "duration_ms": 12.0, "ok": False, "error_code": "not_found"},
    {"name": "confirm", "duration_ms": 30.2, "ok": True},
]


def _path(call_id: object) -> str:
    return f"/v1/ops/calls/{call_id}"


def _one(session, tenant, **over) -> Call:
    args = {
        "caller": CALLER,
        "intent": "pickup",
        "duration": 125,
        "cost": 12,
        "tool_calls": TOOLS,
    } | over
    call = _make(session, tenant, args.pop("at", NOW - timedelta(hours=1)), **args)
    session.commit()
    return call


def _reads(session, tenant) -> list[AuditLog]:
    session.expire_all()
    return list(
        session.scalars(
            select(AuditLog)
            .where(
                AuditLog.tenant_id == tenant.id,
                AuditLog.action == log.SUPPORT_CALL_READ,
            )
            .order_by(AuditLog.at, AuditLog.id)
        )
    )


def _get(http, call_id: object, headers: dict | None = None):
    return http.get(_path(call_id), headers=AUTH | REASON if headers is None else headers)


# --- who may ask --------------------------------------------------------------


def test_the_route_refuses_every_other_key(http, session, tenant):
    call = _one(session, tenant)
    assert _get(http, call.id, REASON).status_code == 401
    wrong = {"Authorization": f"Bearer {'e' * 64}"} | REASON
    assert _get(http, call.id, wrong).status_code == 401
    agent = {"Authorization": f"Bearer {settings.agent_api_token}"} | REASON
    assert _get(http, call.id, agent).status_code == 401
    assert _reads(session, tenant) == []


@pytest.mark.parametrize(
    ("reason", "actor"),
    [
        (None, "maxi"),
        ("restaurant_report #812", None),
        ("because I want to", "maxi"),
        ("restaurant_report", "maxi"),
        ("restaurant_report #812", "Ma xi"),
    ],
)
def test_a_call_is_refused_without_a_listed_reason_and_an_account(
    http, session, tenant, reason, actor
):
    call = _one(session, tenant)
    headers = dict(AUTH)
    if reason is not None:
        headers["X-Ops-Reason"] = reason
    if actor is not None:
        headers["X-Ops-Actor"] = actor

    response = _get(http, call.id, headers)

    assert response.status_code == 400
    assert _reads(session, tenant) == []
    assert "lookup_menu" not in response.text
    assert "call" not in (response.json().get("data") or {})


# --- the answer ---------------------------------------------------------------


def test_a_call_is_its_row_and_its_tool_calls_and_holds_no_number(
    http, session, tenant
):
    call = _one(session, tenant, outcome="transferred")
    call.transfer_reason = "human_requested"
    call.model = "scripted"
    call.ended_at = call.started_at + timedelta(seconds=125)
    session.commit()

    response = _get(http, call.id)

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["tenant"] == {"key": tenant.customer_key, "name": "Testbetrieb"}
    assert data["call_id"] == str(call.id)
    found = data["call"]
    assert found["started_at"] == call.started_at.astimezone(UTC).isoformat()
    assert found["ended_at"] == call.ended_at.astimezone(UTC).isoformat()
    assert found["duration_seconds"] == 125
    assert found["intent"] == "pickup"
    assert found["outcome"] == "transferred"
    assert found["transfer_reason"] == "human_requested"
    assert found["line_note"] is None
    assert found["cost_cents"] == 12
    assert found["model"] == "scripted"
    assert found["is_test"] is False
    assert found["tool_calls_total"] == 3
    assert found["tool_calls"] == [
        {"name": "lookup_menu", "duration_ms": 41.5, "ok": True, "error_code": None},
        {
            "name": "draft_order",
            "duration_ms": 12.0,
            "ok": False,
            "error_code": "not_found",
        },
        {"name": "confirm", "duration_ms": 30.2, "ok": True, "error_code": None},
    ]
    assert not {"caller_id", "caller", "external_session_id"} & _keys(data)
    assert CALLER not in response.text


def test_the_read_is_in_the_audit_log_each_time(http, session, tenant):
    call = _one(session, tenant)

    _get(http, call.id)

    (row,) = _reads(session, tenant)
    assert row.actor == "operator:maxi"
    assert row.entity == "call"
    assert row.payload == {
        "reason": "restaurant_report",
        "reference": "#812",
        "call": str(call.id),
    }
    _get(http, call.id)
    assert len(_reads(session, tenant)) == 2


def test_the_audit_row_is_there_before_the_call_is_read(
    http, session, tenant, monkeypatch
):
    call = _one(session, tenant)

    def broken(*args, **kwargs):
        raise RuntimeError("read failed")

    monkeypatch.setattr(log, "_one_call", broken)
    try:
        response = _get(http, call.id)
    except RuntimeError:
        response = None

    if response is not None:
        assert response.status_code >= 500
        assert "call" not in (response.json().get("data") or {})
    assert len(_reads(session, tenant)) == 1


def test_the_route_writes_nothing_but_its_audit_row(http, session, tenant):
    call = _one(session, tenant)
    before = session.scalar(select(func.count()).select_from(AuditLog))
    updated = call.updated_at

    _get(http, call.id)

    session.expire_all()
    assert session.scalar(select(func.count()).select_from(AuditLog)) == before + 1
    assert session.get(Call, call.id).updated_at == updated
    assert session.scalar(select(func.count()).select_from(Call)) == 1


# --- no such call -------------------------------------------------------------


def test_an_unknown_call_is_no_call_and_still_a_recorded_read(http, session, tenant):
    _one(session, tenant)
    unknown = uuid.uuid4()

    response = _get(http, unknown)

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["tenant"]["name"] == "Testbetrieb"
    assert data["call_id"] == str(unknown)
    assert data["call"] is None
    (row,) = _reads(session, tenant)
    assert row.payload["call"] == str(unknown)


def test_another_restaurants_call_is_no_call(http, session, tenant):
    other = Tenant(name="Anderer Betrieb")
    session.add(other)
    session.flush()
    theirs = _one(session, other)

    response = _get(http, theirs.id)

    assert response.json()["data"]["call"] is None
    assert "lookup_menu" not in response.text


@pytest.mark.parametrize(("back", "shown"), [(13, True), (14, False)])
def test_a_call_reaches_back_as_far_as_the_call_list(
    http, session, tenant, back, shown
):
    call = _one(session, tenant, at=NOW - timedelta(days=back))

    found = _get(http, call.id).json()["data"]["call"]

    assert (found is not None) is shown


def test_a_self_test_call_is_shown_and_says_so(http, session, tenant):
    call = _one(session, tenant, is_test=True)

    assert _get(http, call.id).json()["data"]["call"]["is_test"] is True


@pytest.mark.parametrize("call_id", ["nope", "1", "0" * 31, "x" * 300])
def test_what_is_no_call_id_is_refused_without_an_audit_row(
    http, session, tenant, call_id
):
    _one(session, tenant)

    response = _get(http, call_id)

    # The API's own answer to a field it cannot read (`api/core/errors.py`).
    body = response.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "invalid_input"
    assert "data" not in body or body["data"] is None
    assert _reads(session, tenant) == []


# --- what the log holds, whatever was written into it ----------------------------


def test_only_the_first_two_hundred_tool_calls_are_sent_and_the_total_says_so(
    http, session, tenant
):
    many = [{"name": f"tool_{n}", "duration_ms": 1.0, "ok": True} for n in range(250)]
    call = _one(session, tenant, tool_calls=many)

    found = _get(http, call.id).json()["data"]["call"]

    assert found["tool_calls_total"] == 250
    assert [t["name"] for t in found["tool_calls"]] == [
        f"tool_{n}" for n in range(log.TOOL_CALLS_SENT)
    ]


def test_an_odd_entry_is_sent_as_what_it_can_be(http, session, tenant):
    odd = [
        "no entry",
        {"name": "x" * 500, "duration_ms": "fast", "ok": "yes", "error_code": 7},
        {"duration_ms": -3, "args": {"phone": CALLER}},
        {"name": "confirm", "duration_ms": 10**12, "ok": True, "error_code": "y" * 500},
    ]
    call = _one(session, tenant, tool_calls=odd)

    response = _get(http, call.id)

    found = response.json()["data"]["call"]
    assert found["tool_calls_total"] == 4
    assert found["tool_calls"] == [
        {"name": "?", "duration_ms": None, "ok": False, "error_code": None},
        {"name": "x" * 64, "duration_ms": None, "ok": False, "error_code": None},
        {"name": "?", "duration_ms": None, "ok": False, "error_code": None},
        {"name": "confirm", "duration_ms": None, "ok": True, "error_code": "y" * 64},
    ]
    assert CALLER not in response.text


@pytest.mark.latency
def test_http_call_log_under_300_ms(http, session, tenant):
    call = _one(session, tenant)
    ms = p95_ms(lambda: _get(http, call.id), rounds=20)
    assert ms < 300, f"call log p95 {ms:.0f} ms"
