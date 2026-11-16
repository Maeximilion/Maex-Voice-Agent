"""The calls of one business day, for the call list (T-8.7, docs/06 §4).

Read only. The number is the caller id as the call log holds it; the deletion
job takes it after its period (docs/03 "Löschkonzept"), and how much of it a
screen shows is the screen's decision.
"""

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.core.errors import InvalidInput
from api.core.time import business_day, business_day_bounds_utc
from api.models import AuditLog, Call, Tenant

# The business days a call list reaches back, today included: for the team's
# page and for the operator, who sees no more days than the team does. The
# caller id is kept far longer (docs/03).
DAYS_LISTED = 14
# The operator read a day's call list over the ops API (docs/23 §8).
SUPPORT_READ = "ops.calls_read"
# The operator read one call with its tool calls (the dev tools, docs/23 §5).
SUPPORT_CALL_READ = "ops.call_read"
# A call makes a handful of tool calls; what a broken loop wrote is cut here.
TOOL_CALLS_SENT = 200
_CODE_LENGTH = 64
_LONGEST_MS = 3_600_000


@dataclass(frozen=True)
class CallRow:
    started_at: datetime
    caller_id: str | None
    intent: str | None
    # None while the call is still running.
    outcome: str | None
    duration_seconds: int | None
    cost_cents: int | None
    tool_calls: int
    line_note: str | None


def list_calls(
    session: Session,
    tenant: Tenant,
    day: date,
    *,
    outcome: str | None = None,
    intent: str | None = None,
) -> list[CallRow]:
    """The tenant's calls that started on the business day, newest first."""
    start, end = business_day_bounds_utc(day, tenant.timezone)
    query = select(Call).where(
        Call.tenant_id == tenant.id,
        Call.started_at >= start,
        Call.started_at < end,
        Call.is_test.is_(False),
    )
    if outcome is not None:
        query = query.where(Call.outcome == outcome)
    if intent is not None:
        query = query.where(Call.intent == intent)
    return [
        CallRow(
            started_at=call.started_at,
            caller_id=call.caller_id,
            intent=call.intent,
            outcome=call.outcome,
            duration_seconds=call.duration_seconds,
            cost_cents=call.cost_cents,
            tool_calls=len(call.tool_calls or []),
            line_note=call.line_note,
        )
        for call in session.scalars(query.order_by(Call.started_at.desc()))
    ]


@dataclass(frozen=True)
class SupportCall:
    """A call as the operator sees it (docs/23 §8): no number leaves the stack.
    `caller` counts the day's callers in the order of their first call, so the
    same guest calling three times shows; None for a withheld number."""

    started_at: datetime
    caller: int | None
    intent: str | None
    outcome: str | None
    duration_seconds: int | None
    cost_cents: int | None
    tool_calls: int
    line_note: str | None


def _recorded(
    session: Session,
    tenant: Tenant,
    action: str,
    actor: str,
    reason: str,
    reference: str,
    **what: str,
) -> None:
    """The operator's read in the audit log, committed before a row is read."""
    session.add(
        AuditLog(
            tenant_id=tenant.id,
            actor=actor,
            action=action,
            entity="call",
            payload={"reason": reason, "reference": reference, **what},
        )
    )
    session.commit()


def calls_for_support(
    session: Session,
    tenant: Tenant,
    day: date,
    now: datetime,
    *,
    actor: str,
    reason: str,
    reference: str,
) -> list[SupportCall]:
    """One business day's calls for the operator, newest first. The read is in
    the audit log, committed, before a row is read: who, why and which day,
    nothing of a guest. A day the team's own list does not reach is refused, so
    is one that has not begun."""
    today = business_day(now, tenant.timezone)
    if not timedelta(0) <= today - day < timedelta(days=DAYS_LISTED):
        raise InvalidInput(
            f"the call list reaches back {DAYS_LISTED} business days", status=400
        )
    _recorded(
        session, tenant, SUPPORT_READ, actor, reason, reference, day=day.isoformat()
    )
    rows = list_calls(session, tenant, day)
    callers: dict[str, int] = {}
    for row in reversed(rows):
        if row.caller_id:
            callers.setdefault(row.caller_id, len(callers) + 1)
    return [
        SupportCall(
            started_at=row.started_at,
            caller=callers.get(row.caller_id or ""),
            intent=row.intent,
            outcome=row.outcome,
            duration_seconds=row.duration_seconds,
            cost_cents=row.cost_cents,
            tool_calls=row.tool_calls,
            line_note=row.line_note,
        )
        for row in rows
    ]


@dataclass(frozen=True)
class ToolCall:
    """One entry of `calls.tool_calls` (`core/tool_log.py`): which tool, how
    long, whether it answered ok. Never what was asked or answered."""

    name: str
    duration_ms: float | None
    ok: bool
    error_code: str | None


@dataclass(frozen=True)
class SupportCallLog:
    """One call as the operator's dev tools see it (docs/23 §5): the row
    without its number and its session id, and the tool calls in their order."""

    started_at: datetime
    ended_at: datetime | None
    duration_seconds: int | None
    intent: str | None
    outcome: str | None
    transfer_reason: str | None
    line_note: str | None
    cost_cents: int | None
    model: str | None
    is_test: bool
    tool_calls: list[ToolCall]
    # All of them; `tool_calls` holds the first TOOL_CALLS_SENT.
    tool_calls_total: int


def _tool_call(entry: Any) -> ToolCall:
    """An entry as the four values it should hold. The column is JSON, so
    whatever else stands there is left out, not passed on."""
    found = entry if isinstance(entry, dict) else {}
    name, ms, code = (found.get(k) for k in ("name", "duration_ms", "error_code"))
    timed = isinstance(ms, int | float) and not isinstance(ms, bool)
    return ToolCall(
        name=name[:_CODE_LENGTH] if isinstance(name, str) and name else "?",
        duration_ms=float(ms) if timed and 0 <= ms <= _LONGEST_MS else None,
        ok=found.get("ok") is True,
        error_code=code[:_CODE_LENGTH] if isinstance(code, str) and code else None,
    )


def _one_call(session: Session, tenant: Tenant, call_id: uuid.UUID) -> Call | None:
    return session.scalar(
        select(Call).where(Call.id == call_id, Call.tenant_id == tenant.id)
    )


def call_for_support(
    session: Session,
    tenant: Tenant,
    call_id: uuid.UUID,
    now: datetime,
    *,
    actor: str,
    reason: str,
    reference: str,
) -> SupportCallLog | None:
    """One call for the operator, by its id; a self-test call too. The read is
    in the audit log, committed, before the row is read. None for an id this
    restaurant does not have and for a call older than the call list reaches:
    the operator sees no more days here than there."""
    _recorded(
        session, tenant, SUPPORT_CALL_READ, actor, reason, reference, call=str(call_id)
    )
    call = _one_call(session, tenant, call_id)
    if call is None:
        return None
    age = business_day(now, tenant.timezone) - business_day(
        call.started_at, tenant.timezone
    )
    if not timedelta(0) <= age < timedelta(days=DAYS_LISTED):
        return None
    entries = call.tool_calls or []
    return SupportCallLog(
        started_at=call.started_at,
        ended_at=call.ended_at,
        duration_seconds=call.duration_seconds,
        intent=call.intent,
        outcome=call.outcome,
        transfer_reason=call.transfer_reason,
        line_note=call.line_note,
        cost_cents=call.cost_cents,
        model=call.model,
        is_test=call.is_test,
        tool_calls=[_tool_call(entry) for entry in entries[:TOOL_CALLS_SENT]],
        tool_calls_total=len(entries),
    )
