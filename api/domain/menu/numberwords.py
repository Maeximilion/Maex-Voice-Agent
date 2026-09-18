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
from itertools import pairwise

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
    ref = find_item_number_ref(text)
    return ref.value if ref is not None else None


@dataclass(frozen=True)
class ItemNumber:
    """Eine Gerichtnummer mit allem, was aus derselben Stelle im Satz stammt.

    `text` ist die Schreibweise, wie sie auf der Karte stehen kann: Ziffern
    behalten führende Nullen ("07"), ein direkt folgender Buchstabe a bis f
    gehört dazu ("23a", auch "23 a"). Bei einem Zahlwort ist es die Zahl.
    `marked` heisst: direkt hinter "Nummer"/"Nr." - nicht irgendwo im Satz.
    Alles aus einer Fundstelle, sonst mischt "23a, nein, Nummer 23" den
    Buchstaben der ersten mit der Zahl der zweiten Angabe (Codex PR #117, P1).
    """

    value: int
    text: str
    marked: bool
    # False, wenn eine Endung dranhängt, die keine Karte hat ("23g", "23ab"):
    # die Nummer gilt dann als nicht vorhanden, statt still zur 23 gekürzt zu
    # werden (Codex PR #117, P1).
    valid: bool = True


_SUFFIXES = frozenset("abcdef")
# Buchstaben direkt an Ziffern ("23g", "23ab", "2x") sind im Tokenstrom nicht
# mehr vom Leerzeichen-Fall ("23 bitte") zu unterscheiden. Deshalb je Token
# merken, ob der nächste ohne Lücke folgt - nach Position, nicht nach
# Ziffernfolge, sonst leiht sich "23a, nein, Nummer 23" das a der ersten 23.


def _glued(text: str) -> set[int]:
    """Indizes der Ziffern-Tokens, an denen ohne Lücke Buchstaben hängen."""
    matches = list(_TOKEN.finditer(fold(text)))
    return {
        i
        for i, (m, nxt) in enumerate(pairwise(matches))
        if m.group().isdigit() and nxt.group().isalpha() and m.end() == nxt.start()
    }


def _ref(tokens: list[str], span: _Span, marked: bool, glued: set[int]) -> ItemNumber:
    digits = span.end - span.start == 1 and tokens[span.start].isdigit()
    card = tokens[span.start] if digits else str(span.value)
    nxt = tokens[span.end] if span.end < len(tokens) else ""
    if digits and span.start in glued:
        suffix = nxt
        # "23x" ist keine Kartennummer und auch keine Endung: ungültig, nicht
        # die 23 (Codex PR #117). Echte Mengen ("2x Pho") hat _quantity_spans
        # vorher schon aussortiert, sie kommen hier nicht an.
        valid = suffix in _SUFFIXES
        return ItemNumber(span.value, card + suffix, marked, valid)
    if nxt in _SUFFIXES:
        return ItemNumber(span.value, card + nxt, marked)
    if len(nxt) == 1 and nxt.isalpha() and (nxt != "x" or marked):
        # "Nummer 23 g", "Nummer 23 x": einzelner Buchstabe ohne Karte -
        # ungültig, nicht 23 (Codex PR #117). Ohne Marker ist "23 x" eine Menge
        # und kommt hier gar nicht an.
        return ItemNumber(span.value, card + nxt, marked, valid=False)
    return ItemNumber(value=span.value, text=card, marked=marked)


def _canonical(card: str) -> str:
    """Kartennummer ohne führende Nullen, so wie search_menu sie vergleicht."""
    return card.lstrip("0") or "0"


# Wörter, die eine zweite Zahl zur Alternative oder Korrektur machen.
_ALTERNATIVE_WORDS = frozenset(
    {"oder", "nein", "bzw", "beziehungsweise", "sondern", "lieber", "statt", "anstatt"}
)


def _connected(tokens: list[str], after: int, before: int) -> bool:
    """Steht zwischen zwei Zahlen ein Wort für Alternative oder Korrektur?"""
    return any(t in _ALTERNATIVE_WORDS for t in tokens[after:before])


def _marked(tokens: list[str], glued: set[int]) -> list[ItemNumber]:
    spans: list[_Span] = []
    for i, token in enumerate(tokens):
        if token not in _ITEM_NUMBER_MARKERS:
            continue
        nach_marker = i + 1
        while nach_marker < len(tokens) and tokens[nach_marker] in PUNCTUATION:
            nach_marker += 1  # "Nr. 23"
        span = _scan(tokens, nach_marker)
        if span is not None:
            spans.append(span)
    if not spans:
        return []
    # Weitere Zahlen hinter der ersten markierten Nummer sind Alternative oder
    # Korrektur, aber nur mit einem Wort, das das sagt: "Nummer 23 oder 24",
    # "Nummer 23, nein 24" (Codex PR #117, P1). "Nummer 23 mit 2 Soßen" oder
    # "um 12 Uhr" ist ein Detail, keine zweite Nummer (P2). Zahlen vor dem
    # Marker bleiben Mengen: "zwei Nummer 23".
    mengen = _quantity_spans(tokens)
    for span in _number_spans(tokens):
        if (
            span.start > spans[0].start
            and not any(span.overlaps(m) for m in mengen)
            and not any(span.overlaps(s) for s in spans)
            and _connected(
                tokens, max(s.end for s in spans if s.start < span.start), span.start
            )
        ):
            spans.append(span)
    spans.sort(key=lambda s: s.start)
    found: list[ItemNumber] = []
    for span in spans:
        ref = _ref(tokens, span, marked=True, glued=glued)
        # Gleiche Kartennummer in zwei Schreibweisen ("07", "7") ist eine.
        if all(_canonical(r.text) != _canonical(ref.text) for r in found):
            found.append(ref)
    return found


def find_marked_item_numbers(text: str) -> list[ItemNumber]:
    """Alle verschiedenen Nummern hinter "Nummer"/"Nr.", in Satzfolge - auch
    eine zweite Zahl ohne eigenen Marker ("Nummer 23 oder 24").

    Mehr als eine heisst: der Gast korrigiert sich ("Nummer 23, nein, Nummer
    24") oder stellt zur Wahl ("Nummer 23 oder Nummer 24"). Welche gilt, ist
    nicht entscheidbar - der Aufrufer fragt nach (Codex PR #117, P1).
    """
    return _marked(_tokens(text), _glued(text))


def find_item_number_ref(text: str) -> ItemNumber | None:
    """Wie `find_item_number`, aber mit Kartenschreibweise und Marker.

    Zwei verschiedene ausdrücklich genannte Nummern ergeben `None`: die erste
    zu nehmen wäre geraten (CLAUDE.md §2 Regel 2).
    """
    tokens = _tokens(text)
    glued = _glued(text)
    marked = _marked(tokens, glued)
    if marked:
        return marked[0] if len(marked) == 1 else None

    mengen = _quantity_spans(tokens)
    uebrig = [
        span
        for span in _number_spans(tokens)
        if not any(span.overlaps(menge) for menge in mengen)
    ]
    if len(uebrig) != 1:
        return None
    return _ref(tokens, uebrig[0], marked=False, glued=glued)


def has_item_number_marker(text: str) -> bool:
    """Steht ein ausdrückliches "Nummer"/"Nr." im Satz?

    Ohne Marker ist eine nackte Zahl neben einem Gerichtnamen eher eine Menge:
    "zwei Frühlingsrollen" meint nicht Gericht 2 (search_menu, T-4.3).
    """
    return any(token in _ITEM_NUMBER_MARKERS for token in _tokens(text))


def find_quantity(text: str) -> int | None:
    """Die Menge, aber nur mit Marker: "zweimal", "2 x", "drei Portionen".

    Eine nackte Zahl ist keine Menge -- "die dreiundzwanzig" ist ein Gericht,
    nicht dreiundzwanzig Stück. Ohne Marker `None`, der Aufrufer setzt die
    Voreinstellung.
    """
    spans = _quantity_spans(_tokens(text))
    return spans[0].value if spans else None
