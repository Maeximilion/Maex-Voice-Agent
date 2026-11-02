"""Key figures of the operations view (T-8.4, docs/06 §4).

Four values over a span of business days, read from rows that exist already
(calls, orders, `order.corrected` in the audit log), no table of their own:

- accuracy: share of taken orders the team did not have to correct;
- cost per order: model cost of all calls of the span over the orders taken;
- handed to the team: share of calls that ended `transferred` or `callback`;
- missed calls: calls that ended `abandoned` or `error`.

A value with nothing to divide by is None (an empty day shows a dash, not a
zero that reads as "all good" or "all bad"). Money stays integer cents.
"""

from dataclasses import dataclass
from datetime import date

from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session

from api.core.time import business_day_bounds_utc
from api.domain.ordering.labels import ACTION_CORRECTED
from api.models import AuditLog, Call, Order, Tenant

ORDERS_TAKEN = ("confirmed", "approved", "handed_over")
HANDED_OVER = ("transferred", "callback")
MISSED = ("abandoned", "error")


@dataclass(frozen=True)
class Figures:
    calls: int
    orders: int
    corrected: int
    handed_over: int
    missed: int
    cost_cents: int
    unknown_cost_calls: int

    @property
    def accuracy_percent(self) -> int | None:
        if not self.orders:
            return None
        return _percent(self.orders - self.corrected, self.orders)

    @property
    def cost_per_order_cents(self) -> int | None:
        if not self.orders:
            return None
        return (2 * self.cost_cents + self.orders) // (2 * self.orders)

    @property
    def handed_over_percent(self) -> int | None:
        return _percent(self.handed_over, self.calls) if self.calls else None


def _percent(part: int, whole: int) -> int:
    return (200 * part + whole) // (2 * whole)


def span_figures(
    session: Session, tenant: Tenant, first_day: date, last_day: date
) -> Figures:
    """The four values for the business days first_day to last_day, both included."""
    start, _ = business_day_bounds_utc(first_day, tenant.timezone)
    _, end = business_day_bounds_utc(last_day, tenant.timezone)
    outcomes = dict(
        session.execute(
            select(Call.outcome, func.count())
            .where(
                Call.tenant_id == tenant.id,
                Call.started_at >= start,
                Call.started_at < end,
            )
            .group_by(Call.outcome)
        ).all()
    )
    cost, unknown = session.execute(
        select(
            func.coalesce(func.sum(Call.cost_cents), 0),
            func.count().filter(Call.cost_cents.is_(None)),
        ).where(
            Call.tenant_id == tenant.id,
            Call.started_at >= start,
            Call.started_at < end,
        )
    ).one()
    taken = (
        Order.tenant_id == tenant.id,
        Order.status.in_(ORDERS_TAKEN),
        Order.created_at >= start,
        Order.created_at < end,
    )
    orders = session.scalar(select(func.count()).select_from(Order).where(*taken))
    corrected = session.scalar(
        select(func.count())
        .select_from(Order)
        .where(
            *taken,
            exists().where(
                AuditLog.tenant_id == Order.tenant_id,
                AuditLog.action == ACTION_CORRECTED,
                AuditLog.entity_id == Order.id,
            ),
        )
    )
    return Figures(
        calls=sum(outcomes.values()),
        orders=orders or 0,
        corrected=corrected or 0,
        handed_over=sum(outcomes.get(o, 0) for o in HANDED_OVER),
        missed=sum(outcomes.get(o, 0) for o in MISSED),
        cost_cents=int(cost or 0),
        unknown_cost_calls=unknown,
    )
