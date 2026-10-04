"""German number words and quantities as numbers (docs/11 §menu).

Pure functions: no DB, no HTTP, no configuration. Everything decided here is
decided from the text the speech recognition delivers, nothing else.

Three jobs that differ on the phone:

- `parse_cardinal("dreiundzwanzig")` — the whole text **is** the number
- `find_item_number("Nummer vierzig sieben")` — the number is **inside** the sentence
- `find_quantity("zweimal die Frühlingsrollen")` — how many, not what

The speech recognition delivers the same number in several shapes: as digits
("23"), as one word ("dreiundzwanzig"), written apart ("drei und zwanzig") or
spoken digit by digit ("vierzig sieben"). All four give the same number here.

Card numbers also carry letters: a suffix a to g ("23a", "25g") and, if the
menu has it, a prefix in front ("S12", "SM1"). Which prefixes apply is not
written here; it comes as a `CardFormat` from the numbers on the menu
(CLAUDE.md §2 rule 1, T-4.12). Without a `CardFormat` this module knows no
prefixes.

**No match gives `None`, never a guess** (CLAUDE.md §2 rule 2). Whether that
becomes a follow-up question, a step on the understanding ladder or an abort
is up to the caller, not this module.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from functools import cached_property
from itertools import pairwise, product

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
    """A number found in the sentence, with its position. The position is
    needed to tell whether this number is already taken as a quantity.

    With a card prefix ("S zwölf") the span starts at the prefix: the `lead`
    tokens in front belong to the number, and `prefixes` are the prefixes they
    can mean - one for "S", several for a category word ("Sushi")."""

    start: int
    end: int
    value: int
    lead: int = 0
    prefixes: tuple[str, ...] = ()

    @property
    def digits_at(self) -> int:
        """Where the number itself starts, after a prefix."""
        return self.start + self.lead

    def overlaps(self, other: "_Span") -> bool:
        return self.start < other.end and other.start < self.end


def fold(text: str) -> str:
    """Kleinschreibung plus Umlaut-Ersatzschreibung. Am Telefon klingt "fünf" wie
    "fuenf"; welche Schreibweise die Erkennung liefert, ist Zufall."""
    return text.lower().translate(_UMLAUTS)


# A card number in its parts: prefix, digits, suffix. Which forms are allowed
# is checked by the import (importer.is_card_number); this only splits.
CARD_PARTS = re.compile(r"([a-z]*)(\d+)([a-z]*)")

# Letters as the speech recognition writes them out when the guest spells:
# "Es zwölf" is S12, "Es Em eins" is SM1. German alphabet, not menu data:
# which letter counts as a prefix is decided by the menu alone (`CardFormat`).
_LETTER_NAMES = {
    "a": "a", "be": "b", "ce": "c", "ze": "c", "zeh": "c", "de": "d", "e": "e",
    "ef": "f", "ge": "g", "ha": "h", "i": "i", "jot": "j", "ka": "k", "el": "l",
    "em": "m", "en": "n", "o": "o", "pe": "p", "ku": "q", "er": "r", "es": "s",
    "ess": "s", "te": "t", "u": "u", "vau": "v", "we": "w", "ix": "x",
    "ypsilon": "y", "zett": "z",
}  # fmt: skip
# At most this many tokens form a prefix ("Es Em" -> sm, "S M" -> sm).
_MAX_PREFIX_TOKENS = 3


@dataclass(frozen=True)
class CardFormat:
    """What a card number of this menu can carry in front of the number (T-4.12).

    `prefixes`: letters in front of the number that occur on the menu ("s",
    "sm"). `words`: a category word the guest says instead of the prefix
    ("Sushi zwölf"), with every prefix it can mean - only for categories in
    which every number carries a prefix. Comes from the database
    (`items.card_format`), never from the code (CLAUDE.md §2 rule 1).
    """

    prefixes: frozenset[str] = frozenset()
    # Pairs (word, prefixes) instead of a dict: the value stays hashable and
    # immutable, and NO_PREFIXES shares no mutable state.
    words: tuple[tuple[str, tuple[str, ...]], ...] = ()

    @classmethod
    def from_items(cls, items: Iterable[tuple[str, str]]) -> "CardFormat":
        """From (number, category) of the active dishes.

        A category word only counts if the category is one word and each of
        its prefixes occurs only in it: the group "Sushi" holds S1 to S53 and
        SM1 to SM6, so "Sushi zwölf" means S12 or SM12, and which of them
        exists is decided by the search against the menu.
        """
        prefixes: set[str] = set()
        in_category: dict[str, set[str]] = {}
        categories_of: dict[str, set[str]] = {}
        for number, category in items:
            parts = CARD_PARTS.fullmatch(fold(number.strip()))
            if parts is None:
                continue
            prefix = parts.group(1)
            name = fold(category.strip())
            if prefix:
                prefixes.add(prefix)
                categories_of.setdefault(prefix, set()).add(name)
            in_category.setdefault(name, set()).add(prefix)
        words: dict[str, tuple[str, ...]] = {}
        for name, found in in_category.items():
            carried = found - {""}
            # Only categories whose numbers all carry a prefix: the empty
            # candidate would have no tie to the category, and "Sushi zwölf"
            # would find the soups' 12 (Codex PR #155).
            if (
                carried
                and "" not in found
                and name.isalpha()
                and all(categories_of[p] == {name} for p in carried)
            ):
                words[name] = tuple(sorted(found))
        return cls(frozenset(prefixes), tuple(sorted(words.items())))

    @cached_property
    def _word_map(self) -> dict[str, tuple[str, ...]]:
        """`words` as a dict, built once per card format."""
        return dict(self.words)

    def prefixes_of(self, run: list[str], spelled: bool = True) -> tuple[str, ...]:
        """The prefixes these tokens can mean directly in front of a number.

        `spelled=False`: letter names ("es", "em") do not count. They are also
        German words - "Dann nehme ich es zwei" is not S2 (review PR #155).
        """
        if not self.prefixes or not run or any(t in PUNCTUATION for t in run):
            return ()
        if len(run) == 1 and run[0] in self._word_map:
            return self._word_map[run[0]]
        spellings = {
            "".join(parts)
            for parts in product(
                *(
                    {t, _LETTER_NAMES[t]} if spelled and t in _LETTER_NAMES else {t}
                    for t in run
                )
            )
        }
        return tuple(sorted(spellings & self.prefixes))


# Short words that are said in front of a number on the phone ("ja 12", "so
# 12", "es zwei") or that the recognition emits as a hesitation. As a prefix
# they would silently become part of the number (code review PR #155). "s" and
# "n" stay allowed: S is the register's Sushi prefix, N an obvious one for
# noodles.
_SPOKEN_PREFIXES = frozenset({"ja", "es", "so", "um", "zu", "da", "du", "er", "ob"})


def reserved_prefix(prefix: str) -> bool:
    """Does numberwords already read these letters as something else - a
    quantity ("2 x 12", "3 st"), a marker ("Nr 5"), a hesitation ("hm") or a
    spoken word ("ja 12")? Then a menu cannot use them as a prefix, or "2 x 12"
    would become x12 (review PR #155)."""
    return (
        prefix in _QUANTITY_NOUNS
        or prefix in _ITEM_NUMBER_MARKERS
        or prefix in _HESITATIONS
        or prefix in _SPOKEN_PREFIXES
    )


NO_PREFIXES = CardFormat()


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


# What may stand in front of a spelled prefix ("Es zwölf"). After any other
# word ("nehme ich es zwei") "es" is a pronoun (review PR #155). Allowed as
# well: the wish at the start of the sentence ("Ich haette gern Es zwoelf") and
# the greeting ("Hallo, Es zwoelf", code review PR #155) - but no verb such as
# "nehme" or "hole": after those, "es" is the object.
_SPELLED_LEAD = frozenset(
    {
        "und", "noch", "die", "der", "das", "den", "nummer", "nr", "no", "bitte",
        "ich", "wir", "haette", "haetten", "moechte", "moechten", "gern", "gerne",
    }
)  # fmt: skip


def _clean_lead(tokens: list[str], at: int) -> bool:
    """Is there only punctuation, a hesitation, an article, a marker or a
    quantity before position `at`? Only then may a letter name be a prefix
    there."""
    return all(
        t in PUNCTUATION
        or t in _SPELLED_LEAD
        or t in _LEAD_FILLER
        or t in _HESITATIONS
        or t in _QUANTITY_NOUNS
        or _word_value(t) is not None
        or (_QUANTITY_SUFFIX.match(t) is not None and _word_value(t[:-3]) is not None)
        for t in tokens[:at]
    )


def _number_spans(
    tokens: list[str],
    card: CardFormat = NO_PREFIXES,
    prefixed: set[int] | frozenset[int] = frozenset(),
) -> list[_Span]:
    """All numbers said, with their positions.

    A bare article does not count ("ein Tisch"), but a number that **starts**
    with an article does: "die ein und zwanzig" is 21 and used to be 20,
    because the "ein" was dropped before anyone checked whether it opens a
    number (Codex review PR #105, P1).

    A prefix of the menu directly in front ("S zwölf", "Sushi zwölf") belongs
    to the span (`_with_prefix`).
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
        floor = spans[-1].end if spans else 0
        spans.append(_with_prefix(tokens, span, card, floor, prefixed))
        i = max(span.end, i + 1)
    return spans


def _with_prefix(
    tokens: list[str],
    span: _Span,
    card: CardFormat,
    floor: int,
    prefixed: set[int] | frozenset[int] = frozenset(),
) -> _Span:
    """The span extended by a card prefix directly in front, longest first.

    Only tokens from `floor` on: what belongs to the previous number is not a
    prefix. Letter names only after a clean sentence start (`_clean_lead`) and
    never glued to the digits: "ES12" is the prefix ES, not "Es zwölf" (Codex
    PR #155).
    """
    for n in range(_MAX_PREFIX_TOKENS, 0, -1):
        start = span.start - n
        if start < floor:
            continue
        spelled = _clean_lead(tokens, start) and span.start - 1 not in prefixed
        found = card.prefixes_of(tokens[start : span.start], spelled)
        if found:
            return _Span(start, span.end, span.value, n, found)
    return span


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


def find_item_number(text: str, card: CardFormat = NO_PREFIXES) -> int | None:
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
    ref = find_item_number_ref(text, card)
    # A number with a prefix ("S12") has no bare number to work with: 12 would
    # be a different dish (review PR #155).
    plain = ref is not None and ref.valid and not ref.choices and ref.text[:1].isdigit()
    return ref.value if plain and ref is not None else None


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
    # False if a suffix is attached that no menu has ("23h", "23ab"): the
    # number then counts as not existing, instead of being silently cut down
    # to 23 (Codex PR #117, P1).
    valid: bool = True
    # Several possible card numbers from one place: "Sushi zwölf" is S12 or
    # SM12. `text` then names all of them ("s12 oder sm12"). Which one exists
    # is decided by the search against the menu (T-4.12).
    choices: tuple[str, ...] = ()

    @property
    def cards(self) -> tuple[str, ...]:
        """The card numbers to look up."""
        return self.choices or (self.text,)


# Card suffixes: a to f for variants, g for the register's sauces (25G to 60G,
# T-4.12). The register has no combined suffixes ("35AE").
CARD_SUFFIXES = frozenset("abcdefg")
_SUFFIXES = CARD_SUFFIXES
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
    """Indices of the letter tokens that have digits attached without a gap.

    Counterpart to `_glued`: there the letter follows the digits ("23a"), here
    it comes first ("A12"). If the letter is not a prefix of the menu
    (`CardFormat`), "Nummer A12" is a number that does not exist - and not a
    dish name (Codex PR #117).
    """
    matches = list(_TOKEN.finditer(fold(text)))
    return {
        i
        for i, (m, nxt) in enumerate(pairwise(matches))
        if m.group().isalpha() and nxt.group().isdigit() and m.end() == nxt.start()
    }


# The maximum length of a letter part of a card number. The menu has digits
# with one letter a to g (docs/14); two is already generous. Anything longer is
# a word: "Ente", "Pho", "Beef", "Cafe" are dish names, not card parts (Codex
# PR #117, P2).
_MAX_CARD_LETTERS = 2


def _card_letters(token: str) -> bool:
    """Letters that could belong to a card number.

    Only a to g (docs/14) and at most two of them. Length alone is not enough:
    "so" and "ab" both have two characters, but only one of them can be part
    of a card number. Otherwise "Nummer so 23" would become the non-existent
    number "so23" instead of a follow-up question under rule A (Codex PR #117).
    """
    return (
        token.isalpha()
        and len(token) <= _MAX_CARD_LETTERS
        and all(c in _SUFFIXES for c in token)
    )


# Single letters the recognition emits for swallowed syllables: 'n (einen),
# 's (es, das). Set apart after "Nummer" they are a word, not the prefix of a
# non-existent number (review PR #155); glued to the digits ("Nummer N12")
# they are a prefix (`_glued_card`).
_CLITICS = frozenset({"n", "s"})


def _prefix_letters(token: str) -> bool:
    """Letters set apart that can be a card prefix between "Nummer" and the
    number. Glued ones ("Nummer ZZ12") are read as a whole by `_glued_card`.

    Any single letter: the import allows every prefix (T-4.12), so "Nummer
    Z 12" is a named number that does not exist - `not_found` instead of a
    follow-up question (Codex PR #155, P2). Two letters only from a to g
    (`_card_letters`): "Nummer so 23" stays a word next to the number and
    therefore a follow-up question (Codex PR #117). A prefix the menu knows has
    already been read by `CardFormat`.
    """
    single = len(token) == 1 and token.isalpha() and token not in _CLITICS
    return single or _card_letters(token)


def _long_suffix(token: str) -> bool:
    """Several letters that together want to be a card suffix ("ab").

    Limited to a to g, the only letters a card number can carry (docs/14).
    That keeps "Nummer 23 mit Reis" the 23 with a word next to it, while
    "Nummer 23 ab" is invalid like "Nummer 23ab" - the recognition sets the
    space, not the guest (Codex PR #117, P2).
    """
    return len(token) == _MAX_CARD_LETTERS and _card_letters(token)


def _ref(tokens: list[str], span: _Span, marked: bool, glued: set[int]) -> ItemNumber:
    ref = _plain_ref(tokens, span, marked, glued)
    if not span.prefixes:
        return ref
    cards = tuple(prefix + ref.text for prefix in span.prefixes)
    if len(cards) == 1:
        return ItemNumber(ref.value, cards[0], marked, ref.valid)
    # What is said is what is looked up: "s12 oder sm12" (review PR #155).
    return ItemNumber(ref.value, " oder ".join(cards), marked, ref.valid, cards)


def _plain_ref(
    tokens: list[str], span: _Span, marked: bool, glued: set[int]
) -> ItemNumber:
    """The number without a prefix: digits or a number word, plus the suffix."""
    at = span.digits_at
    digits = span.end - at == 1 and tokens[at].isdigit()
    card = tokens[at] if digits else str(span.value)
    nxt = tokens[span.end] if span.end < len(tokens) else ""
    if digits and at in glued:
        suffix = nxt
        # "23x" ist keine Kartennummer und auch keine Endung: ungültig, nicht
        # die 23 (Codex PR #117). Echte Mengen ("2x Pho") hat _quantity_spans
        # vorher schon aussortiert, sie kommen hier nicht an.
        valid = suffix in _SUFFIXES
        return ItemNumber(span.value, card + suffix, marked, valid)
    # "Nummer 23 ab": a suffix of several letters, set apart. Written together
    # ("23ab") this was invalid all along; apart it used to give the bare 23 -
    # and only via find_item_number_ref, while the search asked back. Limited
    # to letters a card suffix can carry at all (a to g): "Nummer 23 mit Reis"
    # stays the 23 with a word next to it (Codex PR #117, P2).
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


_LEADING_ZEROS = re.compile(r"^([a-z]*)0*(\d)")


def canonical_card(card: str) -> str:
    """The card number as search_menu compares it: lower case, without leading
    zeros of the number, also after a prefix ("S07" like "s7"). One place for
    import, number words and the text phone; search._by_number computes the
    same in SQL."""
    return _LEADING_ZEROS.sub(r"\1\2", card.strip().lower()) or "0"


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


def _suffix_at(tokens: list[str], at: int) -> str:
    """A suffix at position `at`, valid or not ("g", "h", "ab"), else "".

    It belongs to a named number that does not exist ("Nummer 1000g", "Nummer
    Z 12 h"); otherwise it would be left over and the sentence would be unclear
    (Codex PR #155).
    """
    token = tokens[at] if at < len(tokens) else ""
    return token if token.isalpha() and (len(token) == 1 or _long_suffix(token)) else ""


def _said(prefix: str, number: str, suffix: str) -> str:
    """A named number that does not exist, the way it is read back: digits
    joined ("s1000g"), a number word set apart ("s tausend g") - otherwise it
    would read "stausend" (code review PR #155)."""
    if number.isdigit():
        return prefix + number + suffix
    return " ".join(part for part in (prefix, number, suffix) if part)


def _oversized_after_prefix(
    tokens: list[str],
    start: int,
    n: int,
    card: CardFormat,
    spelled: bool,
    marked: bool,
) -> tuple[int, ItemNumber] | None:
    """A prefix of the menu in `tokens[start:start+n]`, followed by a number
    above the card range ("S 1000", "S tausend", "Sushi 1000g"): named, but
    not existing. Returns the end of the number and the invalid number.

    One place for sentences with and without a marker (Codex PR #155).
    """
    at = start + n
    after = tokens[at] if at < len(tokens) else ""
    if not (after.isdigit() or _too_large(after)) or _scan(tokens, at) is not None:
        return None
    found = card.prefixes_of(tokens[start:at], spelled)
    if not found:
        return None
    suffix = _suffix_at(tokens, at + 1)
    text = " oder ".join(_said(p, after, suffix) for p in found)
    return at + 1 + bool(suffix), ItemNumber(0, text, marked, valid=False)


def _glued_card(
    tokens: list[str], j: int, glued: set[int], card: CardFormat
) -> tuple[int, int, _Span | None, ItemNumber | None]:
    """After "Nummer", a card number written in one piece: prefix, digits and
    suffix without a gap ("S12", "S12a", "Z12h", "ZZ1000zz", "JA12").

    One piece, as written, instead of putting every combination of known or
    unknown prefix, card range and suffix back together case by case (code
    review PR #155). If the menu knows the prefix and the number is in range,
    it is a number - `_ref` checks the suffix as for any number. Otherwise the
    whole piece is a named number that does not exist. Returns the same shape
    as `_marker_target`.
    """
    prefix, digits = tokens[j], tokens[j + 1]
    value = _word_value(digits)
    if prefix in card.prefixes and value is not None:
        return j, j, _Span(j, j + 2, value, 1, (prefix,)), None
    suffix = tokens[j + 2] if j + 1 in glued else ""
    bad = ItemNumber(0, prefix + digits + suffix, True, valid=False)
    return j, j + 2 + bool(suffix), None, bad


def _marker_target(
    tokens: list[str],
    i: int,
    prefixed: set[int],
    card: CardFormat = NO_PREFIXES,
    glued: set[int] | frozenset[int] = frozenset(),
) -> tuple[int, int, _Span | None, ItemNumber | None]:
    """What follows the marker at position `i`.

    Returns `(start, end, span, bad)`: `span` is a number that was read, `bad`
    a named one that cannot be a card number. `start` to `end` are the tokens
    that belong to `bad`. At most one of the two is set.

    One place for both callers - `_marked` for the understanding ladder and
    `sole_item_number` for the search. The logic used to exist twice, and the
    prefix case became invalid in only one copy: "Nummer A12" was `not_found`
    in the search but dish 12 via `find_item_number` (Codex PR #117, P2).

    Scan first, then skip: "Nummer ein und zwanzig" is 21; the "ein" opens the
    number and is not a filler word at this position.
    """
    j = i + 1
    while j < len(tokens):
        span = _scan(tokens, j)
        if span is not None:
            return j, j, span, None
        if j in prefixed and len(tokens[j]) <= _MAX_CARD_LETTERS:
            return _glued_card(tokens, j, glued, card)
        # "Nummer S 12", "Nummer Es zwölf": a prefix of the menu before the number.
        for n in range(_MAX_PREFIX_TOKENS, 0, -1):
            following = _scan(tokens, j + n) if j + n < len(tokens) else None
            # Glued ("Nummer ES12"), the letter name does not count (Codex
            # PR #155).
            spelled = j + n - 1 not in prefixed
            found = card.prefixes_of(tokens[j : j + n], spelled) if following else ()
            if following is not None and found:
                return j, j, _Span(j, following.end, following.value, n, found), None
            # "Nummer S 1000", "Nummer S tausend": known prefix, but the number
            # is above the card range - named, not existing (Codex PR #155).
            oversized = _oversized_after_prefix(tokens, j, n, card, spelled, True)
            if oversized is not None:
                return j, oversized[0], None, oversized[1]
        # Glued to the digits ("Nummer JA12", "Nummer HM12"), a word is not a
        # filler but a prefix - never silently the 12 (Codex PR #155).
        if j not in prefixed and (
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
    # Digits outside the card range ("Nummer 1000") carry their value; a number
    # word above it ("tausend", "eintausend") has none in the allowed range, so
    # `value` stays 0 and is never read, because `valid=False` makes the caller
    # turn off before that.
    if word.isdigit() or _too_large(word):
        # Glued ("Nummer 1000zz"), any suffix belongs to it; set apart, only
        # one that looks like a suffix (Codex PR #155).
        suffix = tokens[j + 1] if j in glued else _suffix_at(tokens, j + 1)
        value = int(word) if word.isdigit() else 0
        bad = ItemNumber(value, _said("", word, suffix), True, valid=False)
        return j, j + 1 + bool(suffix), None, bad
    # "Nummer A 12", "Nummer AB 12", "Nummer A zwölf": letters before the
    # number that the menu does not know as a prefix. The number may also come
    # as a word; the recognition delivers both (Codex PR #117, P1). Only card
    # letters count: "Nummer so 23" is a number next to a word and therefore a
    # follow-up question, not a non-existent number "so23" (Codex PR #117, P2).
    if _prefix_letters(word):
        following = _scan(tokens, j + 1)
        if following is not None:
            # A suffix belongs to the named number ("Nummer Z 12 g"), valid or
            # not ("Nummer Z 12 h"), as for numbers without a prefix; otherwise
            # it would be left over and the sentence unclear (Codex PR #155).
            end = following.end
            suffix = _suffix_at(tokens, end)
            end += bool(suffix)
            return (
                j,
                end,
                None,
                ItemNumber(0, word + str(following.value) + suffix, True, valid=False),
            )
        # "Nummer Z 1000", "Nummer Z tausend": a number above the card range
        # after an unknown prefix - named, not existing, no name search for
        # "z" (Codex PR #155).
        after = tokens[j + 1] if j + 1 < len(tokens) else ""
        if after.isdigit() or _too_large(after):
            suffix = _suffix_at(tokens, j + 2)
            bad = ItemNumber(0, _said(word, after, suffix), True, valid=False)
            return j, j + 2 + bool(suffix), None, bad
    return j, j, None, None


def _marked(
    tokens: list[str],
    glued: set[int],
    prefixed: set[int],
    card: CardFormat = NO_PREFIXES,
) -> list[ItemNumber]:
    spans: list[_Span] = []
    # Ziffern hinter dem Marker, die ausserhalb des Zahlbereichs liegen
    # ("Nummer 1000"): ungültig, und keine spätere Zahl darf nachrücken
    # ("Nummer 1000 und 23" ist nicht die 23, Codex PR #117).
    invalid: list[ItemNumber] = []
    for i, token in enumerate(tokens):
        if token not in _ITEM_NUMBER_MARKERS:
            continue
        _, _, span, bad = _marker_target(tokens, i, prefixed, card, glued)
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
            for span in _number_spans(tokens, card, prefixed)
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
        if all(_same_cards(r) != _same_cards(ref) for r in found):
            found.append(ref)
    return found


def _same_cards(ref: ItemNumber) -> frozenset[str]:
    return frozenset(canonical_card(c) for c in ref.cards)


def find_marked_item_numbers(
    text: str, card: CardFormat = NO_PREFIXES
) -> list[ItemNumber]:
    """Alle verschiedenen Nummern hinter "Nummer"/"Nr.", in Satzfolge - auch
    eine zweite Zahl ohne eigenen Marker ("Nummer 23 oder 24").

    Mehr als eine heisst: der Gast korrigiert sich ("Nummer 23, nein, Nummer
    24") oder stellt zur Wahl ("Nummer 23 oder Nummer 24"). Welche gilt, ist
    nicht entscheidbar - der Aufrufer fragt nach (Codex PR #117, P1).
    """
    return _marked(_tokens(text), _glued(text), _prefixed(text), card)


def find_item_number_ref(
    text: str, card: CardFormat = NO_PREFIXES
) -> ItemNumber | None:
    """Wie `find_item_number`, aber mit Kartenschreibweise und Marker.

    Zwei verschiedene ausdrücklich genannte Nummern ergeben `None`: die erste
    zu nehmen wäre geraten (CLAUDE.md §2 Regel 2).
    """
    tokens = _tokens(text)
    glued = _glued(text)
    prefixed = _prefixed(text)
    marked = _marked(tokens, glued, prefixed, card)
    if marked:
        return marked[0] if len(marked) == 1 else None

    mengen = _quantity_spans(tokens)
    uebrig = [
        span
        for span in _number_spans(tokens, card, prefixed)
        if not any(span.overlaps(menge) for menge in mengen)
    ]
    if len(uebrig) != 1:
        return None
    if not uebrig[0].prefixes and uebrig[0].start - 1 in prefixed:
        # "JA12", "A12" without a marker: letters glued to the number, so this
        # is not the bare 12 (Codex PR #155).
        return None
    ref = _ref(tokens, uebrig[0], marked=False, glued=glued)
    # "das ist 5g Zucker": g followed by a word is the unit, not a card suffix
    # - otherwise the ladder would report "Nummer 5g" (review PR #155).
    word_at = uebrig[0].end + 1
    unit = (
        ref.text.endswith("g")
        and word_at < len(tokens)
        and tokens[word_at].isalpha()
        and tokens[word_at] not in _SENTENCE_FILLER
        and tokens[word_at] not in _LEAD_FILLER
    )
    if unit:
        return None
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


def _prefixed_too_large(
    tokens: list[str], card: CardFormat, prefixed: set[int], taken: set[int]
) -> list[tuple[int, int, ItemNumber]]:
    """ "S1000", "S tausend", "Sushi 1000" without a marker: a prefix of the
    menu before a number above the card range. Named, not existing - no name
    search that would find a dish via "sushi" (Codex PR #155).

    Returns `(start, end, bad)` per occurrence. Tokens in `taken` already
    belong to a number after a marker.
    """
    found_all: list[tuple[int, int, ItemNumber]] = []
    i = 0
    while i < len(tokens):
        hit = None
        for n in range(_MAX_PREFIX_TOKENS, 0, -1):
            at = i + n
            if at >= len(tokens) or any(k in taken for k in range(i, at + 1)):
                continue
            spelled = _clean_lead(tokens, i) and at - 1 not in prefixed
            oversized = _oversized_after_prefix(tokens, i, n, card, spelled, False)
            if oversized is not None:
                hit = (i, *oversized)
                break
        if hit is None:
            i += 1
            continue
        found_all.append(hit)
        i = hit[1]
    return found_all


def sole_item_number(
    text: str, card: CardFormat = NO_PREFIXES
) -> tuple[ItemNumber | None, bool]:
    """Rule A: a card number only if the whole sentence is exactly one number.

    Allowed next to the number: "Nummer"/"Nr.", filler words, hesitations,
    punctuation and one quantity ("zweimal", "2x", "drei Portionen", "zwei
    Nummer 23"). Result:

    - `(ref, False)`: exactly one number and nothing else - the caller takes
      it (invalid ones such as "23h" or "Nummer 1000" as `valid=False`).
    - `(None, True)`: a number plus more (second number, text) - ask back.
    - `(None, False)`: not a number sentence. Without "Nummer", a number next
      to a name is a quantity ("zwei Frühlingsrollen") and the name search
      decides.

    One rule instead of many special cases: every sentence form that is not
    clearly one number becomes a follow-up question instead of a silent wrong
    pick (Codex reviews PR #117, 15 rounds).

    A number with a prefix of the menu ("S12", "Sushi zwölf") is never a
    quantity: directly next to a name ("S12 Lachs") the sentence asks back as
    with a marker. Separated from it by "und" or punctuation ("S12 und Pho
    Bo") it is two positions as with a plain number, and the name search and
    `position_parts` decide (T-4.12, code review PR #155). Only a number word
    without an article in front is the quantity ("zwei S zwölf").
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
        start, end, span, bad = _marker_target(tokens, i, prefixed, card, glued)
        if span is not None:
            marked.append(span)
            marker_at.add(i)
        elif bad is not None:
            invalid.append(bad)
            marker_at.add(i)
            consumed.update(range(start, end))
            invalid_at.update(range(start, end))
    taken = invalid_at | {k for s in marked for k in range(s.start, s.end)}
    for start, end, bad in _prefixed_too_large(tokens, card, prefixed, taken):
        invalid.append(bad)
        consumed.update(range(start, end))
        invalid_at.update(range(start, end))
    numbers = list(marked)
    spans = _number_spans(tokens, card, prefixed)
    prefix_at = {s.start for s in [*spans, *marked] if s.prefixes}

    def _quantity_before_prefix(span: _Span) -> bool:
        """ "zwei S zwölf": a number word without an article before the prefix
        is the quantity. Digits or an article in front ("die 23 S12") are a
        number of their own, and the sentence asks back (review PR #155)."""
        return (
            span.end in prefix_at
            and not any(tokens[k].isdigit() for k in range(span.start, span.end))
            and not (span.start > 0 and tokens[span.start - 1] in _LINK_ARTICLES)
        )

    for span in spans:
        if any(span.overlaps(m) for m in marked) or any(
            span.overlaps(q) for q in mengen
        ):
            continue
        if any(k in invalid_at for k in range(span.start, span.end)):
            continue
        if span.end in marker_at or _quantity_before_prefix(span):
            # "zwei Nummer 23", "zwei S zwölf": quantity before marker or prefix.
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
                and (span.digits_at in glued or len(after) == 1)
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
    # What is left over and could be a dish name. A word glued to the digits
    # ("JA12") is never a filler word (Codex PR #155).
    residue_at = [
        k
        for k, t in enumerate(tokens)
        if k not in consumed
        and (
            k in prefixed
            or (
                t not in PUNCTUATION
                and t not in _CONNECTORS
                and t not in _SENTENCE_FILLER
                and (t not in _LEAD_FILLER or k in after_marker)
                and t not in _HESITATIONS
                and t not in _ITEM_NUMBER_MARKERS
                and not _QUANTITY_SUFFIX.match(t)
            )
        )
    ]
    residue = [tokens[k] for k in residue_at]
    has_marker = bool(marked or invalid)

    def _apart(span: _Span) -> bool:
        """Is there an "und" or punctuation between the prefixed number and
        every leftover word? Then the rest is a position of its own (code
        review PR #155)."""

        def cut(a: int, b: int) -> bool:
            return any(
                tokens[x] in PUNCTUATION or tokens[x] == "und" for x in range(a, b)
            )

        return all(
            cut(span.end, k) if k >= span.end else cut(k + 1, span.start)
            for k in residue_at
        )

    # A marker that caught no number itself still counts - but only for a
    # number that comes after it: "Nummer Ente 23" is a number next to a name,
    # so a follow-up question instead of a name search for "ente" (Codex PR
    # #117, P2). If the number comes first, it does not belong to the marker:
    # in "zwei Fruehlingsrollen, Nummer weiss ich nicht" the two is a quantity
    # and the guest is saying they do not know the number - then the name
    # search decides.
    # A number with a prefix is never a quantity ("S12 Lachs"): directly next
    # to a name the sentence asks back, as with a marker (review PR #155).
    nummer_gemeint = (
        has_marker
        or any(s.prefixes and not _apart(s) for s in numbers)
        or any(
            span.start > i
            for i, tok in enumerate(tokens)
            if tok in _ITEM_NUMBER_MARKERS
            for span in numbers
        )
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
