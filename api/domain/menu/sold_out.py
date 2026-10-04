"""Switch "Gericht aus" (T-4.8, docs/06 §3).

One tap on the tablet marks a dish as sold out until the end of the business
day (`menu_items.sold_out_until`). From then on `search_menu` returns it with
`sold_out: true` and the sentence "heute aus", `draft_order` rejects it, and
the agent names up to two dishes of the same category as an alternative - only
what is on the menu and available today (D8).

Closing time here is the end of the business day (05:00 local time,
core/time): nobody orders after that, and the next morning the dish is back by
itself without anyone having to remember. A second tap takes the switch back
("Wieder da"), for a mistake or a late delivery.

Every change locks the row and writes audit_log with the value before and
after, like the switches in the header (domain/status/config.py).
"""

import hashlib
import re
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.core import time as clock
from api.core.errors import NotFound
from api.core.time import business_day, business_day_bounds_utc
from api.domain.menu.items import is_sold_out
from api.domain.menu.normalize import normalize_query
from api.domain.menu.numberwords import CARD_PARTS, canonical_card
from api.models import AuditLog, MenuItem

ACTOR_TABLET = "gui:tablet"
ACTION_SOLD_OUT = "menu_item.sold_out_changed"
# Nobody remembers more than two suggestions on the phone (as with check_slot).
MAX_ALTERNATIVES = 2


@dataclass(frozen=True)
class DishSwitch:
    menu_item_id: uuid.UUID
    number: str
    name: str
    category: str
    sold_out: bool


# A number in the search field: a digit first, also after a prefix ("s1", T-4.12).
_NUMBER_START = re.compile(r"[a-z]{0,2}\d")


def number_key(number: str) -> tuple[str, int, str]:
    """Menu order: 2 before 12, 23 before 23a, numbers without a prefix before
    S2 before S12 before SM1. Numbers are text (docs/14)."""
    match = CARD_PARTS.fullmatch(number.lower())
    if match is None:
        return ("\uffff", 10**9, number)
    return (match.group(1), int(match.group(2)), match.group(3))


def _active(session: Session, tenant_id: uuid.UUID) -> list[MenuItem]:
    items = session.scalars(
        select(MenuItem).where(
            MenuItem.tenant_id == tenant_id, MenuItem.active.is_(True)
        )
    )
    return sorted(items, key=lambda i: number_key(i.number))


def _matches(item: MenuItem, query: str) -> bool:
    """Suchfeld am Tablet: Nummer ("7" findet 07) oder ein Teil des Namens."""
    said = query.strip()
    if not said:
        return True
    # A leading digit can also be a name ("8 Kostbarkeiten", Codex PR #146).
    by_number = bool(_NUMBER_START.match(said.lower())) and canonical_card(
        item.number
    ).startswith(canonical_card(said))
    if by_number:
        return True
    # normalize_query drops numbers: "8 Kostbar" becomes "kostbar", "2" becomes
    # empty - and an empty text must not match every name.
    name_part = normalize_query(said)
    return bool(name_part) and name_part in normalize_query(item.name)


def list_switches(
    session: Session,
    tenant_id: uuid.UUID,
    query: str = "",
    now: datetime | None = None,
) -> list[DishSwitch]:
    """Aktive Gerichte fuer die Liste am Tablet, in Kartenreihenfolge."""
    now = now or clock.utcnow()
    return [
        DishSwitch(
            menu_item_id=item.id,
            number=item.number,
            name=item.name,
            category=item.category,
            sold_out=is_sold_out(item, now),
        )
        for item in _active(session, tenant_id)
        if _matches(item, query)
    ]


def sold_out_count(
    session: Session, tenant_id: uuid.UUID, now: datetime | None = None
) -> int:
    """Wie viele Gerichte heute aus sind - fuer die Kachel in der Kopfzeile."""
    now = now or clock.utcnow()
    return len(_sold_out_ids(session, tenant_id, now))


def _sold_out_ids(
    session: Session, tenant_id: uuid.UUID, now: datetime
) -> list[uuid.UUID]:
    return sorted(
        session.scalars(
            select(MenuItem.id).where(
                MenuItem.tenant_id == tenant_id,
                MenuItem.active.is_(True),
                MenuItem.sold_out_until > now,
            )
        )
    )


def sold_out_change_token(
    session: Session, tenant_id: uuid.UUID, now: datetime | None = None
) -> str:
    """Fingerprint for the event stream: changes as soon as a dish is switched
    off or on again - also in the morning, when the switch expires by
    itself."""
    ids = _sold_out_ids(session, tenant_id, now or clock.utcnow())
    return hashlib.sha1(",".join(map(str, ids)).encode()).hexdigest()[:12]


def set_sold_out(
    session: Session,
    tenant_id: uuid.UUID,
    menu_item_id: uuid.UUID,
    sold_out: bool,
    tz_name: str,
    now: datetime | None = None,
    actor: str = ACTOR_TABLET,
) -> MenuItem:
    """Off until the end of the business day, or back again. One tap, no
    confirmation (docs/06 §1 rule 6: only delete and cancel ask back)."""
    now = now or clock.utcnow()
    item = session.scalars(
        select(MenuItem)
        .where(
            MenuItem.tenant_id == tenant_id,
            MenuItem.id == menu_item_id,
            MenuItem.active.is_(True),
        )
        .with_for_update()
    ).first()
    if item is None:
        raise NotFound("Gericht unbekannt")
    was = is_sold_out(item, now)
    if was != sold_out:
        until = (
            business_day_bounds_utc(business_day(now, tz_name), tz_name)[1]
            if sold_out
            else None
        )
        session.add(
            AuditLog(
                tenant_id=tenant_id,
                actor=actor,
                action=ACTION_SOLD_OUT,
                entity="menu_item",
                entity_id=item.id,
                payload={
                    "number": item.number,
                    "from": was,
                    "to": sold_out,
                    "until": until.isoformat() if until else None,
                },
            )
        )
        item.sold_out_until = until
    session.commit()
    return item


def alternatives(session: Session, item: MenuItem, now: datetime) -> list[MenuItem]:
    """Up to two dishes of the same category that are available today - the
    next ones after the number of the sold-out dish, then from the start."""
    same = [
        other
        for other in _active(session, item.tenant_id)
        if other.category == item.category
        and other.id != item.id
        and not is_sold_out(other, now)
    ]
    key = number_key(item.number)
    after = [o for o in same if number_key(o.number) > key]
    before = [o for o in same if number_key(o.number) <= key]
    return (after + before)[:MAX_ALTERNATIVES]
