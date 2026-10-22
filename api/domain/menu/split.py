"""Ein Satz, mehrere Positionen: vor `search_menu` zerlegen (T-4.5, docs/04 §search_menu).

`search_menu` nimmt je Aufruf genau eine Position (Regel A). Ein Satz wie "die
23 und einmal Pho Bo" verliert dort die 23 still: ohne Nummer-Marker ist er
kein Nummernsatz, und die Namenssuche findet nur Pho Bo. Deshalb zerlegt der
Bestellfluss den Satz vorher und fragt je Teil einzeln.

Getrennt wird an "und", "sowie" und Komma - aber nur, wenn das Ergebnis sicher
ist. Im Zweifel bleibt der Satz ganz, und `search_menu` antwortet laut mit
`ambiguous` statt eine Position zu verschlucken (CLAUDE.md §2 Regel 2):

- Korrekturen und Alternativen ("23, nein 24", "23 oder 24") bleiben ganz:
  gemeint ist eine Position, nicht zwei.
- Ein Teil nur aus Zögerlauten oder Füllwörtern ("23, äh, 24") heißt
  Selbstkorrektur, nicht Aufzählung - der Satz bleibt ganz.
- "drei und zwanzig" ist eine Zahl, kein "drei" und "zwanzig".
- Jeder Teil muss selbst eine Position eröffnen und damit **beginnen**: mit
  Nummer, Menge ("einmal", "zwei") oder "ein/eine". "Schärfe 2" eröffnet
  keine. Sonst ist er Teil eines Namens oder ein Hinweis -
  "Ente süß und sauer", "Nummer 23, ohne Zwiebeln" - und der Satz bleibt ganz
  (Codex PR #124). Beginnt ein Teil nach der Menge mit "ohne", "mit" oder
  "extra", ist er ein Hinweis zur Position davor, keine neue.

Optionen ("Nummer 23 mit Erdnusssauce") trennt das nicht: "mit" hängt an der
Position, die Sauce ist Option und kein zweites Gericht.
"""

import re

from api.domain.menu.numberwords import (
    find_quantity,
    fold,
    hesitation,
    inside_split,
    parse_cardinal,
    pause_before_und,
    tens_half,
)

# A separator takes the whitespace behind it, not the whitespace in front: a
# pattern that begins with `\s*` or `\s+` reads a long run of spaces again from
# every character of it (CodeQL py/polynomial-redos). The whitespace in front
# stays at the end of the piece before; `_strip` takes it off.
_SEPARATOR = re.compile(r",\s*|\s(?:und|sowie)\s+", re.IGNORECASE)
_WORD = re.compile(r"\d+|[a-z]+")
# Nach fold() (ä -> ae). Deckungsgleich mit numberwords._ALTERNATIVE_WORDS.
_CORRECTION_WORDS = frozenset(
    {"oder", "nein", "bzw", "beziehungsweise", "sondern", "lieber", "statt", "anstatt"}
)
_ARTICLES = frozenset({"ein", "eine", "einen", "einem", "einer"})
_DETERMINERS = frozenset({"die", "der", "das", "den"})
_NUMBER_MARKERS = frozenset({"nummer", "nr", "no"})
# Nach fold(). Deckungsgleich mit numberwords._QUANTITY_NOUNS.
_QUANTITY_NOUNS = frozenset({"mal", "x", "portion", "portionen", "stueck", "stk", "st"})
_MODIFIERS = frozenset({"ohne", "mit", "extra", "aber"})
# The first half of "drei und zwanzig".
_UNITS = range(1, 10)
# Next to every hesitation (`_full_words`, numberwords.hesitation).
_EMPTY_WORDS = frozenset(
    {
        "bitte",
        "dann",
        "noch",
        "auch",
        # Einleitend vor einer neuen Position: "und dazu eine Cola" (Codex PR #124)
        "dazu",
        "ausserdem",
        "zusaetzlich",
    }
)


def split_positions(text: str) -> list[str]:
    """Teile je Position, in der gesprochenen Reihenfolge. Nicht sicher trennbar → [text]."""
    stripped = text.strip()
    if not _separable(stripped):
        return [stripped] if stripped else []
    return [_strip(p) for p in _split(stripped)]


def raw_pieces(text: str) -> list[str]:
    """Nur an den Trennern geschnitten, ohne die Pruefung, ob ein Teil eine
    Position eroeffnet. Fuer den Gegencheck mit der Karte (search.position_parts):
    ob "die 23 und Pho Bo" zwei Gerichte sind, entscheidet nicht der Satz,
    sondern ob jedes Stueck fuer sich ein anderes Gericht trifft. Dieselben
    Sperren wie oben: Korrektur und Zoegerlaut ("23, aeh, 24") bleiben ganz, und
    ein Stueck mit "mit", "ohne", "extra" vorn haengt an dem davor ("die 23, mit
    Reis"), auch wenn es selbst ein Gericht traefe (Codex PR #127, P1)."""
    stripped = text.strip()
    if not _separable(stripped):
        return [stripped] if stripped else []
    return [_strip(p) for p in _split(stripped, by_position=False)]


def _strip(part: str) -> str:
    return part.strip().strip(" .!?;:")


def separator_pieces(text: str) -> int:
    """Wie viele Stuecke ein Text an den Trennern hat, ohne jede Pruefung. Fuer
    die Grenze der Spannen in search.position_parts: "Fisch und Chips" hat zwei."""
    return len(_SEPARATOR.split(text.strip()))


def _separable(text: str) -> bool:
    words = _WORD.findall(fold(text))
    if not words or _CORRECTION_WORDS.intersection(words):
        return False
    return all(_content(p) for p in _SEPARATOR.split(text))


def _split(text: str, *, by_position: bool = True) -> list[str]:
    """Am Trenner zerlegen. Ein Teil, der keine eigene Position eröffnet, hängt
    wieder an dem davor: "eine Ente süß und sauer" bleibt ein Name, die Grenze
    vor "die 23" bleibt trotzdem stehen (Codex PR #124).

    Zahlen mit "und" bleiben ebenso zusammen.

    "drei und zwanzig" ist 23. Nach "hundert" ist ein "und" immer Teil der Zahl
    ("hundert und eins", "zweihundert und drei"): parse_cardinal kennt diese
    Form nicht, getrennt würde daraus 100 und 1 (Codex PR #124). Im Zweifel
    bleibt der Satz so ganz und search_menu fragt nach.
    """
    pieces = _SEPARATOR.split(text)
    separators = _SEPARATOR.findall(text)
    parts = [pieces[0]]
    for sep, piece in zip(separators, pieces[1:], strict=True):
        left = _WORD.findall(fold(parts[-1]))
        right = _WORD.findall(fold(piece))
        # A pause at the "und" ("drei und äh zwanzig", "drei äh und zwanzig")
        # does not split the number into the 3 and the 20: the sentence stays
        # whole and `sole_item_number` asks, also next to a prefix, a letter
        # or a name (Codex PR #147). The words here carry no punctuation; a
        # comma is a separator of its own (#328).
        a = len(left)
        while a and pause_before_und(left[a - 1]):
            a -= 1
        b = 0
        while b < len(right) and inside_split(right[b]):
            b += 1
        und = sep.strip().casefold() == "und"
        # A comma next to the "und" is the same pause: "die drei, und
        # zwanzig", "die drei und, zwanzig". A sentence that starts with the
        # second ("und äh, zwanzig") is the same cut-off half. Kept whole,
        # each asks instead of taking item 20 for a spoken 23 (#328).
        cut_off = False
        if not und and b < len(right) and right[b] == "und":
            und = True
            b += 1
            while b < len(right) and inside_split(right[b]):
                b += 1
        elif not und and a and left[a - 1] == "und":
            und = True
            a -= 1
            while a and pause_before_und(left[a - 1]):
                a -= 1
            cut_off = not a
        number_joined = (
            und
            and b < len(right)
            and (
                (cut_off and tens_half(right[b]))
                # "hundert und eins": nur eine Zahl hängt an, keine neue
                # Position ("die hundert und eine Cola", Codex PR #124).
                or (a > 0 and left[a - 1].endswith("hundert") and _only_number(piece))
                # "drei und zwanzig" is 23, "zwei und zwanzigmal" one number too.
                or (
                    a > 0
                    and parse_cardinal(left[a - 1]) in _UNITS
                    and tens_half(right[b])
                )
            )
        )
        attached = not _opens_position(piece) if by_position else _modifies(piece)
        if number_joined or attached:
            parts[-1] += sep + piece
        else:
            parts.append(piece)
    return parts


def _opens_position(part: str) -> bool:
    """Eröffnet der Teil selbst eine Position? Nur, wenn er damit **beginnt**.

    Vorn steht ein Präfix aus Artikel ("eine"), Menge ("zweimal", "2 x", "zwei
    Portionen"), Nummer ("23", "Nummer 23") und "die/der/das/den". Eine Zahl
    weiter hinten ("Schärfe 2", "für 2 Personen") eröffnet keine Position.
    Direkt nach dem ganzen Präfix darf kein Hinweis stehen: "2 x ohne
    Koriander" gehört zur Position davor (Codex PR #124). Besteht der Teil nur
    aus dem Präfix, muss eine Nummer darin sein ("die 13"), sonst ist er leer.
    """
    words = _full_words(part)
    prefix = 0
    while prefix < len(words) and _is_prefix_word(words[prefix]):
        prefix += 1
    rest = words[prefix:]
    # Nur "die" oder "Nummer" davor eröffnet nichts: es braucht Artikel, Menge
    # oder Zahl ("Nummer weiß ich nicht" ist keine Position).
    if not any(_opens(w) for w in words[:prefix]):
        return False
    if rest:
        return rest[0] not in _MODIFIERS
    return any(_is_number(w) for w in words)


def _modifies(part: str) -> bool:
    """Ein Hinweis zur Position davor: nach Artikel und Menge steht vorn "mit",
    "ohne", "extra" oder "aber" ("mit Reis", "2 x ohne Koriander")."""
    words = _full_words(part)
    rest = words[next((i for i, w in enumerate(words) if not _is_prefix_word(w)), 0) :]
    return bool(rest) and rest[0] in _MODIFIERS


def _is_prefix_word(word: str) -> bool:
    return (
        word in _DETERMINERS
        or word in _ARTICLES
        or word in _NUMBER_MARKERS
        or word in _QUANTITY_NOUNS
        or _is_number(word)
        or find_quantity(word) is not None
    )


def _opens(word: str) -> bool:
    return word in _ARTICLES or _is_number(word) or find_quantity(word) is not None


def _is_number(word: str) -> bool:
    return word.isdigit() or parse_cardinal(word) is not None


def _content(part: str) -> bool:
    return bool(_full_words(part))


def _only_number(part: str) -> bool:
    words = _full_words(part)
    return len(words) == 1 and _is_number(words[0])


def _full_words(part: str) -> list[str]:
    """The words of the part without hesitations and empty words."""
    return [
        w
        for w in _WORD.findall(fold(part))
        if w not in _EMPTY_WORDS and not hesitation(w)
    ]
