"""Lunch menus: dishes that are only sold inside the lunch window (T-4.13).

`menu_items.lunch_only` marks the dish (set by the import, docs/14),
`lunch_hours` holds the window per weekday in the tenant's local time. Both
come from the database, never from the code (CLAUDE.md §2 rule 1). A weekday
without a row has no lunch menus: until someone enters a window, a lunch menu
is not sold by phone at all.

What counts is the moment of the call, not the pickup time: an order is always
"as soon as possible" (`pickup_wait_minutes`). Outside the window
`search_menu` does not deliver a lunch menu (error `closed` with the sentence
from here), so its `menu_item_id` cannot enter an order; `draft_order` checks
again, because the window can close during a call.
"""

import uuid
from collections.abc import Sequence
from datetime import datetime, time
from functools import cached_property

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.core.errors import Closed
from api.core.time import tz
from api.domain.reservations.spoken import spoken_time
from api.models import LunchHours, MenuItem, Tenant

MESSAGE_CLOSED = "lunch menu outside the lunch window"
SAY_ONE = "{name} ist ein Mittagsmenü. Das gibt es heute {when}."
SAY_SEVERAL = "Die Mittagsmenüs gibt es heute {when}."
SAY_WINDOW = "nur von {start} bis {end}"
SAY_NOT_TODAY = "leider nicht"


class Lunch:
    """The lunch window at `now`, read on first use.

    Lazy on purpose: a search that touches no lunch menu costs no query.
    """

    def __init__(self, session: Session, tenant_id: uuid.UUID, now: datetime) -> None:
        self._session = session
        self._tenant_id = tenant_id
        self._now = now

    @cached_property
    def _local(self) -> datetime:
        tenant = self._session.get(Tenant, self._tenant_id)
        return self._now.astimezone(tz(tenant.timezone if tenant else None))

    @cached_property
    def window(self) -> tuple[time, time] | None:
        """Today's window in local time, None on a day without lunch menus."""
        row = self._session.execute(
            select(LunchHours.starts_at, LunchHours.ends_at).where(
                LunchHours.tenant_id == self._tenant_id,
                LunchHours.weekday == self._local.weekday(),
            )
        ).first()
        return None if row is None else (row.starts_at, row.ends_at)

    @cached_property
    def open(self) -> bool:
        if self.window is None:
            return False
        start, end = self.window
        return start <= self._local.time() < end

    def closed(self, item: MenuItem) -> bool:
        """Is this dish a lunch menu that is not sold right now?"""
        return item.lunch_only and not self.open

    def without_closed(self, items: Sequence[MenuItem]) -> list[MenuItem]:
        """The dishes that are sold right now. If lunch menus outside the
        window are all there is, that is `closed` with the sentence why."""
        sold = [item for item in items if not self.closed(item)]
        if items and not sold:
            raise Closed(MESSAGE_CLOSED, say=self.say(items))
        return sold

    def say(self, items: Sequence[MenuItem]) -> str:
        """Why the dish is not sold now. One dish is named, so the guest hears
        what was understood; several ("Menü eins" is M1 or VM1) are not."""
        if self.window is None:
            when = SAY_NOT_TODAY
        else:
            start, end = (
                spoken_time(datetime.combine(self._local.date(), t))
                for t in self.window
            )
            when = SAY_WINDOW.format(start=start, end=end)
        if len(items) == 1:
            return SAY_ONE.format(name=items[0].name, when=when)
        return SAY_SEVERAL.format(when=when)
