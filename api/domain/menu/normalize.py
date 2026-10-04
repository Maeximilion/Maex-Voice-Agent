"""Make the guest's wording comparable (docs/11 §domain/menu).

An alias is stored the way the search later builds it from what was spoken.
Both sides must use the same function, otherwise "Die knusprigen Rollen!"
never matches "die knusprigen rollen".

Umlauts stay: the trigram search compares character sequences, and "Frühling"
against "Fruehling" is a miss there, not a hit. Filler words ("einmal ...
bitte") are removed by the search (T-4.3), not by the import - an alias from
the menu is already the wanted short form.
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
# Punctuation at the edge carries nothing on the phone; inside a word
# ("Wan-Tan") it stays. Plus the typographic quotation marks, written as
# escapes so they cannot be confused with a comma or apostrophe in the source.
_EDGE_PUNCT = " \t\"'.,;:!?()[]{}\u201e\u201c\u201d\u201a\u2018\u2019\u00ab\u00bb"


# What is said around the dish name on the phone and says nothing about the
# dish. Short on purpose: no word in here can ever be part of a hit.
#
# Every word with an umlaut is in here twice, once with it and once in the
# ae/oe/ue spelling: some speech recognition delivers "ich haette gern Pho"
# instead of "hätte". Without the second form "haette pho" would remain, the
# exact alias "pho" would no longer match and the trigram score could fall
# below the threshold (Codex PR #117, P2). Only this fixed list is rewritten,
# never the dish name itself - "Frühling" against "Fruehling" stays a miss.
FILLER = frozenset(
    {
        "ich", "wir", "hätte", "hätten", "möchte", "möchten", "nehme", "nehmen",
        "gern", "gerne", "bitte", "dann", "noch", "und", "also", "ja", "äh", "ähm",
        "hm", "mal", "einmal", "die", "der", "das", "den", "dem", "des", "ein",
        "eine", "einen", "einem", "einer", "nummer", "nr", "x", "portion",
        "portionen", "von", "vom",
        # the same words as the recognition writes them without an umlaut,
        # plus the remaining hesitations from numberwords._HESITATIONS
        "haette", "haetten", "moechte", "moechten", "aeh", "aehm", "aehh",
        "hmm", "ehm", "oehm",
        # like numberwords._LEAD_FILLER (review PR #147)
        "würde", "würden", "würd", "wuerde", "wuerden", "wuerd", "hallo",
        "guten", "tag", "abend", "dazu", "bestellen", "bestelle",
    }
)  # fmt: skip


# Quantity words that follow a number ("2 Stück", "2 stk", "3 x") - like
# numberwords._QUANTITY_NOUNS, here with the umlaut because normalize_alias
# keeps umlauts. Only removed directly after a number: "Stück" on its own can
# be part of a name.
_QUANTITY_NOUNS = frozenset(
    {"x", "portion", "portionen", "stück", "stueck", "stk", "st"}
)
# A card letter set apart ("23 a"), like numberwords._SUFFIXES, and "2x":
# number and quantity sign in one word.
_SUFFIX_LETTERS = _NAME_SUFFIXES
_COMPACT_QUANTITY = re.compile(r"\d+x")


def _is_number_word(token: str) -> bool:
    # "23a": a card number with a letter, not part of the dish name.
    if _CARD_NUMBER.fullmatch(token) or _COMPACT_QUANTITY.fullmatch(token):
        return True
    if parse_cardinal(token) is not None:
        return True
    # "zweimal", "dreimal": a quantity, not part of the dish name.
    return token.endswith("mal") and parse_cardinal(token[:-3]) is not None


def normalize_query(text: str) -> str:
    """Reduce what was spoken to the dish name: like an alias, without filler
    words and without numbers and quantities.

    "Ich hätte gern zweimal die knusprige Ente, bitte" -> "knusprige ente",
    "2 Stück Pho" -> "pho". Quantity forms as in numberwords (Codex PR #117).
    The number itself is evaluated by search_menu before (number or quantity).
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
            # "2 Stück", "die 23 a": belongs to the number, not to the dish name.
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
    # lower() instead of casefold(): casefold turns "Soße" into "sosse".
    text = unicodedata.normalize("NFC", text).lower()
    text = _SPACE.sub(" ", text)
    return text.strip(_EDGE_PUNCT).strip()
