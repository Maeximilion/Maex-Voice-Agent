"""Verträge der Menü-Tools (docs/04 §search_menu, §get_item_details)."""

import uuid
from datetime import date
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


class ItemDetailsRequest(ToolRequest):
    menu_item_id: uuid.UUID
    # Pflichtfeld ohne Vorgabe: fragt der Gast nach Allergenen, haengt daran der
    # Satz zum Rueckruf (docs/04, docs/05 §3). Ein Vorgabewert waere hier die
    # falsche Sicherheit - fehlt das Feld, ist die Antwort invalid_input und der
    # Agent merkt es, statt still die Auskunft zu verlieren.
    allergen_question: bool


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
