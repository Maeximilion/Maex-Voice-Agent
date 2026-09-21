"""Verträge der Menü-Tools (docs/04 §search_menu)."""

import uuid
from typing import Literal

from pydantic import BaseModel, Field

from api.schemas.common import ToolRequest

MatchType = Literal["exact_number", "alias", "fuzzy_single", "ambiguous"]


class SearchMenuRequest(ToolRequest):
    query: str = Field(min_length=1, max_length=300)
    max_results: int = Field(default=3, ge=1, le=5)


class OptionOut(BaseModel):
    name: str
    price_delta_cents: int
    default: bool


class OptionGroup(BaseModel):
    group: str
    required: bool
    options: list[OptionOut]


class MenuHit(BaseModel):
    menu_item_id: uuid.UUID
    number: str
    name: str
    price_cents: int
    sold_out: bool
    option_groups: list[OptionGroup] = Field(default_factory=list)


class SearchResult(BaseModel):
    match_type: MatchType
    results: list[MenuHit]
    say: str | None = Field(default=None, exclude=True)
