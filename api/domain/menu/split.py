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
    parse_cardinal,
)

_SEPARATOR = re.compile(r"\s*,\s*|\s+(?:und|sowie)\s+", re.IGNORECASE)
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
_EMPTY_WORDS = frozenset(
    {
        "aeh",
        "aehm",
        "aehh",
        "hm",
        "hmm",
        "ehm",
        "oehm",
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
    return [p.strip(" .!?;:") for p in _split(stripped)]


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
    return [p.strip(" .!?;:") for p in _split(stripped, by_position=False)]


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
        number_joined = (
            sep.strip().casefold() == "und"
            and left
            and right
            and (
                # "hundert und eins": nur eine Zahl hängt an, keine neue
                # Position ("die hundert und eine Cola", Codex PR #124).
                (left[-1].endswith("hundert") and _only_number(piece))
                or parse_cardinal(f"{left[-1]} und {right[0]}") is not None
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
    words = [w for w in _WORD.findall(fold(part)) if w not in _EMPTY_WORDS]
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
    words = [w for w in _WORD.findall(fold(part)) if w not in _EMPTY_WORDS]
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
    return any(w not in _EMPTY_WORDS for w in _WORD.findall(fold(part)))


def _only_number(part: str) -> bool:
    words = [w for w in _WORD.findall(fold(part)) if w not in _EMPTY_WORDS]
    return len(words) == 1 and _is_number(words[0])
