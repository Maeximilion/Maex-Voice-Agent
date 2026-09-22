"""Der Vorlesetext, deterministisch aus dem Entwurf. Der Agent liest ihn wörtlich vor."""

from collections.abc import Sequence

from api.domain.ordering.pricing import Line
from api.domain.reservations.spoken import COUNTS
from api.models import Order


def readback(order: Order, lines: Sequence[Line]) -> str:
    """Bezug für "in etwa N Minuten" ist der Anlagezeitpunkt, nicht die Uhr:
    derselbe Schlüssel liefert denselben Satz."""
    satz = " ".join(_spoken_line(line) for line in lines)
    satz += f" Macht {spoken_euro(order.total_cents)}"
    if order.ready_at is not None:
        # Abrunden: ready_at ist aufgerundet, die Differenz liegt in
        # [Wartezeit, Wartezeit + 1 Minute) und ergibt so genau die Wartezeit.
        minutes = int((order.ready_at - order.created_at).total_seconds() // 60)
        satz += f", abholbereit in etwa {minutes} Minuten"
    return satz + f", auf den Namen {order.customer_name}. Passt das so?"


def _spoken_line(line: Line) -> str:
    text = f"{spoken_times(line.quantity).capitalize()} Nummer {line.item.number} {line.item.name}"
    if line.options:
        text += " mit " + " und ".join(o["option"] for o in line.options)
    if line.note:
        text += f", {line.note}"
    return text + "."


def spoken_times(n: int) -> str:
    """1 → "einmal", 2 → "zweimal". Über zwölf als Ziffer, das liest die Stimme sauber."""
    if n == 1:
        return "einmal"
    word = COUNTS.get(n)
    return f"{word}mal" if word else f"{n} mal"


def spoken_euro(cents: int) -> str:
    """1730 → "17,30 Euro", 700 → "7 Euro"."""
    euros, rest = divmod(cents, 100)
    return f"{euros} Euro" if rest == 0 else f"{euros},{rest:02d} Euro"
