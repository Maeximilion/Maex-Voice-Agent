"""Display of the key figures (T-8.4, docs/06 §4): German words and numbers.

The numbers come from `domain/calls/figures.py`; this file only formats them.
"""

from datetime import timedelta

from sqlalchemy.orm import Session

from api.core.time import business_day
from api.domain.calls.figures import Figures, span_figures
from api.models import Tenant

WEEK_DAYS = 7
HISTORY_DAYS = 14
DASH = "\N{EN DASH}"
WEEKDAYS = ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So")
LABELS = (
    "Genauigkeit",
    "Kosten je Bestellung",
    "An das Team übergeben",
    "Verpasste Anrufe",
)


def _percent(value: int | None) -> str:
    return DASH if value is None else f"{value} %"


def _euro(cents: int | None) -> str:
    if cents is None:
        return DASH
    return f"{cents // 100},{cents % 100:02d} €"


def values(figures: Figures) -> list[str]:
    """The four values as text, in the order of LABELS (docs/06 §4)."""
    return [
        _percent(figures.accuracy_percent),
        _euro(figures.cost_per_order_cents),
        _percent(figures.handed_over_percent),
        str(figures.missed),
    ]


def page(session: Session, tenant: Tenant) -> dict:
    today = business_day(tz_name=tenant.timezone)
    week = span_figures(session, tenant, today - timedelta(days=WEEK_DAYS - 1), today)
    history = []
    for back in range(HISTORY_DAYS):
        day = today - timedelta(days=back)
        figures = span_figures(session, tenant, day, day)
        history.append(
            {
                "day": f"{WEEKDAYS[day.weekday()]} {day:%d.%m.}",
                "calls": figures.calls,
                "orders": figures.orders,
                "values": values(figures),
                "unknown_cost": figures.unknown_cost_calls,
            }
        )
    return {
        "week": list(zip(LABELS, values(week), strict=True)),
        "week_unknown_cost": week.unknown_cost_calls,
        "labels": LABELS,
        "history": history,
    }
