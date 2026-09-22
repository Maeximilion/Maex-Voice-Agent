"""Summe aus Positionen und Optionen. Cent-Integer, sonst nichts (CLAUDE.md §8).

Die Pauschale für Lieferung kommt mit T-6.5 dazu.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from api.models import MenuItem


@dataclass(frozen=True)
class Line:
    """Eine geprüfte Position. `options` in der Form von order_items.options."""

    item: MenuItem
    quantity: int
    options: list[dict]
    note: str | None


def unit_cents(line: Line) -> int:
    return line.item.price_cents + sum(o["price_delta_cents"] for o in line.options)


def items_total_cents(lines: Sequence[Line]) -> int:
    return sum(unit_cents(line) * line.quantity for line in lines)
