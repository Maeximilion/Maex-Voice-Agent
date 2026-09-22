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

Optionen ("Nummer 23 mit Erdnusssauce") trennt das nicht: "mit" hängt an der
Position, die Sauce ist Option und kein zweites Gericht.
"""

import re

from api.domain.menu.numberwords import fold, parse_cardinal

_SEPARATOR = re.compile(r"\s*,\s*|\s+(?:und|sowie)\s+", re.IGNORECASE)
_WORD = re.compile(r"\d+|[a-z]+")
# Nach fold() (ä -> ae). Deckungsgleich mit numberwords._ALTERNATIVE_WORDS.
_CORRECTION_WORDS = frozenset(
    {"oder", "nein", "bzw", "beziehungsweise", "sondern", "lieber", "statt", "anstatt"}
)
_EMPTY_WORDS = frozenset(
    {"aeh", "aehm", "aehh", "hm", "hmm", "ehm", "oehm", "bitte", "dann", "noch", "auch"}
)


def split_positions(text: str) -> list[str]:
    """Teile je Position, in der gesprochenen Reihenfolge. Nicht sicher trennbar → [text]."""
    stripped = text.strip()
    words = _WORD.findall(fold(stripped))
    if not words or _CORRECTION_WORDS.intersection(words):
        return [stripped] if stripped else []

    parts = [p.strip(" .!?;:") for p in _split(stripped)]
    if any(not _content(p) for p in parts):
        return [stripped]
    return parts


def _split(text: str) -> list[str]:
    """Am Trenner zerlegen, "drei und zwanzig" dabei zusammenlassen."""
    pieces = _SEPARATOR.split(text)
    separators = _SEPARATOR.findall(text)
    parts = [pieces[0]]
    for sep, piece in zip(separators, pieces[1:], strict=True):
        left = _WORD.findall(fold(parts[-1]))
        right = _WORD.findall(fold(piece))
        if (
            sep.strip().casefold() == "und"
            and left
            and right
            and parse_cardinal(f"{left[-1]} und {right[0]}") is not None
        ):
            parts[-1] += sep + piece
        else:
            parts.append(piece)
    return parts


def _content(part: str) -> bool:
    return any(w not in _EMPTY_WORDS for w in _WORD.findall(fold(part)))
