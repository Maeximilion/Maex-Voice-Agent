"""Gemeinsame Bausteine der Karten-Tools: Optionen und der Zustand "aus" (docs/04).

Beide Tools lesen dieselben Kindtabellen. Sie liegen deshalb hier und nicht
zweimal nebeneinander in search.py und details.py.
"""

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.models import ItemOption, MenuItem
from api.schemas.menu import OptionChoice, OptionGroup


def is_sold_out(item: MenuItem, now: datetime) -> bool:
    """Ausverkauft bis `sold_out_until` (docs/06 §3). Kein Wert heisst verfuegbar."""
    return item.sold_out_until is not None and item.sold_out_until > now


def option_groups(
    session: Session, item_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, list[OptionGroup]]:
    """Optionen aller genannten Gerichte in einer Abfrage, nach Gruppe gebuendelt.

    Eine Gruppe ist Pflicht, sobald eine ihrer Optionen Pflicht ist: die Frage
    "mit welcher Sauce?" haengt an der Gruppe, nicht an der einzelnen Sauce.
    """
    if not item_ids:
        return {}
    rows = session.scalars(
        select(ItemOption)
        .where(ItemOption.menu_item_id.in_(item_ids))
        .order_by(
            ItemOption.menu_item_id,
            ItemOption.group_name,
            ItemOption.is_default.desc(),
            ItemOption.price_delta_cents,
            ItemOption.option_name,
        )
    ).all()

    groups: dict[uuid.UUID, dict[str, OptionGroup]] = {}
    for row in rows:
        per_item = groups.setdefault(row.menu_item_id, {})
        group = per_item.get(row.group_name)
        if group is None:
            group = OptionGroup(group=row.group_name, required=False)
            per_item[row.group_name] = group
        group.required = group.required or row.required
        group.options.append(
            OptionChoice(
                name=row.option_name,
                price_delta_cents=row.price_delta_cents,
                default=row.is_default,
            )
        )
    return {item_id: list(per_item.values()) for item_id, per_item in groups.items()}
