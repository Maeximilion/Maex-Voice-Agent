"""The calls of one business day, for the call list (T-8.7, docs/06 §4).

Read only. The number is the caller id as the call log holds it; the deletion
job takes it after its period (docs/03 "Löschkonzept"), and how much of it a
screen shows is the screen's decision.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta

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
    session.add(
        AuditLog(
            tenant_id=tenant.id,
            actor=actor,
            action=SUPPORT_READ,
            entity="call",
            payload={
                "reason": reason,
                "reference": reference,
                "day": day.isoformat(),
            },
        )
    )
    session.commit()
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
