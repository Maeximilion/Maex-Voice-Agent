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
    # Warum die Option mehr kostet, aus der Karte (T-4.10). None: nichts gepflegt.
    reason: str | None = None


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


class Wish(BaseModel):
    """Ein Wunsch zur Position (T-4.10, domain/menu/wishes.py). `kind`: `note`
    (Weglassen), `option` (steht auf der Karte, mit Aufpreis und Grund),
    `allergy` (Hinweis ohne Zusage), `unknown` (nicht angeboten, D8), `open`
    (erst nach der Wahl des Gerichts einzuordnen)."""

    text: str
    kind: Literal["note", "option", "allergy", "unknown", "open"]
    group: str | None = None
    option: str | None = None
    price_delta_cents: int | None = None
    reason: str | None = None
    # Bei einer Allergie die Zutat aus den Worten des Gastes (E14). None: der
    # Agent fragt nach, wogegen.
    ingredient: str | None = None


class SearchResult(BaseModel):
    match_type: MatchType
    results: list[MenuHit]
    wish: Wish | None = None
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
