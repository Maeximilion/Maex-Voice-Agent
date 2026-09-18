"""Verträge der Karten-Tools search_menu und get_item_details (docs/04).

Die Suche liefert nie Text, den der Agent frei ausschmücken darf: jede Position
trägt ihre `menu_item_id`, und nur damit darf der Agent weiterarbeiten
(CLAUDE.md §2 Regel 2).
"""

import uuid
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

from api.schemas.common import ToolRequest

MATCH_TYPES = ("exact_number", "alias", "fuzzy_single", "ambiguous")
MAX_RESULTS = 3


class SearchMenuRequest(ToolRequest):
    query: str = Field(min_length=1, max_length=300)
    max_results: int = Field(default=MAX_RESULTS, ge=1, le=MAX_RESULTS)


class ItemDetailsRequest(ToolRequest):
    menu_item_id: uuid.UUID


class OptionChoice(BaseModel):
    name: str
    price_delta_cents: int
    default: bool


class OptionGroup(BaseModel):
    group: str
    required: bool
    options: list[OptionChoice] = Field(default_factory=list)


class MenuHit(BaseModel):
    menu_item_id: uuid.UUID
    number: str
    name: str
    price_cents: int
    sold_out: bool
    option_groups: list[OptionGroup] = Field(default_factory=list)


class MenuSearch(BaseModel):
    match_type: Literal["exact_number", "alias", "fuzzy_single", "ambiguous"]
    results: list[MenuHit] = Field(default_factory=list)
    say: str | None = Field(default=None, exclude=True)


class Allergens(BaseModel):
    """`known: false` heisst "keine Auskunft", nie "frei davon" (docs/04, CLAUDE.md §9)."""

    known: bool
    codes: list[str] = Field(default_factory=list)
    confirmed_at: date | None = None


class ItemDetails(BaseModel):
    menu_item_id: uuid.UUID
    number: str
    name: str
    price_cents: int
    sold_out: bool
    description: str | None = None
    allergens: Allergens
    option_groups: list[OptionGroup] = Field(default_factory=list)
    say: str | None = Field(default=None, exclude=True)
