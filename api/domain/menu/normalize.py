"""Kundensprache vergleichbar machen (docs/11 §domain/menu).

Ein Alias wird so gespeichert, wie ihn die Suche später auch aus dem Gesprochenen
bildet. Beide Seiten müssen dieselbe Funktion benutzen, sonst trifft "Die
knusprigen Rollen!" nie "die knusprigen rollen".

Umlaute bleiben: die Trigram-Suche vergleicht Zeichenfolgen, und "Frühling"
gegen "Fruehling" ist dort ein Fehlertreffer, kein Treffer. Füllwörter
("einmal ... bitte") entfernt erst die Suche (T-4.3), nicht der Import - ein
Alias aus der Karte ist schon die gewünschte Kurzform.
"""

import re
import unicodedata

from api.domain.menu.numberwords import CARD_SUFFIXES, parse_cardinal

_SPACE = re.compile(r"\s+")
# Suffixes that belong to the number inside a name. Without g: next to a name
# "250g" is a weight ("Rumpsteak 250g") and tells two dishes apart; on its own
# ("die 25g") it was the card number, which search_menu has already evaluated
# (code review PR #155).
_NAME_SUFFIXES = CARD_SUFFIXES - {"g"}
_WEIGHT = re.compile(r"\d+g")
# Only real card forms (like importer._CARD_NUMBER): "23", "23a", "07". An
# alias such as "7up" is not a number and stays (Codex PR #117). A prefix
# ("s12") is known only to the menu; search_menu has already evaluated the
# number (numberwords.CardFormat, T-4.12).
_CARD_NUMBER = re.compile(r"\d+[" + "".join(sorted(_NAME_SUFFIXES)) + "]?")
# Satzzeichen am Rand tragen am Telefon nichts; im Wort ("Wan-Tan") bleiben sie.
# Dazu die typografischen Anfuehrungszeichen, als Escape geschrieben, damit sie
# im Quelltext nicht mit Komma oder Apostroph zu verwechseln sind.
_EDGE_PUNCT = " \t\"'.,;:!?()[]{}\u201e\u201c\u201d\u201a\u2018\u2019\u00ab\u00bb"


# Was am Telefon um den Gerichtnamen herum gesagt wird und nichts über das
# Gericht sagt. Bewusst kurz: jedes Wort hier kann nie Teil eines Treffers sein.
#
# Jedes Wort mit Umlaut steht zweimal drin, einmal mit und einmal in der
# ae/oe/ue-Schreibweise: manche Spracherkennung liefert "ich haette gern Pho"
# statt "hätte". Ohne die zweite Form bliebe "haette pho" stehen, der exakte
# Alias "pho" träfe nicht mehr und der Trigram-Wert könnte unter die Schwelle
# fallen (Codex PR #117, P2). Umgeschrieben wird nur diese feste Liste, nie der
# Gerichtname selbst - "Frühling" gegen "Fruehling" bleibt ein Fehlertreffer.
FILLER = frozenset(
    {
        "ich", "wir", "hätte", "hätten", "möchte", "möchten", "nehme", "nehmen",
        "gern", "gerne", "bitte", "dann", "noch", "und", "also", "ja", "äh", "ähm",
        "hm", "mal", "einmal", "die", "der", "das", "den", "dem", "des", "ein",
        "eine", "einen", "einem", "einer", "nummer", "nr", "x", "portion",
        "portionen", "von", "vom",
        # dieselben Wörter, wie die Erkennung sie ohne Umlaut schreibt,
        # dazu die übrigen Zögerlaute aus numberwords._HESITATIONS
        "haette", "haetten", "moechte", "moechten", "aeh", "aehm", "aehh",
        "hmm", "ehm", "oehm",
        # wie numberwords._LEAD_FILLER (Review PR #147)
        "würde", "würden", "würd", "wuerde", "wuerden", "wuerd", "hallo",
        "guten", "tag", "abend", "dazu", "bestellen", "bestelle",
    }
)  # fmt: skip


# Mengenwörter, die nach einer Zahl stehen ("2 Stück", "2 stk", "3 x") - wie
# numberwords._QUANTITY_NOUNS, hier mit Umlaut, weil normalize_alias die
# Umlaute behält. Nur direkt nach einer Zahl entfernt: "Stück" allein kann
# Teil eines Namens sein.
_QUANTITY_NOUNS = frozenset(
    {"x", "portion", "portionen", "stück", "stueck", "stk", "st"}
)
# "2x": Zahl und Mengenzeichen in einem Wort.
# Abgesetzter Kartenbuchstabe ("23 a"), wie numberwords._SUFFIXES.
_SUFFIX_LETTERS = _NAME_SUFFIXES
_COMPACT_QUANTITY = re.compile(r"\d+x")


def _is_number_word(token: str) -> bool:
    # "23a": Kartennummer mit Buchstabe, kein Teil des Gerichtnamens.
    if _CARD_NUMBER.fullmatch(token) or _COMPACT_QUANTITY.fullmatch(token):
        return True
    if parse_cardinal(token) is not None:
        return True
    # "zweimal", "dreimal": Menge, kein Teil des Gerichtnamens.
    return token.endswith("mal") and parse_cardinal(token[:-3]) is not None


def normalize_query(text: str) -> str:
    """Das Gesprochene auf den Gerichtnamen verkürzen: wie ein Alias, ohne
    Füllwörter und ohne Zahl- und Mengenangaben.

    "Ich hätte gern zweimal die knusprige Ente, bitte" -> "knusprige ente",
    "2 Stück Pho" -> "pho". Mengenformen wie in numberwords (Codex PR #117).
    Die Zahl selbst wertet search_menu vorher aus (Nummer oder Menge).
    """
    kept: list[str] = []
    weights = 0
    after_number = False
    for raw in normalize_alias(text).split(" "):
        token = raw.strip(_EDGE_PUNCT)
        if not token:
            continue
        if _is_number_word(token):
            after_number = True
            continue
        if after_number and (token in _QUANTITY_NOUNS or token in _SUFFIX_LETTERS):
            # "2 Stück", "die 23 a": gehört zur Zahl, nicht zum Gerichtnamen.
            continue
        weight = _WEIGHT.fullmatch(token) is not None or (after_number and token == "g")
        after_number = False
        if weight:
            weights += 1
            kept.append(token)
        elif token not in FILLER:
            kept.append(token)
    # Without a name next to it, "25g" was the card number, not a weight.
    return "" if weights == len(kept) else " ".join(kept)


def normalize_alias(text: str) -> str:
    """Kleinschreibung, eine Form je Zeichen (NFC), einfache Leerzeichen, kein Rand."""
    # lower() statt casefold(): casefold macht aus "Soße" "sosse".
    text = unicodedata.normalize("NFC", text).lower()
    text = _SPACE.sub(" ", text)
    return text.strip(_EDGE_PUNCT).strip()
