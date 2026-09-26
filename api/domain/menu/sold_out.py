"""Schalter "Gericht aus" (T-4.8, docs/06 §3).

Ein Tap am Tablet stellt ein Gericht bis zum Ende des Betriebstags auf
ausverkauft (`menu_items.sold_out_until`). Ab dann liefert `search_menu` es mit
`sold_out: true` und dem Satz "heute aus", `draft_order` lehnt es ab, und der
Agent nennt bis zu zwei Gerichte derselben Kategorie als Alternative - nur, was
auf der Karte steht und heute zu haben ist (D8).

Betriebsschluss ist hier das Ende des Betriebstags (05:00 Ortszeit, core/time):
danach bestellt niemand mehr, und am naechsten Morgen ist das Gericht von
selbst wieder da, ohne dass jemand daran denken muss. Ein zweiter Tap nimmt den
Schalter zurueck ("Wieder da"), fuer den Irrtum und die Nachlieferung.

Jede Aenderung sperrt die Zeile und schreibt audit_log mit Wert davor und
danach, wie die Schalter der Kopfzeile (domain/status/config.py).
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
from api.domain.menu.numberwords import canonical_card
from api.models import AuditLog, MenuItem

ACTOR_TABLET = "gui:tablet"
ACTION_SOLD_OUT = "menu_item.sold_out_changed"
# Mehr als zwei Vorschlaege merkt sich am Telefon niemand (wie bei check_slot).
MAX_ALTERNATIVES = 2


@dataclass(frozen=True)
class DishSwitch:
    menu_item_id: uuid.UUID
    number: str
    name: str
    category: str
    sold_out: bool


def number_key(number: str) -> tuple[int, str]:
    """Kartenreihenfolge: 2 vor 12, 23 vor 23a. Nummern sind Text (docs/14)."""
    match = re.match(r"\d+", number)
    return (int(match.group()), number[match.end() :]) if match else (10**9, number)


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
    if said[0].isdigit():
        return canonical_card(item.number).startswith(canonical_card(said))
    return normalize_query(said) in normalize_query(item.name)


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
    """Fingerabdruck fuer den Ereignisstrom: aendert sich, sobald ein Gericht
    aus- oder wieder angeschaltet wird - auch am Morgen, wenn der Schalter
    von selbst ablaeuft."""
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
    """Aus bis zum Ende des Betriebstags, oder wieder da. Ein Tap, keine
    Rueckfrage (docs/06 §1 Regel 6: nur Loeschen und Stornieren fragen nach)."""
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
    """Bis zu zwei Gerichte derselben Kategorie, die heute zu haben sind -
    die naechsten nach der Nummer des ausverkauften, dann von vorn."""
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
