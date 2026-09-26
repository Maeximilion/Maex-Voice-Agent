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
# Zahlwörter oberhalb von MAX_VALUE. _scan kennt sie nicht, sie sind aber
# erkennbar als Zahl gemeint: "Nummer tausend" ist eine Nummer, die es nicht
# gibt, und darf nicht als Name "tausend" in der Suche landen (Codex PR #117).
_TOO_LARGE = frozenset({"tausend", "million", "millionen", "milliarde", "milliarden"})
# Zwischen Marker und Zahl erlaubt: "die Nummer ist 23", "Nummer die 23".
_MARKER_FILLER = frozenset({"ist", "war", "waere", "die", "der", "das", "den"})

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

    Eine genannte Nummer, die keine Kartenform hat ("Nummer 23g", "Nummer A12",
    "Nummer tausend"), ergibt ebenfalls `None`: sie trägt keine Zahl, mit der
    sich weiterarbeiten liesse. Wer die Schreibweise braucht, um danach zu
    fragen, nimmt `find_item_number_ref` (Codex PR #117, P2).
    """
    ref = find_item_number_ref(text)
    return ref.value if ref is not None and ref.valid else None


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


def _prefixed(text: str) -> set[int]:
    """Indizes der Buchstaben-Tokens, an denen ohne Lücke Ziffern hängen.

    Gegenstück zu `_glued`: dort folgt der Buchstabe auf die Ziffer ("23a"),
    hier geht er ihr voran ("A12"). Eine Kartennummer sieht so nie aus (docs/14
    lässt nur Ziffern mit optionalem a-f dahinter zu), deshalb ist "Nummer A12"
    eine Nummer, die es nicht gibt - und kein Gerichtname (Codex PR #117).
    """
    matches = list(_TOKEN.finditer(fold(text)))
    return {
        i
        for i, (m, nxt) in enumerate(pairwise(matches))
        if m.group().isalpha() and nxt.group().isdigit() and m.end() == nxt.start()
    }


# Wie lang ein Buchstabenteil einer Kartennummer hoechstens ist. Die Karte
# kennt eine Ziffernfolge mit einem Buchstaben a bis f (docs/14); zwei ist
# schon grosszuegig. Alles darueber ist ein Wort: "Ente", "Pho", "Beef",
# "Cafe" sind Gerichtnamen, keine Kartenteile (Codex PR #117, P2).
_MAX_CARD_LETTERS = 2


def _card_letters(token: str) -> bool:
    """Buchstaben, die zu einer Kartennummer gehoeren koennten.

    Nur a bis f (docs/14) und hoechstens zwei davon. Die Laenge allein reicht
    nicht: "so" und "ab" sind beide zwei Zeichen, aber nur eines davon kann
    Teil einer Kartennummer sein. Sonst wuerde "Nummer so 23" zur nicht
    vorhandenen Nummer "so23", statt nach Regel A nachzufragen (Codex PR #117).
    """
    return (
        token.isalpha()
        and len(token) <= _MAX_CARD_LETTERS
        and all(c in _SUFFIXES for c in token)
    )


def _long_suffix(token: str) -> bool:
    """Mehrere Buchstaben, die zusammen eine Kartenendung sein wollen ("ab").

    Begrenzt auf a bis f, die einzigen Buchstaben, die eine Kartennummer
    tragen kann (docs/14). Damit bleibt "Nummer 23 mit Reis" die 23 mit einem
    Wort daneben, waehrend "Nummer 23 ab" wie "Nummer 23ab" ungueltig ist -
    die Erkennung setzt das Leerzeichen, nicht der Gast (Codex PR #117, P2).
    """
    return len(token) == _MAX_CARD_LETTERS and _card_letters(token)


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
    # "Nummer 23 ab": abgesetzte Endung aus mehreren Buchstaben. Zusammen
    # geschrieben ("23ab") war das laengst ungueltig, getrennt kam bisher die
    # blanke 23 heraus - und zwar nur ueber find_item_number_ref, waehrend die
    # Suche nachfragte. Begrenzt auf Buchstaben, die eine Kartenendung
    # ueberhaupt tragen kann (a bis f): "Nummer 23 mit Reis" bleibt die 23 mit
    # einem Wort daneben (Codex PR #117, P2).
    if marked and _long_suffix(nxt):
        return ItemNumber(span.value, card + nxt, marked, valid=False)
    if nxt in _SUFFIXES:
        return ItemNumber(span.value, card + nxt, marked)
    if len(nxt) == 1 and nxt.isalpha() and (nxt != "x" or marked):
        # "Nummer 23 g", "Nummer 23 x": einzelner Buchstabe ohne Karte -
        # ungültig, nicht 23 (Codex PR #117). Ohne Marker ist "23 x" eine Menge
        # und kommt hier gar nicht an.
        return ItemNumber(span.value, card + nxt, marked, valid=False)
    return ItemNumber(value=span.value, text=card, marked=marked)


def canonical_card(card: str) -> str:
    """Kartennummer so, wie search_menu sie vergleicht: klein, ohne führende
    Nullen. Eine Stelle für Import, Zahlwörter und Text-Telefon."""
    return card.lower().lstrip("0") or "0"


_LINK_ARTICLES = frozenset({"die", "der", "das", "den"})
# Zögerlaute der Spracherkennung, nach fold() (ä -> ae).
_HESITATIONS = frozenset({"aeh", "aehm", "aehh", "hm", "hmm", "ehm", "oehm"})
# Wörter, die eine zweite Zahl zur Alternative oder Korrektur machen.
_ALTERNATIVE_WORDS = frozenset(
    {"oder", "nein", "bzw", "beziehungsweise", "sondern", "lieber", "statt", "anstatt"}
)
# Wörter, die zwei Zahlen verbinden können. Sie sind nur dann durchsichtig,
# wenn links und rechts wirklich eine Zahl steht - "Nummer 23, nein" ist eine
# zurückgenommene Bestellung, keine 23 (Codex PR #117, P1).
_CONNECTORS = _ALTERNATIVE_WORDS | {"und"}


def _connected(tokens: list[str], after: int, before: int) -> bool:
    """Verbindet, was zwischen zwei Zahlen steht, sie zu Kandidaten?

    Ja bei einem Wort für Alternative, Korrektur oder Aufzählung ("oder",
    "nein", "und") und bei bloßen Satzzeichen ("Nummer 23, 24"): eine zweite
    genannte Nummer wird nie verschluckt (Codex PR #117). "drei und zwanzig"
    ist davon nicht betroffen, das fasst _scan vorher zu einer Zahl zusammen.
    """
    between = [t for t in tokens[after:before] if t not in PUNCTUATION]
    # Zögerlaute und Artikel sind durchsichtig ("Nummer 23, äh, 24", "Nummer 23
    # oder die 24"). Alles andere muss ein Verbindungswort sein - ein "oder"
    # zwischen Reis und Nudeln verbindet keine spätere Uhrzeit (Codex PR #117).
    words = [t for t in between if t not in _HESITATIONS and t not in _LINK_ARTICLES]
    return all(t in _ALTERNATIVE_WORDS or t == "und" for t in words)


def _marker_target(
    tokens: list[str], i: int, prefixed: set[int]
) -> tuple[int, int, _Span | None, ItemNumber | None]:
    """Was hinter dem Marker an Stelle `i` steht.

    Rückgabe `(start, end, span, bad)`: `span` ist eine gelesene Zahl, `bad`
    eine genannte, die keine Kartennummer sein kann. `start` bis `end` sind die
    Token, die zu `bad` gehören. Höchstens eines von beiden ist gesetzt.

    Eine Stelle für beide Ausgänge - `_marked` für die Verständnisleiter und
    `sole_item_number` für die Suche. Vorher stand die Logik zweimal da, und
    der Prefix-Fall wurde nur in einer der beiden Kopien ungültig: "Nummer A12"
    war in der Suche `not_found`, über `find_item_number` aber Gericht 12
    (Codex PR #117, P2).

    Erst scannen, dann überspringen: "Nummer ein und zwanzig" ist 21, das "ein"
    eröffnet die Zahl und ist an dieser Stelle kein Füllwort.
    """
    j = i + 1
    while j < len(tokens):
        span = _scan(tokens, j)
        if span is not None:
            return j, j, span, None
        if (
            tokens[j] in PUNCTUATION
            or tokens[j] in _MARKER_FILLER
            or tokens[j] in _SENTENCE_FILLER
            or tokens[j] in _HESITATIONS
        ):
            j += 1
            continue
        break
    if j >= len(tokens):
        return j, j, None, None
    word = tokens[j]
    # Ziffern ausserhalb des Kartenbereichs ("Nummer 1000") tragen ihren Wert;
    # ein Zahlwort darüber ("tausend", "eintausend") hat keinen im erlaubten
    # Bereich, `value` bleibt dann 0 und wird nie gelesen, weil `valid=False`
    # den Aufrufer vorher abbiegen lässt.
    if word.isdigit():
        return j, j + 1, None, ItemNumber(int(word), word, True, valid=False)
    if _too_large(word):
        return j, j + 1, None, ItemNumber(0, word, True, valid=False)
    # "Nummer A12", "Nummer A 12", "Nummer AB 12", "Nummer A zwölf": Buchstaben
    # vor der Zahl. Keine Kartenform - eine Endung steht hinter der Zahl, nie
    # davor. Die Zahl darf dabei auch als Wort kommen, die Erkennung liefert
    # beides (Codex PR #117, P1). Nur Kartenbuchstaben zählen: "Nummer so 23"
    # ist eine Nummer neben einem Wort und damit eine Rückfrage, keine nicht
    # vorhandene Nummer "so23" (Codex PR #117, P2).
    if _card_letters(word):
        dahinter = _scan(tokens, j + 1)
        if dahinter is not None:
            return (
                j,
                dahinter.end,
                None,
                ItemNumber(0, word + str(dahinter.value), True, valid=False),
            )
    if j in prefixed and _card_letters(word):
        return j, j + 2, None, ItemNumber(0, word + tokens[j + 1], True, valid=False)
    return j, j, None, None


def _marked(tokens: list[str], glued: set[int], prefixed: set[int]) -> list[ItemNumber]:
    spans: list[_Span] = []
    # Ziffern hinter dem Marker, die ausserhalb des Zahlbereichs liegen
    # ("Nummer 1000"): ungültig, und keine spätere Zahl darf nachrücken
    # ("Nummer 1000 und 23" ist nicht die 23, Codex PR #117).
    invalid: list[ItemNumber] = []
    for i, token in enumerate(tokens):
        if token not in _ITEM_NUMBER_MARKERS:
            continue
        _, _, span, bad = _marker_target(tokens, i, prefixed)
        if span is not None:
            spans.append(span)
        elif bad is not None:
            invalid.append(bad)
    if not spans:
        return invalid
    # Weitere Zahlen hinter der ersten markierten Nummer sind Alternative oder
    # Korrektur, aber nur mit einem Wort, das das sagt: "Nummer 23 oder 24",
    # "Nummer 23, nein 24" (Codex PR #117, P1). "Nummer 23 mit 2 Soßen" oder
    # "um 12 Uhr" ist ein Detail, keine zweite Nummer (P2). Zahlen vor dem
    # Marker bleiben Mengen: "zwei Nummer 23".
    # Kette: eine spätere Zahl ist Alternative, wenn ihr direkter Vorgänger
    # schon Kandidat ist und sie entweder unmittelbar folgt ("Nummer zwei drei",
    # "Nummer 23 24") oder ein Korrekturwort dazwischen steht - ohne eine
    # andere Zahl dazwischen. "Nummer 23 mit 2 oder 3 Soßen": das "oder"
    # verbindet die Soßen, nicht die 23 (Codex PR #117, P1, P2). Mengen sind
    # nie Kandidat und unterbrechen die Kette.
    mengen = _quantity_spans(tokens)
    later = sorted(
        (
            span
            for span in _number_spans(tokens)
            if span.start > spans[0].start and not any(span.overlaps(s) for s in spans)
        ),
        key=lambda s: s.start,
    )
    chain = sorted(
        [(s, True) for s in spans] + [(s, False) for s in later],
        key=lambda e: e[0].start,
    )
    prev: tuple[_Span, bool] | None = None
    for span, is_marked in chain:
        if span.start < spans[0].start:
            continue
        if not is_marked:
            candidate = (
                prev is not None
                and prev[1]
                and not any(span.overlaps(m) for m in mengen)
                and (
                    span.start == prev[0].end
                    or _connected(tokens, prev[0].end, span.start)
                )
            )
            if candidate:
                spans.append(span)
            prev = (span, candidate)
        else:
            prev = (span, True)
    spans.sort(key=lambda s: s.start)
    found: list[ItemNumber] = list(invalid)
    for span in spans:
        ref = _ref(tokens, span, marked=True, glued=glued)
        # Gleiche Kartennummer in zwei Schreibweisen ("07", "7") ist eine.
        if all(canonical_card(r.text) != canonical_card(ref.text) for r in found):
            found.append(ref)
    return found


def find_marked_item_numbers(text: str) -> list[ItemNumber]:
    """Alle verschiedenen Nummern hinter "Nummer"/"Nr.", in Satzfolge - auch
    eine zweite Zahl ohne eigenen Marker ("Nummer 23 oder 24").

    Mehr als eine heisst: der Gast korrigiert sich ("Nummer 23, nein, Nummer
    24") oder stellt zur Wahl ("Nummer 23 oder Nummer 24"). Welche gilt, ist
    nicht entscheidbar - der Aufrufer fragt nach (Codex PR #117, P1).
    """
    return _marked(_tokens(text), _glued(text), _prefixed(text))


def find_item_number_ref(text: str) -> ItemNumber | None:
    """Wie `find_item_number`, aber mit Kartenschreibweise und Marker.

    Zwei verschiedene ausdrücklich genannte Nummern ergeben `None`: die erste
    zu nehmen wäre geraten (CLAUDE.md §2 Regel 2).
    """
    tokens = _tokens(text)
    glued = _glued(text)
    marked = _marked(tokens, glued, _prefixed(text))
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
    ref = _ref(tokens, uebrig[0], marked=False, glued=glued)
    # Ohne "Nummer" ist eine Zahl, die keine saubere Kartenform ergibt, keine
    # genannte Nummer, sondern Text: "einmal 7up bitte", "das ist 5g Zucker".
    # Sonst meldete die Leiter "die Nummer 7up gibt es nicht", waehrend
    # search_menu den Alias findet (Review PR #117).
    return ref if ref.valid else None


# Wörter, die in einem reinen Nummernsatz stehen dürfen, nach fold(). Alles
# andere daneben macht den Satz unklar (Regel A, docs/01_STATUS.md).
_SENTENCE_FILLER = frozenset(
    {
        "ich", "wir", "haette", "haetten", "moechte", "moechten", "nehme", "nehmen",
        "meine", "meinte", "gern", "gerne", "bitte", "dann", "noch", "und", "also",
        "ja", "genau", "mal", "die", "der", "das", "den", "dem", "des", "ein",
        "eine", "einen", "einem", "einer", "von", "vom", "ist", "war", "waere",
    }
)  # fmt: skip
# Nur im Satz, nicht zwischen Marker und Zahl: "ich wuerde die 13", "Guten Tag,
# die 13", "dazu die 24" fielen ohne diese Woerter als Rest in die Namenssuche
# (T-5.2). Zwischen "Nummer" und Zahl machen sie den Satz weiter unklar -
# "Nummer dazu 23" ist keine 23 (Review PR #147). Dieselben Woerter stehen in
# normalize.FILLER, sonst suchte die Namenssuche nach "wuerde pho".
_LEAD_FILLER = frozenset(
    {
        "wuerde", "wuerden", "wuerd", "hallo", "guten", "tag", "abend", "dazu",
        "bestellen", "bestelle",
    }
)  # fmt: skip
# Womit ein Satz die Bestellung aus dem vorigen fortsetzt: "Und noch die 24",
# "Und dann dazu die 24".
_OPENING = frozenset({"und", "dann", "noch", "dazu", "also", "ja"})


def _too_large(token: str) -> bool:
    """Ein Zahlwort oberhalb von MAX_VALUE, auch zusammengesetzt.

    "tausend", "eintausend", "zweitausend", "dreitausendzwei", "Million": als
    Zahl gemeint, aber keine Kartennummer. Der Aufrufer macht daraus
    `valid=False`, nicht `None` - sonst würde aus einer genannten Nummer eine
    Namenssuche (CLAUDE.md §2 Regel 2).

    Gesucht wird an jeder Stelle im Wort, nicht nur am Anfang: im Deutschen
    steht der Faktor davor ("zweitausend"), und "tausend" kann in der Mitte
    sitzen ("dreitausendzwei"). Ein Gerichtname trägt keines dieser Wörter,
    und geprüft wird ohnehin nur direkt hinter einem Marker (Codex PR #117).
    """
    return any(word in token for word in _TOO_LARGE)


def sole_item_number(text: str) -> tuple[ItemNumber | None, bool]:
    """Regel A: eine Kartennummer nur, wenn der ganze Satz genau eine Nummer ist.

    Erlaubt neben der Nummer: "Nummer"/"Nr.", Füllwörter, Zögerlaute,
    Satzzeichen und eine Menge ("zweimal", "2x", "drei Portionen", "zwei
    Nummer 23"). Ergebnis:

    - `(ref, False)`: genau eine Nummer, sonst nichts - der Aufrufer nimmt sie
      (ungültige wie "23g" oder "Nummer 1000" als `valid=False`).
    - `(None, True)`: Nummer plus mehr (zweite Zahl, Text) - nachfragen.
    - `(None, False)`: kein Nummernsatz. Ohne "Nummer" ist eine Zahl neben
      einem Namen eine Menge ("zwei Frühlingsrollen"), die Namenssuche
      entscheidet.

    Eine Regel statt vieler Sonderfälle: jede Satzform, die nicht eindeutig
    eine Nummer ist, wird zur Rückfrage statt zum stillen Fehlgriff
    (Codex-Reviews PR #117, 15 Runden).
    """
    tokens = _tokens(text)
    glued = _glued(text)
    prefixed = _prefixed(text)
    mengen = _quantity_spans(tokens)
    consumed: set[int] = set()
    marked: list[_Span] = []
    invalid: list[ItemNumber] = []
    # Token, die schon zu einer ungueltigen Nummer gehoeren. Die Ziffer aus
    # "Nummer A12" darf nicht noch einmal als eigene Zahl zaehlen, sonst waere
    # der Satz "unklar" statt einer Nummer, die es nicht gibt.
    invalid_at: set[int] = set()
    marker_at: set[int] = set()
    for i, token in enumerate(tokens):
        if token not in _ITEM_NUMBER_MARKERS:
            continue
        start, end, span, bad = _marker_target(tokens, i, prefixed)
        if span is not None:
            marked.append(span)
            marker_at.add(i)
        elif bad is not None:
            invalid.append(bad)
            marker_at.add(i)
            consumed.update(range(start, end))
            invalid_at.update(range(start, end))
    numbers = list(marked)
    for span in _number_spans(tokens):
        if any(span.overlaps(m) for m in marked) or any(
            span.overlaps(q) for q in mengen
        ):
            continue
        if any(k in invalid_at for k in range(span.start, span.end)):
            continue
        if span.end in marker_at:
            # "zwei Nummer 23": Menge vor dem Marker, gehört nicht zum Rest.
            consumed.update(range(span.start, span.end))
            continue
        numbers.append(span)
    if not numbers and not invalid:
        return None, False

    for span in [*numbers, *mengen]:
        consumed.update(range(span.start, span.end))
        nxt = span.end
        if nxt >= len(tokens):
            continue
        after = tokens[nxt]
        # Zur Zahl gehören: ein Mengenwort ("2 x", "drei Portionen"), eine
        # Kartenendung a bis f ("23a", "23 a") und - nur hinter "Nummer" - jede
        # andere Endung, die die Nummer dann ungültig macht ("Nummer 23g").
        # Ohne Marker bleibt "7up" oder "23g" Text für die Namenssuche.
        if (
            after in _QUANTITY_NOUNS
            or after in _SUFFIXES
            or (span in marked and _long_suffix(after))
            or (
                span in marked
                and after.isalpha()
                and (span.start in glued or len(after) == 1)
            )
        ):
            consumed.add(nxt)
    consumed |= marker_at
    # Token, die zu einer Zahl gehören - gültig oder nicht.
    number_at = {k for s in numbers for k in range(s.start, s.end)} | invalid_at

    def _connects(k: int) -> bool:
        """Steht links und rechts von Position k eine Zahl?"""
        return any(x < k for x in number_at) and any(x > k for x in number_at)

    # Ein Verbindungswort zwischen zwei Zahlen ist durchsichtig: "23 oder 24"
    # ist eine Rückfrage nach der Nummer, kein Satz über ein Gericht namens
    # "oder". Hängt es dagegen frei ("Nummer 23, nein", "Nummer 23 oder"), hat
    # der Gast zurückgenommen oder nicht zu Ende gesprochen. Es ist dann kein
    # Gerichtname - es gehört also nicht in den Rest, sondern macht den Satz
    # für sich unklar (Codex PR #117, P1).
    #
    # Ausnahme: ein "und", das den Satz eröffnet. "Und noch die 24" setzt die
    # Bestellung aus dem vorigen Satz fort und verbindet keine zweite Zahl in
    # diesem (T-5.2). Nur ganz vorn: steht davor schon eine Menge ("zweimal und
    # die 24") oder ein Marker ("Nummer und 24"), verbindet es etwas in diesem
    # Satz, das fehlt. Und nie vor einem Zehner: "und zwanzig" ist die zweite
    # Hälfte von "drei und zwanzig", die Erkennung hat nur abgeschnitten
    # (Review PR #147). Nur "und": ein "oder" oder "nein" vorweg bezieht sich
    # auf etwas, das dieser Satz nicht nennt, und bleibt eine Rückfrage.
    def _opens(k: int) -> bool:
        if tokens[k] != "und" or (k + 1 < len(tokens) and tokens[k + 1] in TENS):
            return False
        return all(
            t in PUNCTUATION or t in _HESITATIONS or t in _OPENING for t in tokens[:k]
        )

    loose = any(
        t in _CONNECTORS and not _connects(k) and not _opens(k)
        for k, t in enumerate(tokens)
        if k not in consumed
    )
    # Token zwischen einem Marker und der nächsten Zahl: dort gilt _LEAD_FILLER
    # nicht, "Nummer dazu 23" bleibt unklar (Review PR #147).
    after_marker = {
        k
        for i, tok in enumerate(tokens)
        if tok in _ITEM_NUMBER_MARKERS
        for k in range(i + 1, min((n.start for n in numbers if n.start > i), default=0))
    }
    # Was daneben übrig bleibt und ein Gerichtname sein könnte.
    residue = [
        t
        for k, t in enumerate(tokens)
        if k not in consumed
        and t not in PUNCTUATION
        and t not in _CONNECTORS
        and t not in _SENTENCE_FILLER
        and (t not in _LEAD_FILLER or k in after_marker)
        and t not in _HESITATIONS
        and t not in _ITEM_NUMBER_MARKERS
        and not _QUANTITY_SUFFIX.match(t)
    ]
    has_marker = bool(marked or invalid)
    # Ein Marker, der selbst keine Zahl gefasst hat, zaehlt trotzdem - aber nur
    # fuer eine Zahl, die nach ihm kommt: "Nummer Ente 23" ist eine Nummer
    # neben einem Namen, also eine Rueckfrage statt der Namenssuche nach
    # "ente" (Codex PR #117, P2). Steht die Zahl davor, gehoert sie nicht zum
    # Marker: in "zwei Fruehlingsrollen, Nummer weiss ich nicht" ist die zwei
    # eine Menge und der Gast sagt gerade, dass er die Nummer nicht kennt -
    # dann entscheidet die Namenssuche.
    nummer_gemeint = has_marker or any(
        span.start > i
        for i, tok in enumerate(tokens)
        if tok in _ITEM_NUMBER_MARKERS
        for span in numbers
    )
    # Ein frei hängendes Verbindungswort ist nur dann eine zurückgenommene
    # Nummer, wenn überhaupt von einer Nummer die Rede war: mit Marker, oder
    # wenn kein Gerichtname daneben steht. Sonst gehört das "oder" zum Namen -
    # "zwei Cola oder Fanta" ist eine Menge neben einem Namen, und der Alias
    # "cola oder fanta" muss erreichbar bleiben (Codex PR #117, P2).
    dangling = loose and (has_marker or not residue)
    if len(numbers) + len(invalid) == 1 and not residue and not dangling:
        if invalid:
            return invalid[0], False
        return _ref(tokens, numbers[0], marked=has_marker, glued=glued), False
    if nummer_gemeint or dangling or not residue:
        return None, True
    return None, False


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
