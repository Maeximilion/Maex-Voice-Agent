"""Der Vorlesetext, deterministisch aus dem Entwurf. Der Agent liest ihn wörtlich vor."""

from collections.abc import Sequence
from dataclasses import dataclass

from api.domain.reservations.spoken import COUNTS
from api.models import Order


@dataclass(frozen=True)
class SpokenLine:
    """Was von einer Position vorgelesen wird.

    Nummer und Name sind der Stand der Karte beim Anlegen, nicht der aktuelle:
    ein Replay nach einem Import sagt dasselbe Gericht (Codex PR #124).
    """

    number: str
    name: str
    quantity: int
    options: list[dict]
    note: str | None


def readback(order: Order, lines: Sequence[SpokenLine]) -> str:
    """Bezug für "in etwa N Minuten" ist der Anlagezeitpunkt, nicht die Uhr:
    derselbe Schlüssel liefert denselben Satz."""
    satz = " ".join(_spoken_line(line) for line in lines)
    if order.note:
        # The note of the whole order after its dishes (D24), repeated like
        # the note of a line.
        satz += f" Für die ganze Bestellung: {order.note}."
    satz += f" Macht {spoken_euro(order.total_cents)}"
    if order.ready_at is not None:
        # Abrunden: ready_at ist aufgerundet, die Differenz liegt in
        # [Wartezeit, Wartezeit + 1 Minute) und ergibt so genau die Wartezeit.
        minutes = int((order.ready_at - order.created_at).total_seconds() // 60)
        satz += f", abholbereit in etwa {minutes} Minuten"
    return satz + f", auf den Namen {order.customer_name}. Passt das so?"


def _spoken_line(line: SpokenLine) -> str:
    text = (
        f"{spoken_times(line.quantity).capitalize()} Nummer {line.number} {line.name}"
    )
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
    """1730 → "17 Euro 30", 700 → "7 Euro", 50 → "50 Cent".

    Two plain numbers and no decimal comma: a voice that does not know prices
    reads "17,30 Euro" as "siebzehn Komma drei null Euro" (eval abholung_0076).
    Single cents behind euros carry their unit, "17 Euro 5 Cent": a bare "5"
    is easily taken for "50" on the phone.
    """
    euros, rest = divmod(cents, 100)
    if rest == 0:
        return f"{_count(euros)} Euro"
    if euros == 0:
        return f"{_count(rest)} Cent"
    if rest >= 10:
        return f"{_count(euros)} Euro {rest}"
    return f"{_count(euros)} Euro {_count(rest)} Cent"


def _count(n: int) -> str:
    """A lone 1 as a word: the test-phase voice reads "1 Euro" as "eins Euro"."""
    return "ein" if n == 1 else str(n)
