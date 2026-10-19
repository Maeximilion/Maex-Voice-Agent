"""Deutsche Zahlwörter und Mengen in Zahlen (docs/11 §menu).

Reine Funktion: keine DB, keine HTTP, keine Konfiguration. Was hier entschieden
wird, entscheidet sich allein am Text, den die Spracherkennung liefert.

Drei Aufgaben, die am Telefon auseinanderfallen:

- `parse_cardinal("dreiundzwanzig")` — der ganze Text **ist** die Zahl
- `find_item_number("Nummer vierzig sieben")` — die Zahl **steckt** im Satz
- `find_quantity("zweimal die Frühlingsrollen")` — wie oft, nicht was

Die Spracherkennung liefert dieselbe Zahl in mehreren Gestalten: als Ziffer
("23"), als ein Wort ("dreiundzwanzig"), auseinandergeschrieben ("drei und
zwanzig") oder ziffernweise gesprochen ("vierzig sieben"). Alle vier Formen
ergeben hier dieselbe Zahl.

**Kein Treffer ergibt `None`, nie eine Vermutung** (CLAUDE.md §2 Regel 2). Ob
daraus eine Rückfrage, eine Stufe der Verständnis-Leiter oder ein Abbruch wird,
entscheidet der Aufrufer, nicht dieses Modul.
"""

import re
from dataclasses import dataclass

# Obergrenze: Speisekarten-Nummern und Mengen bleiben dreistellig. Alles darüber
# ist am Telefon kein Zahlwort mehr, sondern eine Ziffernfolge.
MAX_VALUE = 999

_UMLAUTS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})

UNITS = {
    "null": 0,
    "eins": 1,
    "ein": 1,
    "eine": 1,
    "einen": 1,
    "einem": 1,
    "einer": 1,
    "zwei": 2,
    "zwo": 2,  # am Telefon üblich, um zwei und drei zu unterscheiden
    "drei": 3,
    "vier": 4,
    "fuenf": 5,
    "sechs": 6,
    "sieben": 7,
    "acht": 8,
    "neun": 9,
}
TEENS = {
    "zehn": 10,
    "elf": 11,
    "zwoelf": 12,
    "dreizehn": 13,
    "vierzehn": 14,
    "fuenfzehn": 15,
    "sechzehn": 16,
    "siebzehn": 17,
    "achtzehn": 18,
    "neunzehn": 19,
    # Formen, die die Spracherkennung regelmäßig so ausgibt
    "sechszehn": 16,
    "siebenzehn": 17,
}
TENS = {
    "zwanzig": 20,
    "dreissig": 30,
    "vierzig": 40,
    "fuenfzig": 50,
    "sechzig": 60,
    "siebzig": 70,
    "achtzig": 80,
    "neunzig": 90,
    "sechszig": 60,
    "siebenzig": 70,
}
HUNDRED = "hundert"

# Bloße Artikel. "ein Tisch" ist keine Eins, "einmal" schon. Ohne diese Ausnahme
# fände `find_item_number` in fast jedem Satz eine Eins.
ARTICLES = frozenset({"ein", "eine", "einen", "einem", "einer"})

# Mengen-Marker: ohne einen davon gibt es keine Menge, sondern nur eine Zahl.
_QUANTITY_SUFFIX = re.compile(r"^(.+?)mal$")
_QUANTITY_NOUNS = frozenset({"mal", "x", "portion", "portionen", "stueck", "stk", "st"})
_ITEM_NUMBER_MARKERS = frozenset({"nummer", "nr", "no", "position", "pos"})

# Satzzeichen trennen zwei Angaben: "Nummer 20, eine Portion" ist die 20 mit
# einer Portion, nicht die 21 (Codex-Review PR #105, P1). Der Bindestrich steht
# bewusst nicht dabei, der verbindet.
PUNCTUATION = frozenset(".,;:!?")

_TOKEN = re.compile(r"\d+|[a-z]+|[.,;:!?]")


@dataclass(frozen=True)
class _Span:
    """Eine gefundene Zahl mit ihrer Lage im Satz. Die Lage braucht, wer wissen
    will, ob diese Zahl schon als Menge vergeben ist."""

    start: int
    end: int
    value: int

    def overlaps(self, other: "_Span") -> bool:
        return self.start < other.end and other.start < self.end


def fold(text: str) -> str:
    """Kleinschreibung plus Umlaut-Ersatzschreibung. Am Telefon klingt "fünf" wie
    "fuenf"; welche Schreibweise die Erkennung liefert, ist Zufall."""
    return text.lower().translate(_UMLAUTS)


def _below_hundred(word: str) -> int | None:
    """Ein einzelnes Wort unter hundert, auch zusammengesetzt ("dreiundzwanzig")."""
    if word in TEENS:
        return TEENS[word]
    if word in TENS:
        return TENS[word]
    if word in UNITS:
        return UNITS[word]
    unit, sep, ten = word.partition("und")
    if sep and unit in UNITS and ten in TENS and UNITS[unit] < 10:
        return UNITS[unit] + TENS[ten]
    return None


def _word_value(word: str) -> int | None:
    """Ein Wort als Zahl, inklusive Hunderter ("zweihundertdreiundzwanzig")."""
    if word.isdigit():
        value = int(word)
        return value if value <= MAX_VALUE else None
    if word == HUNDRED:
        return 100
    before, sep, after = word.partition(HUNDRED)
    if not sep:
        return _below_hundred(word)
    hundreds = 1 if before in ("", "ein") else _below_hundred(before)
    if hundreds is None or not 1 <= hundreds <= 9:
        return None
    rest = 0 if not after else _below_hundred(after)
    return None if rest is None else hundreds * 100 + rest


def _tokens(text: str) -> list[str]:
    return _TOKEN.findall(fold(text))


def _scan(tokens: list[str], start: int) -> _Span | None:
    """Längste Zahl ab Position `start`.

    Zusammengesetzt wird nur, was sich am Telefon auch zusammen anhört:
    "vierzig sieben" (Zehner plus Einer), "drei und zwanzig", "zwei hundert".
    "zwei drei" bleibt zwei und drei -- daraus 23 zu machen wäre geraten.

    Zwei harte Grenzen: ein Satzzeichen und ein Artikel. Ohne sie wuchs
    "Nummer 20, eine Portion" zur 21, weil die Regel für ziffernweise
    gesprochene Zahlen über das Komma und über das "eine" der Mengenangabe
    hinweggriff (Codex-Review PR #105, P1).
    """
    i = start
    total: int | None = None
    while i < len(tokens):
        token = tokens[i]
        if token in PUNCTUATION:
            break
        if token == "und" and total is not None and total < 10:
            # "ein und zwanzig": der Zehner muss folgen, sonst war es ein normales "und"
            if i + 1 < len(tokens) and _word_value(tokens[i + 1]) in TENS.values():
                i += 1
                continue
            break
        if total is not None and token in ARTICLES:
            break

        value = _word_value(token)
        if value is None:
            break
        if total is None:
            total = value
        elif value == 100 and total <= 9:
            total *= 100
        elif total % 10 == 0 and total >= 20 and 1 <= value <= 9:
            total += value  # "vierzig sieben"
        elif total < 10 and value % 10 == 0 and 20 <= value <= 90:
            total += value  # "drei und zwanzig", das "und" wurde übersprungen
        elif total % 100 == 0 and total >= 100 and value < 100:
            total += value  # "zweihundert dreiundzwanzig"
        else:
            break
        i += 1
    if total is None or total > MAX_VALUE:
        return None
    return _Span(start, i, total)


def _scan_ending_at(tokens: list[str], end: int) -> _Span | None:
    """Zahl, die unmittelbar **vor** `end` endet ("drei Portionen", "zwei mal")."""
    for start in range(max(0, end - 3), end + 1):
        span = _scan(tokens, start)
        if span is not None and span.end == end + 1:
            return span
    return None


def _number_spans(tokens: list[str]) -> list[_Span]:
    """Alle genannten Zahlen mit ihrer Lage.

    Ein bloßer Artikel zählt nicht mit ("ein Tisch"), eine Zahl, die mit einem
    Artikel **beginnt**, sehr wohl: "die ein und zwanzig" ist die 21 und war
    vorher die 20, weil das "ein" verworfen wurde, bevor jemand geprüft hat, ob
    es eine Zahl eröffnet (Codex-Review PR #105, P1).
    """
    spans: list[_Span] = []
    i = 0
    while i < len(tokens):
        if tokens[i] in PUNCTUATION:
            i += 1
            continue
        span = _scan(tokens, i)
        if span is None or (tokens[i] in ARTICLES and span.end == i + 1):
            i += 1
            continue
        spans.append(span)
        i = max(span.end, i + 1)
    return spans


def _quantity_spans(tokens: list[str]) -> list[_Span]:
    """Zahlen, die an einem Mengen-Marker hängen: "zweimal", "2 x", "drei Portionen"."""
    spans: list[_Span] = []
    for i, token in enumerate(tokens):
        suffix = _QUANTITY_SUFFIX.match(token)
        if suffix:
            value = _word_value(suffix.group(1))
            if value is not None:
                spans.append(_Span(i, i + 1, value))
                continue
        if token in _QUANTITY_NOUNS and i > 0:
            span = _scan_ending_at(tokens, i - 1)
            if span is not None:
                spans.append(span)
    return spans


def parse_cardinal(text: str) -> int | None:
    """Der gesamte Text als eine Zahl, sonst `None`.

    Anders als `find_item_number` zählt hier auch ein alleinstehendes "eine":
    wer diese Funktion aufruft, hat bereits entschieden, dass an dieser Stelle
    eine Zahl steht. Satzzeichen am Rand stören nicht.
    """
    tokens = _tokens(text)
    while tokens and tokens[0] in PUNCTUATION:
        tokens.pop(0)
    while tokens and tokens[-1] in PUNCTUATION:
        tokens.pop()
    if not tokens:
        return None
    span = _scan(tokens, 0)
    if span is None:
        return None
    return span.value if span.end == len(tokens) else None


def find_numbers(text: str) -> list[int]:
    """Alle genannten Zahlen im Satz, in der Reihenfolge, in der sie gesagt wurden.

    Nicht mitgezählt werden bloße Artikel ("ein Tisch", siehe `ARTICLES`) und
    Mengenadverbien ("zweimal"): das eine ist keine Zahl, das andere eine Menge
    und damit Sache von `find_quantity`.
    """
    return [span.value for span in _number_spans(_tokens(text))]


def find_item_number(text: str) -> int | None:
    """Die Gerichtnummer im Satz. Ein ausdrückliches "Nummer …" schlägt alles andere.

    Eine Zahl, die an einem Mengen-Marker hängt, ist keine Gerichtnummer: "2 x
    die 23" ist eindeutig, auch wenn zwei Zahlen fallen (Codex-Review PR #105,
    P2). Bleiben danach mehrere Zahlen übrig, ist nicht entscheidbar, welche
    gemeint war -- dann `None` statt der ersten (CLAUDE.md §2 Regel 2).
    """
    tokens = _tokens(text)
    for i, token in enumerate(tokens):
        if token not in _ITEM_NUMBER_MARKERS:
            continue
        nach_marker = i + 1
        while nach_marker < len(tokens) and tokens[nach_marker] in PUNCTUATION:
            nach_marker += 1  # "Nr. 23"
        span = _scan(tokens, nach_marker)
        if span is not None:
            return span.value

    mengen = _quantity_spans(tokens)
    uebrig = [
        span
        for span in _number_spans(tokens)
        if not any(span.overlaps(menge) for menge in mengen)
    ]
    return uebrig[0].value if len(uebrig) == 1 else None


def find_quantity(text: str) -> int | None:
    """Die Menge, aber nur mit Marker: "zweimal", "2 x", "drei Portionen".

    Eine nackte Zahl ist keine Menge -- "die dreiundzwanzig" ist ein Gericht,
    nicht dreiundzwanzig Stück. Ohne Marker `None`, der Aufrufer setzt die
    Voreinstellung.
    """
    spans = _quantity_spans(_tokens(text))
    return spans[0].value if spans else None
