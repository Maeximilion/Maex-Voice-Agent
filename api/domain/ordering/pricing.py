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


def row_cents(unit_price_cents: int, options: Sequence[dict]) -> int:
    """Stueckpreis einer Position: Grundpreis plus Optionen.

    `order_items.unit_price_cents` haelt nur den Grundpreis, die Optionen tragen
    ihre Differenz selbst. Wer eine gespeicherte Position rechnet, nimmt diese
    Funktion und nie den Kartenpreis von jetzt (Korrektur im Tablet, T-4.7).
    """
    return unit_price_cents + sum(o["price_delta_cents"] for o in options)


def unit_cents(line: Line) -> int:
    return row_cents(line.item.price_cents, line.options)


def items_total_cents(lines: Sequence[Line]) -> int:
    return sum(unit_cents(line) * line.quantity for line in lines)
