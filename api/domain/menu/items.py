"""Gemeinsame Bausteine der Karten-Tools: Optionen und der Zustand "aus" (docs/04).

`search_menu` und `get_item_details` lesen dieselben Kindtabellen und muessen
dasselbe antworten - ein Gericht, das in der Suche Optionen hat, hat sie in den
Details auch. Deshalb liegt die Logik hier und nicht zweimal nebeneinander.
"""

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.models import ItemOption, MenuItem
from api.schemas.menu import OptionGroup, OptionOut


def option_key(text: str) -> str:
    """Vergleichsform von Gruppen- und Optionsnamen: ohne Groß-/Kleinschreibung und Mehrfach-Leerzeichen.

    Import und draft_order vergleichen damit gleich. Sonst nähme der Import
    "Sauce/Erdnuss" und "sauce/erdnuss" als zwei Optionen an, und draft_order
    könnte nicht sagen, welche gemeint ist (Codex PR #124).
    """
    return " ".join(text.split()).casefold()


def is_sold_out(item: MenuItem, now: datetime) -> bool:
    """Ausverkauft bis `sold_out_until` (docs/06 §3). Kein Wert heisst verfuegbar."""
    return item.sold_out_until is not None and item.sold_out_until > now


def option_groups(
    session: Session, item_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, list[OptionGroup]]:
    """Optionen aller genannten Gerichte in einer Abfrage, nach Gruppe gebuendelt.

    Voreinstellung zuerst, damit der Agent sie als erste vorliest.
    """
    if not item_ids:
        return {}
    options = session.scalars(
        select(ItemOption)
        .where(ItemOption.menu_item_id.in_(item_ids))
        .order_by(
            ItemOption.group_name,
            ItemOption.is_default.desc(),
            ItemOption.option_name,
        )
    ).all()

    by_item: dict[uuid.UUID, dict[str, OptionGroup]] = {}
    for option in options:
        groups = by_item.setdefault(option.menu_item_id, {})
        group = groups.setdefault(
            option.group_name,
            OptionGroup(group=option.group_name, required=option.required, options=[]),
        )
        group.options.append(
            OptionOut(
                name=option.option_name,
                price_delta_cents=option.price_delta_cents,
                default=option.is_default,
            )
        )
    return {item_id: list(groups.values()) for item_id, groups in by_item.items()}
