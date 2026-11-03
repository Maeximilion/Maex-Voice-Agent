"""The wait a guest is told: the standard, or today's wait while it holds (T-3.14).

The one reader of the wait. A wait for today carries the moment it ends, the end
of the business day, so nothing has to run at 05:00: read after that moment it
gives the standard. Same idea as `menu_items.sold_out_until`.
"""

from datetime import datetime

from api.models import ServiceConfig

STANDARD_FIELDS = {
    "pickup": "pickup_wait_standard_minutes",
    "delivery": "delivery_wait_standard_minutes",
}
TODAY_FIELDS = {
    "pickup": "pickup_wait_today_minutes",
    "delivery": "delivery_wait_today_minutes",
}
UNTIL_FIELDS = {
    "pickup": "pickup_wait_today_until",
    "delivery": "delivery_wait_today_until",
}


def standard_wait(config: ServiceConfig, service: str) -> int:
    return getattr(config, STANDARD_FIELDS[service])


def holds_today(config: ServiceConfig, service: str, now: datetime) -> bool:
    """Does a wait for today hold at `now`?"""
    until = getattr(config, UNTIL_FIELDS[service])
    return until is not None and now < until


def current_wait(config: ServiceConfig, service: str, now: datetime) -> int:
    """The wait at `now`, in minutes."""
    if holds_today(config, service, now):
        return getattr(config, TODAY_FIELDS[service])
    return standard_wait(config, service)
