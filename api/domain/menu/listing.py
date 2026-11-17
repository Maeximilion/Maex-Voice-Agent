"""The menu as the tab "Menü" reads it (#571, plan number 9, docs/06 §4).

The active dishes in card order, a search by number or name, and the two notes
a row can carry. Both are decided here and nowhere in `gui/`, because they say
what the agent does with a dish (CLAUDE.md §2 rule 1):

- `allergens_missing`: the dish has no row in `item_allergens`, so
  `get_item_details` gives no allergen information for it
  (`details.without_allergens`). No row never means "contains none"
  (docs/03), and this module never names an allergen.
- `register_cents`: the register's price where the last price check found
  another one and the team has not answered (`price_check.open_differences`).
  The agent keeps saying `price_cents` until somebody answers on the board.

It only reads: no price, no allergen and no audit row is written from here.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.domain.menu.details import without_allergens
from api.domain.menu.price_check import open_differences
from api.domain.menu.sold_out import matches, number_key, searched
from api.models import MenuItem

# Rows on one page: a menu of a few hundred dishes is read whole from the
# database (as the list "Gericht aus" does), but never sent whole.
PAGE_SIZE = 50
# The filters: only the dishes that carry that note.
ONLY_ALLERGENS = "allergens"
ONLY_PRICE = "price"


@dataclass(frozen=True)
class Row:
    menu_item_id: uuid.UUID
    number: str
    name: str
    price_cents: int
    allergens_missing: bool
    # The register's open price, None without a difference. 0 is a price.
    register_cents: int | None


@dataclass(frozen=True)
class Listing:
    rows: list[Row]
    # The search text as it was compared (`sold_out.searched`), to show again.
    query: str
    page: int
    pages: int
    # Dishes the search and the filter leave, over all pages.
    matching: int
    # The whole active menu, whatever is searched.
    items: int
    allergens_missing: int
    price_differences: int


def listing(
    session: Session,
    tenant_id: uuid.UUID,
    query: str = "",
    only: str | None = None,
    page: int = 1,
) -> Listing:
    """One page of the active menu. A page that does not exist is the nearest
    one that does; a filter nobody offers filters nothing."""
    query = searched(query)
    items = session.execute(
        select(MenuItem.id, MenuItem.number, MenuItem.name, MenuItem.price_cents).where(
            MenuItem.tenant_id == tenant_id, MenuItem.active.is_(True)
        )
    ).all()
    unknown = without_allergens(session, tenant_id)
    register = {
        d.menu_item_id: d.register_cents for d in open_differences(session, tenant_id)
    }
    rows = [
        Row(
            menu_item_id=item_id,
            number=number,
            name=name,
            price_cents=price_cents,
            allergens_missing=item_id in unknown,
            register_cents=register.get(item_id),
        )
        # The number itself breaks a tie ("07" and "7" are one place in card
        # order): without it two requests could cut the pages differently.
        for item_id, number, name, price_cents in sorted(
            items, key=lambda item: (number_key(item.number), item.number)
        )
        if matches(number, name, query)
    ]
    if only == ONLY_ALLERGENS:
        rows = [row for row in rows if row.allergens_missing]
    elif only == ONLY_PRICE:
        rows = [row for row in rows if row.register_cents is not None]
    pages = max(1, -(-len(rows) // PAGE_SIZE))
    page = min(max(page, 1), pages)
    return Listing(
        rows=rows[(page - 1) * PAGE_SIZE : page * PAGE_SIZE],
        query=query,
        page=page,
        pages=pages,
        matching=len(rows),
        items=len(items),
        # Counted over the dishes read above: a dish switched off between the
        # statements is in neither count.
        allergens_missing=sum(item.id in unknown for item in items),
        price_differences=sum(item.id in register for item in items),
    )
