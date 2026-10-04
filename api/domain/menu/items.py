"""Shared building blocks of the menu tools: options and the state "aus" (docs/04).

`search_menu` and `get_item_details` read the same child tables and must give
the same answer - a dish that has options in the search has them in the
details as well. That is why the logic lives here and not twice side by side.
"""

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.domain.menu.numberwords import CardFormat
from api.models import ItemOption, MenuItem
from api.schemas.menu import OptionGroup, OptionOut


def option_key(text: str) -> str:
    """Comparison form of group and option names: case-insensitive, without repeated spaces.

    Import and draft_order compare with it in the same way. Otherwise the
    import would take "Sauce/Erdnuss" and "sauce/erdnuss" as two options, and
    draft_order could not tell which one is meant (Codex PR #124).
    """
    return " ".join(text.split()).casefold()


def is_sold_out(item: MenuItem, now: datetime) -> bool:
    """Ausverkauft bis `sold_out_until` (docs/06 §3). Kein Wert heisst verfuegbar."""
    return item.sold_out_until is not None and item.sold_out_until > now


def active_numbers(session: Session, tenant_id: uuid.UUID) -> list[tuple[str, str]]:
    """Number and category of every active dish. One query; for a menu of a
    few hundred rows that is one index scan."""
    rows = session.execute(
        select(MenuItem.number, MenuItem.category).where(
            MenuItem.tenant_id == tenant_id, MenuItem.active.is_(True)
        )
    )
    return [(number, category) for number, category in rows]


def card_format(session: Session, tenant_id: uuid.UUID) -> CardFormat:
    """Prefixes and category words of the active menu (T-4.12).

    Which letters can stand in front of a number ("S12", "SM1") follows from
    the numbers in the database, never from the code (CLAUDE.md §2 rule 1).
    """
    return CardFormat.from_items(active_numbers(session, tenant_id))


def option_groups(
    session: Session, item_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, list[OptionGroup]]:
    """Options of all given dishes in one query, grouped by option group.

    The default comes first, so the agent reads it out first.
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
                reason=option.price_reason,
            )
        )
    return {item_id: list(groups.values()) for item_id, groups in by_item.items()}
