"""The stack's side of the customer detail page (#571, docs/23 §8):
`GET /v1/ops/figures` and `GET /v1/ops/calls`, the reason and actor headers, the
audit row written before a call is read, and the pure helpers around them.
"""

import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from api.config import settings
from api.core import ops_reason
from api.domain.calls import log
from api.domain.ordering.labels import ACTION_CORRECTED
from api.gui import calls_view
from api.models import AuditLog, Call, Order
from api.tests import test_ops_status as base
from api.tests.conftest import p95_ms
from api.tests.test_ops_status import AUTH, CALLER, NOW

# Fixtures of the status tests, taken by assignment: an import would be flagged
# as unused and then as redefined by every parameter of the same name.
bridge_settings = base.bridge_settings
http = base.http
session = base.session
tenant = base.tenant

OTHER = "+4972215550777"
THIRD = "+4972215550888"
REASON = {"X-Ops-Reason": "restaurant_report #812", "X-Ops-Actor": "maxi"}
CALLS = "/v1/ops/calls"
TODAY = date(2026, 10, 29)


def _day(back: int = 0) -> str:
    return (TODAY - timedelta(days=back)).isoformat()


def _make(
    session,
    tenant,
    at,
    *,
    caller=CALLER,
    outcome="completed",
    cost=12,
    intent=None,
    is_test=False,
    line_note=None,
    duration=None,
    tool_calls=None,
    order=False,
    corrected=False,
) -> Call:
    call = Call(
        tenant_id=tenant.id,
        external_session_id=uuid.uuid4().hex,
        caller_id=caller,
        started_at=at,
        duration_seconds=duration,
        intent=intent,
        outcome=outcome,
        line_note=line_note,
        cost_cents=cost,
        tool_calls=tool_calls or [],
        is_test=is_test,
        delete_after=date(2028, 10, 29),
    )
    session.add(call)
    session.flush()
    if order:
        placed = Order(
            tenant_id=tenant.id,
            call_id=call.id,
            type="pickup",
            status="confirmed",
            phone="+4917612345678",
            customer_name="Test",
            items_total_cents=1000,
            total_cents=1000,
            idempotency_key=uuid.uuid4().hex,
            created_at=at,
        )
        session.add(placed)
        session.flush()
        if corrected:
            session.add(
                AuditLog(
                    tenant_id=tenant.id,
                    at=at,
                    actor="gui:tablet",
                    action=ACTION_CORRECTED,
                    entity="order",
                    entity_id=placed.id,
                    payload={},
                )
            )
    return call


def _reads(session, tenant) -> list[AuditLog]:
    session.expire_all()
    return list(
        session.scalars(
            select(AuditLog)
            .where(AuditLog.tenant_id == tenant.id, AuditLog.action == log.SUPPORT_READ)
            .order_by(AuditLog.at, AuditLog.id)
        )
    )


def _busy_week(session, tenant) -> None:
    berlin = NOW.tzinfo
    # Today, the 29th: two calls, two orders, one corrected.
    _make(session, tenant, NOW - timedelta(hours=5), order=True, cost=10)
    _make(
        session,
        tenant,
        NOW - timedelta(hours=2),
        outcome="transferred",
        order=True,
        corrected=True,
        cost=14,
    )
    # The 28th: one in the evening, one at 03:00 on the 29th, which still
    # belongs to the 28th.
    _make(session, tenant, NOW - timedelta(days=1, hours=-2), order=True)
    _make(session, tenant, datetime(2026, 10, 29, 3, 0, tzinfo=berlin))
    # The 27th: one abandoned call, no order.
    _make(session, tenant, NOW - timedelta(days=2), outcome="abandoned")
    # Eight business days back: in no figure.
    _make(session, tenant, NOW - timedelta(days=8), order=True)
    session.commit()


# --- the token --------------------------------------------------------------


@pytest.mark.parametrize("path", ["/v1/ops/figures", CALLS + "?day=2026-10-29"])
def test_both_routes_refuse_every_other_key(http, tenant, path):
    assert http.get(path, headers=REASON).status_code == 401
    wrong = {"Authorization": f"Bearer {'e' * 64}"} | REASON
    assert http.get(path, headers=wrong).status_code == 401
    agent = {"Authorization": f"Bearer {settings.agent_api_token}"} | REASON
    assert http.get(path, headers=agent).status_code == 401


def test_a_wrong_token_writes_no_audit_row(http, session, tenant):
    wrong = {"Authorization": f"Bearer {'e' * 64}"} | REASON
    assert http.get(CALLS + "?day=2026-10-29", headers=wrong).status_code == 401
    assert _reads(session, tenant) == []


# --- /figures ---------------------------------------------------------------


def test_figures_of_a_busy_week(http, session, tenant):
    _busy_week(session, tenant)

    response = http.get("/v1/ops/figures", headers=AUTH)

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["tenant"] == {"key": tenant.customer_key, "name": "Testbetrieb"}
    assert data["business_day"] == "2026-10-29"
    days = data["days"]
    assert len(days) == 7
    assert [d["day"] for d in days] == [_day(back) for back in range(7)]
    by_day = {d["day"]: d for d in days}
    today = by_day["2026-10-29"]
    assert (today["calls"], today["orders"], today["handed_over"]) == (2, 2, 1)
    assert today["corrected"] == 1
    assert today["cost_cents"] == 24
    # 1 of 2 orders kept, 1 of 2 calls handed over, (2 * 24 + 2) // 4 cents.
    assert today["accuracy_percent"] == 50
    assert today["handed_over_percent"] == 50
    assert today["cost_per_order_cents"] == 12
    assert (by_day["2026-10-28"]["calls"], by_day["2026-10-28"]["orders"]) == (2, 1)
    assert (by_day["2026-10-27"]["calls"], by_day["2026-10-27"]["missed"]) == (1, 1)
    week = data["week"]
    assert (week["calls"], week["orders"], week["missed"]) == (5, 3, 1)
    assert week["calls"] == sum(d["calls"] for d in days)
    assert week["orders"] == sum(d["orders"] for d in days)
    assert week["handed_over"] == 1
    assert CALLER not in response.text


def test_figures_of_an_empty_stack_are_zeros_and_nulls(http, tenant):
    data = http.get("/v1/ops/figures", headers=AUTH).json()["data"]

    assert len(data["days"]) == 7
    for figures in (data["week"], *data["days"]):
        assert (figures["calls"], figures["orders"], figures["missed"]) == (0, 0, 0)
        assert figures["accuracy_percent"] is None
        assert figures["cost_per_order_cents"] is None
        assert figures["handed_over_percent"] is None


def test_a_stack_without_its_restaurant_answers_not_found(http, session):
    figures = http.get("/v1/ops/figures", headers=AUTH).json()
    assert figures["ok"] is False and figures["error"]["code"] == "not_found"

    calls = http.get(CALLS + "?day=2026-10-29", headers=AUTH | REASON).json()
    assert calls["ok"] is False and calls["error"]["code"] == "not_found"
    session.expire_all()
    assert (
        session.scalars(
            select(AuditLog).where(AuditLog.action == "ops.calls_read")
        ).all()
        == []
    )


# --- /calls: the reason -----------------------------------------------------

GOOD_ACTOR = "maxi"
GOOD_REASON = "restaurant_report #812"
REFUSED_HEADERS = [
    pytest.param(None, GOOD_ACTOR, id="no-reason"),
    pytest.param(GOOD_REASON, None, id="no-actor"),
    pytest.param("curiosity #12", GOOD_ACTOR, id="unknown-category"),
    pytest.param("restaurant_report", GOOD_ACTOR, id="no-number"),
    pytest.param("restaurant_report 12", GOOD_ACTOR, id="no-hash"),
    pytest.param("restaurant_report #0", GOOD_ACTOR, id="zero"),
    pytest.param("restaurant_report #12 Mueller", GOOD_ACTOR, id="a-name-after"),
    pytest.param("restaurant_report  #12", GOOD_ACTOR, id="two-spaces"),
    pytest.param("restaurant_report #1234567890", GOOD_ACTOR, id="ten-digits"),
    pytest.param("Herr Mueller hat angerufen", GOOD_ACTOR, id="free-text"),
    pytest.param("#12 restaurant_report", GOOD_ACTOR, id="reversed"),
    pytest.param(GOOD_REASON, "Maxi", id="actor-upper-case"),
    pytest.param(GOOD_REASON, "a", id="actor-too-short"),
    pytest.param(GOOD_REASON, "operator:maxi", id="actor-with-colon"),
    pytest.param(GOOD_REASON, "x" * 41, id="actor-too-long"),
    pytest.param(GOOD_REASON, "ma xi", id="actor-with-space"),
    pytest.param(GOOD_REASON, "", id="actor-empty"),
]


@pytest.mark.parametrize(("reason", "actor"), REFUSED_HEADERS)
def test_calls_are_refused_without_a_listed_reason_and_an_account(
    http,
    session,
    tenant,
    reason,
    actor,
):
    _make(session, tenant, NOW - timedelta(hours=1), caller=CALLER, intent="pickup")
    session.commit()
    headers = dict(AUTH)
    if reason is not None:
        headers["X-Ops-Reason"] = reason
    if actor is not None:
        headers["X-Ops-Actor"] = actor

    response = http.get(CALLS + "?day=2026-10-29", headers=headers)

    assert response.status_code == 400
    body = response.json()
    assert body["ok"] is False and body["error"]["code"] == "invalid_input"
    assert _reads(session, tenant) == []
    assert "pickup" not in response.text and CALLER not in response.text
    assert "calls" not in (body.get("data") or {})


# --- /calls: the answer -----------------------------------------------------


def _calls_day(session, tenant) -> None:
    _make(
        session,
        tenant,
        datetime(2026, 10, 29, 8, 0, tzinfo=NOW.tzinfo),
        caller=CALLER,
        intent="pickup",
        duration=125,
        cost=12,
        tool_calls=[{"name": "lookup"}, {"name": "confirm"}],
    )
    _make(
        session,
        tenant,
        datetime(2026, 10, 29, 9, 0, tzinfo=NOW.tzinfo),
        caller=OTHER,
        intent="info",
        outcome="transferred",
    )
    _make(
        session,
        tenant,
        datetime(2026, 10, 29, 10, 0, tzinfo=NOW.tzinfo),
        caller=CALLER,
        intent="reservation",
    )
    _make(
        session,
        tenant,
        datetime(2026, 10, 29, 11, 0, tzinfo=NOW.tzinfo),
        caller=None,
        outcome="abandoned",
        line_note="no_caller_audio",
    )
    # The business day before, and a self-test call today: in neither answer.
    _make(session, tenant, NOW - timedelta(days=1), caller=THIRD)
    _make(session, tenant, NOW - timedelta(hours=1), caller=THIRD, is_test=True)
    session.commit()


def _keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {k for v in value.values() for k in _keys(v)}
    if isinstance(value, list):
        return {k for v in value for k in _keys(v)}
    return set()


def test_the_calls_of_a_day_count_callers_and_hold_no_number(
    http,
    session,
    tenant,
):
    _calls_day(session, tenant)

    response = http.get(CALLS + "?day=2026-10-29", headers=AUTH | REASON)

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["tenant"] == {"key": tenant.customer_key, "name": "Testbetrieb"}
    assert data["business_day"] == "2026-10-29"
    calls = data["calls"]
    assert [c["started_at"] for c in calls] == [
        datetime(2026, 10, 29, h, tzinfo=NOW.tzinfo).astimezone(UTC).isoformat()
        for h in (11, 10, 9, 8)
    ]
    assert [c["caller"] for c in calls] == [None, 1, 2, 1]
    assert [c["intent"] for c in calls] == [None, "reservation", "info", "pickup"]
    assert calls[0]["outcome"] == "abandoned"
    assert calls[0]["line_note"] == "no_caller_audio"
    assert calls[2]["outcome"] == "transferred"
    first = calls[3]
    assert first["duration_seconds"] == 125
    assert first["cost_cents"] == 12
    assert first["tool_calls"] == 2
    assert {"intent", "outcome", "duration_seconds", "cost_cents", "tool_calls"} <= set(
        first
    )
    assert "caller_id" not in _keys(data)
    for number in (CALLER, OTHER, THIRD):
        assert number not in response.text


def test_the_read_is_in_the_audit_log(http, session, tenant):
    _calls_day(session, tenant)

    http.get(CALLS + "?day=2026-10-29", headers=AUTH | REASON)

    (row,) = _reads(session, tenant)
    assert row.actor == "operator:maxi"
    assert row.entity == "call"
    assert row.entity_id is None
    assert row.payload == {
        "reason": "restaurant_report",
        "reference": "#812",
        "day": "2026-10-29",
    }
    http.get(CALLS + "?day=2026-10-29", headers=AUTH | REASON)
    assert len(_reads(session, tenant)) == 2


def test_the_audit_row_is_there_before_a_call_is_read(
    http,
    session,
    tenant,
    monkeypatch,
):
    _calls_day(session, tenant)

    def broken(*args, **kwargs):
        raise RuntimeError("read failed")

    monkeypatch.setattr(log, "list_calls", broken)
    try:
        response = http.get(CALLS + "?day=2026-10-29", headers=AUTH | REASON)
    except RuntimeError:
        response = None

    if response is not None:
        assert response.status_code >= 500
        assert "calls" not in (response.json().get("data") or {})
    assert len(_reads(session, tenant)) == 1


# --- /calls: the day --------------------------------------------------------


def test_the_day_reaches_back_thirteen_days_and_no_further(
    http,
    session,
    tenant,
):
    answered = http.get(CALLS + f"?day={_day(13)}", headers=AUTH | REASON)
    assert answered.status_code == 200
    assert answered.json()["data"]["business_day"] == _day(13)
    assert len(_reads(session, tenant)) == 1

    for day in (_day(14), _day(-1)):
        refused = http.get(CALLS + f"?day={day}", headers=AUTH | REASON)
        assert refused.status_code == 400
        assert refused.json()["error"]["code"] == "invalid_input"
    assert len(_reads(session, tenant)) == 1


@pytest.mark.parametrize("query", ["?day=garbage", "", "?day=2026-13-45", "?day="])
def test_a_missing_or_bad_day_is_refused_without_an_audit_row(
    http,
    session,
    tenant,
    query,
):
    response = http.get(CALLS + query, headers=AUTH | REASON)

    body = response.json()
    assert body["ok"] is False and body["error"]["code"] == "invalid_input"
    assert _reads(session, tenant) == []


def test_two_day_parameters_answer_one_business_day(
    http,
    session,
    tenant,
):
    _make(session, tenant, NOW - timedelta(hours=1), caller=CALLER)
    _make(session, tenant, NOW - timedelta(days=1), caller=OTHER)
    _make(session, tenant, NOW - timedelta(days=1, hours=1), caller=THIRD)
    session.commit()

    response = http.get(CALLS + f"?day={_day(0)}&day={_day(1)}", headers=AUTH | REASON)

    data = response.json()["data"]
    assert data["business_day"] in (_day(0), _day(1))
    assert len(data["calls"]) == {_day(0): 1, _day(1): 2}[data["business_day"]]
    (row,) = _reads(session, tenant)
    assert row.payload is not None
    assert row.payload["day"] == data["business_day"]


@pytest.mark.latency
def test_http_figures_and_calls_under_300_ms(http, session, tenant):
    _busy_week(session, tenant)
    _calls_day(session, tenant)
    ms = p95_ms(lambda: http.get("/v1/ops/figures", headers=AUTH), rounds=20)
    assert ms < 300, f"figures p95 {ms:.0f} ms"
    ms = p95_ms(
        lambda: http.get(CALLS + "?day=2026-10-29", headers=AUTH | REASON), rounds=20
    )
    assert ms < 300, f"calls p95 {ms:.0f} ms"


# --- the helpers ------------------------------------------------------------


@pytest.mark.parametrize("number", ["812", "#812", " 812 "])
def test_a_reason_takes_the_number_with_or_without_its_hash(number):
    assert ops_reason.reason("restaurant_report", number) == "restaurant_report #812"


@pytest.mark.parametrize("number", ["", "0", "12a", "##12", "Mueller"])
def test_a_reason_needs_a_ticket_number(number):
    assert ops_reason.reason("restaurant_report", number) is None


@pytest.mark.parametrize("category", ["curiosity", ""])
def test_a_reason_needs_a_listed_category(category):
    assert ops_reason.reason(category, "812") is None


def test_parse_reads_only_what_reason_writes():
    assert ops_reason.parse("restaurant_report #812") == ("restaurant_report", "#812")
    for bad in (
        "restaurant_report #812 ",
        "restaurant_report #8\n12",
        "curiosity #812",
        "",
    ):
        assert ops_reason.parse(bad) is None
    for category in ops_reason.REASONS:
        value = ops_reason.reason(category, "5")
        assert value is not None
        assert ops_reason.parse(value) == (category, "#5")


def test_the_team_page_and_the_operator_see_the_same_days():
    assert calls_view.DAYS == log.DAYS_LISTED
