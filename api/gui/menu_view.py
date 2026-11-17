"""Display of the tab "Menü" (#571, plan number 9, docs/06 §4): German words,
amounts as euros, and the three fields of the address (`q`, `hinweis`, `seite`).

The dishes, the price in cents and both notes come from
`domain/menu/listing.py`; this file decides nothing about a dish. An address
nobody offers (a filter that does not exist, a page that is no number) shows
the list without it, never an error.
"""

from typing import Any
from urllib.parse import urlencode

from sqlalchemy.orm import Session

from api.domain.menu import listing
from api.domain.menu.sold_out import QUERY_MAX
from api.gui.orders_view import euro
from api.models import Tenant

PATH = "/gui/menue"
# The value in the address -> the filter of the domain and its words.
FILTERS = {
    "allergene": (listing.ONLY_ALLERGENS, "Allergene fehlen"),
    "preis": (listing.ONLY_PRICE, "Preisabweichung offen"),
}
# The digits read of a page number: far more pages than a menu has. A longer
# number is past the end.
PAGE_DIGITS = 6


def _page_number(text: str) -> int:
    # ASCII only: "²" is a digit to `isdigit` and no number to `int`.
    if not (text.isascii() and text.isdigit()):
        return 1
    # Never converted beyond that: `int` refuses a run of thousands of digits.
    return int(text) if len(text) <= PAGE_DIGITS else 10**PAGE_DIGITS


def page(
    session: Session, tenant: Tenant, q: str, hinweis: str, seite: str
) -> dict[str, Any]:
    only = hinweis if hinweis in FILTERS else ""
    shown = listing.listing(
        session,
        tenant.id,
        q,
        FILTERS[only][0] if only else None,
        _page_number(seite),
    )
    # The text as the domain compared it (cut, without what nobody sees), so
    # the field and the links of the pages show what was searched.
    query = shown.query

    def address(number: int) -> str:
        kept = {"q": query, "hinweis": only, "seite": str(number)}
        return f"{PATH}?" + urlencode({k: v for k, v in kept.items() if v})

    narrowed = bool(query or only)
    return {
        "query": query,
        "query_max": QUERY_MAX,
        "filters": [
            (value, label, value == only) for value, (_, label) in FILTERS.items()
        ],
        "rows": [
            {
                "number": row.number,
                "name": row.name,
                "price": euro(row.price_cents),
                "allergens_missing": row.allergens_missing,
                # 0 is a price the register can say: None alone means no note.
                "register": None
                if row.register_cents is None
                else euro(row.register_cents),
            }
            for row in shown.rows
        ],
        "narrowed": narrowed,
        # What a search or a filter left, beside the counts of the whole menu.
        "found": f"{shown.matching} von {shown.items} "
        + ("Gericht" if shown.items == 1 else "Gerichten"),
        "items": shown.items,
        "allergens_missing": shown.allergens_missing,
        "price_differences": shown.price_differences,
        "page": shown.page,
        "pages": shown.pages,
        "previous": address(shown.page - 1) if shown.page > 1 else None,
        "next": address(shown.page + 1) if shown.page < shown.pages else None,
    }
