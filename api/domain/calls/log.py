"""The calls of one business day, for the call list (T-8.7, docs/06 §4).

Read only. The number is the caller id as the call log holds it; the deletion
job takes it after its period (docs/03 "Löschkonzept"), and how much of it a
screen shows is the screen's decision.
"""

from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.core.time import business_day_bounds_utc
from api.models import Call, Tenant


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
        Call.tenant_id == tenant.id, Call.started_at >= start, Call.started_at < end
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
