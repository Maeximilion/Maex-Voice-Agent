"""The menu: search, details, numbers and splitting of what a caller orders.

The names below load on first use, not with the package (#983 point 5): a tool
that needs one submodule, such as scripts/call_log.py with `numberwords`, does
not pull in the SQLAlchemy ones.
"""

from importlib import import_module
from typing import TYPE_CHECKING, Any

_SOURCE = {
    "ItemNumber": "numberwords",
    "find_item_number": "numberwords",
    "find_item_number_ref": "numberwords",
    "find_marked_item_numbers": "numberwords",
    "find_numbers": "numberwords",
    "find_quantity": "numberwords",
    "get_item_details": "details",
    "has_item_number_marker": "numberwords",
    "is_sold_out": "items",
    "option_groups": "items",
    "parse_cardinal": "numberwords",
    "search_menu": "search",
    "split_positions": "split",
}

if TYPE_CHECKING:
    from api.domain.menu.details import get_item_details
    from api.domain.menu.items import is_sold_out, option_groups
    from api.domain.menu.numberwords import (
        ItemNumber,
        find_item_number,
        find_item_number_ref,
        find_marked_item_numbers,
        find_numbers,
        find_quantity,
        has_item_number_marker,
        parse_cardinal,
    )
    from api.domain.menu.search import search_menu
    from api.domain.menu.split import split_positions
else:

    def __getattr__(name: str) -> Any:
        # Any other name raises, so `from api.domain.menu import listing` goes
        # on to import the submodule of that name.
        if name not in _SOURCE:
            raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
        value = getattr(import_module(f"{__name__}.{_SOURCE[name]}"), name)
        globals()[name] = value
        return value


__all__ = [
    "ItemNumber",
    "find_item_number",
    "find_item_number_ref",
    "find_marked_item_numbers",
    "find_numbers",
    "find_quantity",
    "get_item_details",
    "has_item_number_marker",
    "is_sold_out",
    "option_groups",
    "parse_cardinal",
    "search_menu",
    "split_positions",
]
