"""Wunsch zu einer Position: vom Gericht trennen und einordnen (T-4.10, D8).

"die 23 ohne Karotten" fand vorher nichts - der Wunsch lief mit in die Suche.
Hier wird er abgetrennt und eingeordnet, bevor der Agent etwas zusagt:

- `note`: Weglassen ("ohne Karotten", "keine Zwiebeln"). Ein Hinweis fuer die
  Kueche, ohne Preis.
- `option`: was als Option des Gerichts auf der Karte steht ("mit Nudeln statt
  Reis"). Gruppe, Name, Aufpreis und Begruendung kommen aus `item_options`,
  nie vom Modell (CLAUDE.md §2 Regel 1).
- `allergy`: eine eigene Allergie ist kein Wunsch. Sie geht als Hinweis im
  festen Wortlaut an die Kueche (E14), ohne Zusage, dass das Gericht frei davon
  ist (CLAUDE.md §9). Eine Frage nach den Allergenen eines Gerichts ist keine
  eigene Allergie, sondern der Allergenpfad (`get_item_details`).
- `unknown`: alles andere. Was die Karte nicht kennt, bietet der Agent am
  Telefon nicht an (Entscheidung D8, 24.09.2026).
- `open`: noch nicht entscheidbar - bei mehreren Treffern fuer das Gericht oder
  wenn die Option in mehreren Gruppen steht (`groups`); der Agent fragt.

Weglassen zusammen mit einer Zugabe ("ohne Zwiebeln, dafür mit Nudeln") wird
nie zur freien Notiz: die Zugabe ist Option oder unbekannt, das Weglassen kommt
als `note` mit. Sonst bekaeme die Kueche eine Zugabe ohne Preis (Review PR #139).

Reine Funktionen ohne Datenbank.
"""

import re
from bisect import bisect_left, bisect_right
from collections.abc import Callable, Iterator
from itertools import pairwise

from api.domain.menu.numberwords import fold, parse_cardinal
from api.schemas.menu import OptionGroup, Wish
from api.schemas.orders import NOTE_MAX

# Nach fold() (ä -> ae).
_REMOVE = frozenset({"ohne", "kein", "keine", "keinen", "keinem"})
_ADD = frozenset({"mit", "extra"})
_INSTEAD = frozenset({"statt", "anstatt"})
_LEAD_FILLER = frozenset({"bitte", "aber", "und", "dann", "dafuer"})
# Verbindet Teile eines Wunsches, ohne selbst etwas zu wuenschen.
_GLUE = frozenset({"gern", "gerne", "dazu", "noch", "als", "die", "der", "das", "den"})
_ARTICLES = frozenset({"ein", "eine", "einen", "einem", "einer"})
# Womit ein Satzteil zur eigenen Allergie beginnt, auch ohne Komma davor.
# "weil", "wegen", "denn": the reason in front of the allergy belongs to its
# clause, not to the wish before it ("ohne Zwiebeln, weil ich ...", #252).
_CLAUSE_OPENERS = frozenset(
    {"ich", "wir", "mein", "meine", "meinem", "meiner", "weil", "wegen", "denn"}
)

_WORD = re.compile(r"[^\W_]+")
_TOKEN = re.compile(r"[^\W_]+|,")
# The patterns below run on what the caller said and must stay linear in its
# length (CodeQL py/polynomial-redos). None of them begins with a repetition: a
# pattern that may start anywhere and begins with `[\s,]*` reads a long run of
# separators again from every character of it. The separators in front of a
# match are taken off by `_separators_start` instead.
#
# "bitte" at the very end, and the word that joins the next part of a wish
# ("ohne Zwiebeln, dafür" / "mit Nudeln, aber").
_PLEASE_AT_END = re.compile(r"bitte[\s.!?]*$", re.IGNORECASE)
# Lower case only, as the pattern it replaces was: "Und" at the end stays.
_JOINER_AT_END = re.compile(r"(?:dafür|dafuer|aber|und)[\s,]*$")
# Joining words at the end of a part, one after the other ("mit Salami, aber").
_GLUE_WORD = re.compile(r"aber|und|dafür|dafuer|dann|bitte", re.IGNORECASE)
# Eine Menge gehoert zur Position, nie in den Hinweis ("zweimal", "2 x").
_TIMES = re.compile(r"\b(\w+?)mal\b", re.IGNORECASE)
_COUNTED = re.compile(r"\b\d+\s*(?:x|portionen?|stück|stueck)\b", re.IGNORECASE)


# Fester Wortlaut des Kuechenhinweises (E14, Maxi 24.09.2026): wird beim
# Vorlesen wiederholt, nie mit der Zusage, das Gericht sei frei davon.
ALLERGY_NOTE = "WICHTIG: Keine {ingredient}. Grund: Allergie"
# Die Zutaten reichen bis zum naechsten Satzteil: "gegen Erdnuesse und Sesam" sind
# zwei (Codex PR #139, P1), "gegen Sesam und dann noch eine Cola" ist eine.
# Der Rest steht im Lookahead: so findet die Suche auch die naechste Wendung
# ("allergisch gegen X und allergisch gegen Y", Codex PR #139, P1).
_REST = r"(?=(.+)$)"
# "ich bin gegen Nuesse allergisch" is read by `_against_allergic`.
_INGREDIENT = (
    # "allergisch gegen Sesam", "Allergie gegen Sellerie"
    re.compile(
        r"(?:allergisch|allergie|unvertr(?:ä|ae)glichkeit|intoleran\w*)\s+"
        r"(?:gegen|auf)\s+" + _REST,
        re.IGNORECASE,
    ),
    # "ich vertrage keine Erdnuesse"
    re.compile(r"vertr(?:a|ä|ae)g\w*\s+(?:keine[nm]?|kein)\s+" + _REST, re.IGNORECASE),
)
# "Erdnussallergie", "Erdnuss-Allergie" und der Wortanfang in "Nuss- und
# Sesamallergie": jede gilt, nicht nur die erste (Codex PR #139, P1).
#
# Read by `_compound_stems`, one word at a time. As a single pattern,
# `(\w+?)-?(?:allergie|...)|(\w+)-(?=\s*(?:,|und|oder|sowie)\s)`, the search
# began again at every letter of a long word (CodeQL py/polynomial-redos).
_ALLERGY_NOUN = re.compile(
    r"allergie|intoleranz|unvertr(?:ä|ae)glichkeit", re.IGNORECASE
)
_LIST_WORD = re.compile(r"(?:,|und|oder|sowie)\s", re.IGNORECASE)
_WORD_RUN = re.compile(r"\w+")
# Nach einem "und" oder Komma beginnt hier ein neuer Satzteil, keine Zutat mehr.
_CLAUSE_WORDS = (
    frozenset(
        {"dann", "noch", "ich", "wir", "bitte", "aber", "dazu", "auch", "ausserdem"}
    )
    | _ARTICLES
    | frozenset({"die", "der", "das", "den", "einmal"})
)
_PIECE = re.compile(r"[^\W_]+|[,.;!?]")
# Kein Allergen, nur die Art der Allergie: "Nahrungsmittelallergie" - dann bleibt
# "Wogegen?" offen (Review PR #139).
_GENERIC_STEMS = frozenset(
    {"nahrungsmittel", "lebensmittel", "essens", "speise", "kreuz", "kontakt"}
)
# Bestaetigung nach der Zutat ("Sesam, ja genau") beendet die Liste (Review PR #139).
_AFFIRMATIONS = frozenset({"ja", "genau", "richtig", "stimmt", "sicher"})
# Unsicherheit oder Ablehnung ist keine Zutat: "weiss ich nicht", "nein", "keine
# Ahnung" - dann bleibt die Frage offen (Codex PR #139, P1).
_NO_INGREDIENT = frozenset(
    {"nein", "ja", "nicht", "nichts", "weiss", "ahnung", "egal", "vielleicht"}
    | {"unsicher", "sicher", "genau", "irgendwas", "irgendwelche"}
)


def _words(text: str) -> list[str]:
    return [fold(w) for w in _WORD.findall(text)]


def _tokens(text: str) -> tuple[list[re.Match[str]], list[str]]:
    """The words and commas of `text`, and the same folded."""
    tokens = list(_TOKEN.finditer(text))
    return tokens, [fold(token.group()) for token in tokens]


def _is_allergy(word: str) -> bool:
    """Eine eigene Allergie. "Allergene" ist die Frage nach dem Gericht und
    gehoert in den Allergenpfad, nicht hierher (Review PR #139)."""
    # "allergiefrei", "allergenfrei": die Frage nach dem Gericht (Codex PR #139).
    if word.startswith("allergen") or word.endswith("frei"):
        return False
    return (
        "allerg" in word
        or "unvertraeglich" in word
        # "Laktoseintoleranz" ist Allergie-Rede wie in docs/17 (Codex PR #139).
        or "intoleran" in word
        or word.startswith(("vertrag", "vertraeg"))
    )


def _mentions_allergy(words: list[str], i: int) -> bool:
    """Ist das Wort an Stelle i eine eigene Allergie? "vertrage" nur mit
    Verneinung danach: "ich vertrage keine Erdnuesse", nicht "ich vertrage
    alles" (Review PR #139)."""
    word = words[i]
    if word.startswith(("vertrag", "vertraeg")):
        return any(w in _REMOVE or w == "nicht" for w in words[i + 1 : i + 3])
    return _is_allergy(word)


def _ingredient(text: str) -> str | None:
    """Die Zutaten, wie der Gast sie sagt: "Erdnussallergie" -> "Erdnuss",
    "allergisch gegen Sesam" -> "Sesam", mehrere in Reihenfolge ("Milch und
    Nuss"). None: nicht erkennbar, nachfragen."""
    found: list[tuple[int, str]] = []
    for pattern in _INGREDIENT:
        for match in pattern.finditer(text):
            found.append((match.start(1), _until_new_clause(match.group(1))))
    for start, rest in _against_allergic(text):
        found.append((start, _until_new_clause(rest)))
    for start, stem, at_noun in _compound_stems(text):
        if fold(stem) in _GENERIC_STEMS:
            continue
        found.append((start, stem))
        if at_noun:
            found.extend(_stems_before(text[:start]))
    pieces: list[str] = []
    for _, piece in sorted(found):
        if not piece or _NO_INGREDIENT & set(_words(piece)):
            return None
        piece = piece[0].upper() + piece[1:]
        if piece not in pieces:
            pieces.append(piece)
    if not pieces:
        return None
    if len(pieces) == 1:
        return pieces[0]
    return ", ".join(pieces[:-1]) + " und " + pieces[-1]


_GEGEN = re.compile(r"gegen", re.IGNORECASE)
_GEGEN_WORD = re.compile(r"\bgegen\b", re.IGNORECASE)
_ALLERGISCH = re.compile(r"allergisch", re.IGNORECASE)
_SPACES = re.compile(r"\s+")
_SENTENCE_END = re.compile(r"[.;!?]")


def _against_allergic(text: str) -> Iterator[tuple[int, str]]:
    """ "ich bin gegen Nuesse allergisch": where each ingredient between "gegen"
    and "allergisch" begins, and the ingredient. It holds no second "gegen",
    or the first phrase would swallow the second, and ends at ".;!?".

    The same matches, in the same order, as the pattern
    `gegen\\s+((?:(?!\\bgegen\\b)[^.;!?])+?)\\s+allergisch` (IGNORECASE),
    which read a long run of spaces again from every character of it (issue
    #252, CodeQL py/polynomial-redos); test_domain_menu_regex_cost.py compares
    the two. Here each run of spaces is read once: the ingredient may end at
    any space of a run that "allergisch" follows, and must end before the
    first stop after "gegen"."""
    # Runs of spaces that "allergisch" follows: start, end, end of "allergisch".
    runs = [
        (run.start(), run.end(), word.end())
        for run in _SPACES.finditer(text)
        if (word := _ALLERGISCH.match(text, run.end()))
    ]
    ends = [end for _, end, _ in runs]
    stops = sorted(
        [found.start() for found in _GEGEN_WORD.finditer(text)]
        + [found.start() for found in _SENTENCE_END.finditer(text)]
    )
    pos = 0
    while (gegen := _GEGEN.search(text, pos)) is not None:
        pos = gegen.start() + 1
        space = _SPACES.match(text, gegen.end())
        if space is None:
            continue
        first = space.end()
        at = bisect_left(stops, first)
        stop = stops[at] if at < len(stops) else len(text)
        # The ingredient takes at least one character, then ends at the first
        # space of a run that "allergisch" follows, at the latest at the stop.
        run = bisect_right(ends, first + 1)
        end = runs[run][0] if run < len(runs) else stop + 1
        if end <= stop:
            yield first, text[first:end]
            pos = runs[run][2]
        elif first - space.start() >= 3 and (word := _ALLERGISCH.match(text, first)):
            # "gegen   allergisch": the pattern backed off into the spaces and
            # took one of them as the ingredient (it reads as none).
            yield first - 2, text[first - 2 : first - 1]
            pos = word.end()


def _compound_stems(text: str) -> Iterator[tuple[int, str, bool]]:
    """The stems of compound allergy words, in order: where each begins, the
    stem, and whether it stands at the allergy noun itself ("Erdnussallergie",
    "Erdnuss-Allergie") or ends in a hyphen that a list carries on ("Nuss- und
    Sesamallergie"). A word is read once: from its beginning, then on from
    behind each allergy noun found in it."""
    pos = end = 0
    while True:
        if pos >= end:
            run = _WORD_RUN.search(text, pos)
            if run is None:
                return
            pos, end = run.span()
        noun = _ALLERGY_NOUN.search(text, pos + 1, end)
        if noun is not None:
            yield pos, text[pos : noun.start()], True
            pos = noun.end()
            continue
        if text.startswith("-", end):
            noun = _ALLERGY_NOUN.match(text, end + 1)
            if noun is not None:
                yield pos, text[pos:end], True
                pos = noun.end()
                continue
            after = end + 1
            while after < len(text) and text[after].isspace():
                after += 1
            if _LIST_WORD.match(text, after) is not None:
                yield pos, text[pos:end], False
        pos = end


# Kein Wortanfang einer Allergie: was davor steht, gehoert zum Satz.
_NOT_STEM = (
    _ARTICLES
    | _CLAUSE_WORDS
    | _REMOVE
    | _ADD
    | frozenset({"gegen", "auf", "habe", "hab", "hat", "und", "oder"})
)
_WORD_AT = re.compile(r"[^\W_]+|,")


def _stems_before(head: str) -> list[tuple[int, str]]:
    """Die Wortanfaenge vor "...allergie", die die Erkennung ohne Bindestrich
    schreibt: "Erdnuss und Sesamallergie", "Milch, Ei und Nussallergie" (Codex
    PR #139, P1). Rueckwaerts, je ein Wort hinter "und", "oder" oder Komma."""
    tokens = list(_WORD_AT.finditer(head))
    stems: list[tuple[int, str]] = []
    i = len(tokens) - 1
    while i >= 1 and fold(tokens[i].group()) in (",", "und", "oder", "sowie"):
        word = tokens[i - 1]
        folded = fold(word.group())
        if (
            folded in _NOT_STEM
            or _is_allergy(folded)
            or parse_cardinal(folded) is not None
        ):
            break
        stems.append((word.start(), word.group()))
        i -= 2
    return stems


# "und auch Sesam", "und noch Sesam": das Beiwort beendet die Liste nicht, wenn
# eine Zutat folgt (Codex PR #139, P1).
_LIST_MODIFIERS = frozenset({"auch", "noch", "zudem", "ausserdem", "zusaetzlich"})


def _ends_list(word: str | None) -> bool:
    """Beginnt mit diesem Wort nach "und" oder Komma ein neuer Satzteil?"""
    return (
        word is None
        or word in ",.;!?"
        or word in _CLAUSE_WORDS
        or word in ("gegen", "auf")
        or word in _AFFIRMATIONS
        or _is_allergy(word)
        or word.endswith("mal")
        or parse_cardinal(word) is not None
    )


def _until_new_clause(rest: str) -> str:
    """Die Zutaten bis zum naechsten Satzteil, mit "und" und Komma dazwischen."""
    # Read piece by piece up to the end of the clause, not the whole rest: a
    # long text with many allergy phrases stays linear (own review, #252).
    found = _PIECE.finditer(rest)
    pieces: list[str] = []

    def word_at(i: int) -> str | None:
        while len(pieces) <= i:
            piece = next(found, None)
            if piece is None:
                return None
            pieces.append(piece.group())
        return fold(pieces[i])

    kept: list[str] = []
    skip: set[int] = set()
    i = -1
    while (word := word_at(i := i + 1)) is not None:
        piece = pieces[i]
        if i in skip:
            continue
        # Eine weitere Allergie-Wendung zaehlt fuer sich, nicht als Zutat.
        if piece in ".;!?" or word == "bitte" or _is_allergy(word):
            break
        if piece == "," or word in ("und", "sowie", "oder"):
            nxt = word_at(i + 1)
            after = word_at(i + 2)
            if nxt in _LIST_MODIFIERS and not _ends_list(after):
                skip.add(i + 1)
                nxt = after
            if _ends_list(nxt):
                break
        kept.append(piece)
    text = ""
    for piece in kept:
        text += piece if piece == "," else f" {piece}"
    return text.strip()


def _adds(words: list[str], i: int) -> bool:
    """Beginnt hier eine Zugabe? "extra" direkt nach "ohne" gehoert zum
    Weglassen: "ohne extra Kaese" ist keine Zugabe (Codex PR #139)."""
    if words[i] not in _ADD:
        return False
    return not (words[i] == "extra" and i > 0 and words[i - 1] in _REMOVE)


def _starts(words: list[str]) -> list[int]:
    """Wo ein Wunsch beginnen kann, in Reihenfolge: an "ohne", "kein", "mit",
    "extra"; bei "statt" ein Wort davor; bei einer Allergie am Anfang ihres
    Satzteils - am Komma oder, ohne Komma, bei "ich", "wir", "mein" (Codex PR
    #139). Nach einer Allergie beginnt kein weiterer Wunsch."""
    starts: list[int] = []
    for i, word in enumerate(words):
        if word in _REMOVE or _adds(words, i):
            starts.append(i)
        elif word in _INSTEAD and i > 0 and words[i - 1] != ",":
            starts.append(i - 1)
        elif _mentions_allergy(words, i):
            starts.append(_clause_start(words, i))
            break
    return sorted(set(starts))


def _clause_start(words: list[str], i: int, nearest: bool = False) -> int:
    """Where the clause of the allergy word at `i` begins (`words` with
    commas): at "ich", "wir", "mein", "weil" after the last comma before it,
    else right after that comma, else where its phrase begins. `nearest`: at
    the last such word, not the first ("ohne Zwiebeln fuer meine Frau, ich
    habe ..." keeps "fuer meine Frau" with the removal)."""
    comma = max((j for j in range(i) if words[j] == ","), default=-1)
    openers = [j for j in range(comma + 1, i) if words[j] in _CLAUSE_OPENERS]
    if openers and not nearest:
        return openers[0]
    if openers:
        # Back over the words that open the clause with it: "weil ich".
        j = openers[-1]
        while j > comma + 1 and words[j - 1] in _PHRASE_LEAD | _CLAUSE_OPENERS:
            j -= 1
        return j
    return comma + 1 if comma >= 0 else _phrase_start(words, i)


# Polarity of an allergy (issue #252). A clause that only denies one ("ich
# habe keine Erdnussallergie") gives no allergy. A negation mixed with more
# ("keine Allergie gegen Erdnuesse, aber gegen Sesam") gives an allergy whose
# ingredient is asked for, never one read from the words: the denied one would
# go to the kitchen and the real one could be missing. Strict on purpose,
# anything the grammar below does not know keeps the allergy.
_KEIN = (_REMOVE - {"ohne"}) | {"keiner", "keinerlei"}
_PREPOSITIONS = frozenset({"gegen", "auf"})
# The verb behind a denial in a clause: "weil ich keine Allergie habe".
_DENIAL_VERBS = frozenset({"habe", "hab", "haben", "bin", "sind"})
# What may stand in front of a denial: "ich habe doch gar keine Allergie".
_DENIAL_LEAD = (
    _DENIAL_VERBS
    | {"ich", "wir", "auch", "doch", "gar", "ueberhaupt", "eigentlich", "wirklich"}
    | {"also", "aber", "und", "nein", "weil", "wegen", "denn"}
)
# Words of an allergy phrase in front of its allergy word, when no comma or
# "ich" marks where the phrase begins: "ohne Zwiebeln hab eine Erdnussallergie".
_PHRASE_LEAD = _ARTICLES | _KEIN | _DENIAL_LEAD | {"nicht", "hat", "ist", "noch"}
# Between a denial and the wish after it: "keine Allergie, danke. Ohne ...".
_AFTER_DENIAL = frozenset({",", "danke", "dankeschoen"})
# Words around a denial that may carry an allergen of their own.
_MAY_NAME = _PREPOSITIONS | {"nichts"}
# No ingredient in a denied list: "gegen Erdnuesse und Sesam", not "gegen
# Erdnuesse aber Sesam" or "gegen Erdnuesse, Sesam schon".
_NOT_ITEM = (
    _KEIN
    | _PREPOSITIONS
    | _REMOVE
    | _ADD
    | frozenset(
        {",", "nicht", "nichts", "und", "oder", "sowie", "aber", "sondern", "nur"}
    )
)


def _allergy_word(word: str) -> bool:
    """An allergy noun or adjective: "Erdnussallergie", "allergisch",
    "Allergiker", "laktoseintolerant"; not "vertrage", whose negation names
    the allergy ("ich vertrage keine Erdnuesse")."""
    return _is_allergy(word) and not word.startswith(("vertrag", "vertraeg"))


def _negated(words: list[str]) -> bool:
    """Does a negation touch an allergy anywhere in `words` (with commas):
    "keine Erdnussallergie", "nicht allergisch", "nicht gegen", "gegen
    nichts"?"""
    return any(
        ((word in _KEIN or word == "nicht") and _allergy_word(after))
        or (word == "nicht" and after in _PREPOSITIONS)
        or (word in _PREPOSITIONS and after == "nichts")
        for word, after in pairwise(words)
    )


def _list_end(words: list[str], i: int) -> int | None:
    """After the ingredients at `i`, one word each, joined by "und" or "oder";
    None if there is none."""

    def item(k: int) -> bool:
        return (
            k < len(words) and words[k] not in _NOT_ITEM and not _is_allergy(words[k])
        )

    if not item(i):
        return None
    i += 1
    while i + 1 < len(words) and words[i] in ("und", "oder") and item(i + 1):
        i += 2
    return i


def _denial_end(words: list[str], i: int) -> int | None:
    """Where the denial that begins at `i` ends: "ich habe keine
    Erdnussallergie", "keine Allergie gegen Erdnuesse", "nicht allergisch",
    "nicht gegen Erdnuesse allergisch", "gegen nichts allergisch". None: no
    such denial."""
    while i < len(words) and words[i] in _DENIAL_LEAD:
        i += 1
    word, after = [*words[i : i + 2], "", ""][:2]
    end: int | None = None
    if (word in _KEIN or word == "nicht") and _allergy_word(after):
        end = i + 2
        if end < len(words) and words[end] in _PREPOSITIONS:
            end = _list_end(words, end + 1)
    elif word == "nicht" and after in _PREPOSITIONS:
        end = _list_end(words, i + 2)
        if end is not None and end < len(words) and _allergy_word(words[end]):
            end += 1
        else:
            end = None
    elif (
        word in _PREPOSITIONS
        and after == "nichts"
        and i + 2 < len(words)
        and _allergy_word(words[i + 2])
    ):
        end = i + 3
    while end is not None and end < len(words) and words[end] in _DENIAL_VERBS:
        end += 1
    return end


def _denial(words: list[str], named: list[int], start: int) -> int | None:
    """Does the allergy phrase at `start` only deny an allergy? Then where a
    wish after it begins (`len(words)` without one); None when it does not."""
    end = _denial_end(words, start)
    if end is None or named[-1] >= end:
        return None
    after = _wish_after(words, end)
    # What is said around the denial must not name an allergen either: no
    # "gegen", "auf" or "nichts" in front ("gegen Sesam schon, aber keine
    # Allergie gegen Nuesse"), no such word and no "vertrage" behind it.
    if (
        after is None
        or _MAY_NAME & set(words[:start])
        or _MAY_NAME & set(words[after:])
    ):
        return None
    if any(_is_allergy(word) for word in words[after:]):
        return None
    return after


def denies_allergy(text: str) -> bool:
    """Does the guest only deny an allergy of their own ("ich habe keine
    Allergie", "nicht allergisch")? Wishes around it may still follow."""
    _, words = _tokens(_clean(text))
    named = _allergy_words(words)
    return bool(named) and (
        _denial(words, named, _clause_start(words, named[0], nearest=True)) is not None
    )


def _wish_after(words: list[str], i: int) -> int | None:
    """Where a wish begins after a denial that ends at `i`: "ich habe keine
    Allergie, aber bitte ohne Koriander", also after a full stop the words do
    not show. `len(words)` when nothing follows, None when something else
    does: it may name an allergy after all."""
    while i < len(words) and (words[i] in _AFTER_DENIAL or words[i] in _LEAD_FILLER):
        i += 1
    if i == len(words) or words[i] in _REMOVE or _adds(words, i):
        return i
    return None


def _phrase_start(words: list[str], i: int) -> int:
    """Where the allergy phrase of the word at `i` begins when no comma or
    "ich" marks it: back over "keine", "hab eine", "bin", and to the "gegen"
    of "gegen Nuesse allergisch"."""
    follows = words[i + 1] if i + 1 < len(words) else None
    if words[i].startswith("allergisch") and follows not in _PREPOSITIONS:
        # Only the "gegen" whose list of ingredients ends right here: not
        # the "auf" of "Ente auf Thai Art", not one beyond a removal.
        prep = next(
            (j for j in range(i - 1, -1, -1) if words[j] in _PREPOSITIONS), None
        )
        if prep is not None and _list_end(words, prep + 1) == i:
            i = prep
    while i > 0 and words[i - 1] in _PHRASE_LEAD:
        i -= 1
    return i


def _allergy_words(words: list[str]) -> list[int]:
    """Where the guest names an allergy of their own, as indices into `words`
    (with commas). Judged on the words alone, as `classify_wish` always did:
    "vertrage" looks past a comma for its negation."""
    at = [i for i, word in enumerate(words) if word != ","]
    plain = [words[i] for i in at]
    return [at[n] for n in range(len(plain)) if _mentions_allergy(plain, n)]


def wish_candidates(text: str) -> list[tuple[str, str, str]]:
    """Jede moegliche Trennung in Gericht, Wunsch und dessen ersten Satzteil, von
    vorn. Die Suche nimmt die erste, deren Satzteil nicht zum Namen gehoert:
    "Sommerrollen mit Garnelen ohne Koriander" - "mit Garnelen" ist Name,
    "ohne Koriander" der Wunsch (Codex PR #139). Eine Menge im Wunsch bleibt
    nicht darin ("ohne Zwiebeln, zweimal"), sie gehoert zur Position."""
    tokens, words = _tokens(text)
    starts = [i for i in _starts(words) if i > 0]
    candidates = []
    for n, start in enumerate(starts):
        at = tokens[start].start()
        dish = text[:at].strip(" ,.;")
        wish = _clean(text[at:])
        end = tokens[starts[n + 1]].start() if n + 1 < len(starts) else len(text)
        # "mit Salami, aber" - das Bindewort vor dem naechsten Wunsch gehoert
        # nicht zum Satzteil, sonst traefe es den Namen nie (Codex PR #139).
        segment = _drop_trailing_glue(_clean(text[at:end]))
        if dish and wish:
            candidates.append((dish, wish, segment))
    return candidates


def _is_separator(char: str) -> bool:
    return char == "," or char.isspace()


def _separators_start(text: str, end: int) -> int:
    """Where the run of whitespace and commas in front of `end` begins."""
    while end and _is_separator(text[end - 1]):
        end -= 1
    return end


def _cut_tail(pattern: re.Pattern[str], text: str) -> str:
    """Without what `pattern` finds at the end and the separators in front of it."""
    found = pattern.search(text)
    return text if found is None else text[: _separators_start(text, found.start())]


def _drop_trailing_glue(text: str) -> str:
    """Without the joining words at the end and the separators around them:
    "mit Salami, aber" -> "mit Salami". A word counts only behind a separator;
    read from the end, word by word."""
    end = _separators_start(text, len(text))
    cut = None
    while True:
        word = end
        while word and not _is_separator(text[word - 1]):
            word -= 1
        if word == 0 or _GLUE_WORD.fullmatch(text, word, end) is None:
            break
        cut = end = _separators_start(text, word)
    return text if cut is None else text[:cut]


def _clean(text: str) -> str:
    return _cut_tail(_PLEASE_AT_END, _drop_quantity(text)).strip(" ,.;!?")


def _is_quantity(word: str) -> bool:
    """ "ein" and number words in front of "mal": "einmal", "zweimal"."""
    return fold(word) == "ein" or parse_cardinal(fold(word)) is not None


def _without(
    pattern: re.Pattern[str], text: str, drop: Callable[[re.Match[str]], bool]
) -> str:
    """Without the matches `drop` says yes to, each with the separators in front."""
    kept: list[str] = []
    last = 0
    for found in pattern.finditer(text):
        if drop(found):
            kept.append(text[last : _separators_start(text, found.start())])
            last = found.end()
    kept.append(text[last:])
    return "".join(kept)


def _drop_quantity(text: str) -> str:
    """Nur echte Mengen: "zweimal", "einmal", "2 x" - nicht "normal"."""
    text = _without(_COUNTED, text, lambda found: True)
    return _without(_TIMES, text, lambda found: _is_quantity(found.group(1)))


def classify_wish(text: str, groups: list[OptionGroup]) -> Wish | None:
    """Der Wunsch zu einem feststehenden Gericht, gegen dessen Optionen. None:
    nothing to note, the guest denied an allergy ("ich habe keine Allergie")."""
    text = _clean(text)
    tokens, words = _tokens(text)
    named = _allergy_words(words)
    if named:
        return _with_allergy(text, tokens, words, named, groups)
    return _classify(text, groups)


def kitchen_note(removal: str | None, allergy: str) -> str:
    """The kitchen note for an allergy and the removal the guest said with it,
    the allergy first as the kitchen reads it (E14): "WICHTIG: Keine Erdnuss.
    Grund: Allergie. Ohne Zwiebeln". A note that ends with the removal already
    keeps it once. One too long for an order line keeps the allergy alone:
    the removal is the part to lose."""
    if not removal:
        return allergy
    removal = removal[0].upper() + removal[1:]
    if fold(allergy).endswith(". " + fold(removal)):
        return allergy
    note = f"{allergy}. {removal}"
    return note if len(note) <= NOTE_MAX else allergy


def removal_with(wish: Wish) -> str | None:
    """The removal kept next to an allergy, to repeat it to the guest:
    "Nummer 13 Pho Bo, ohne Zwiebeln"."""
    if wish.kind != "allergy" or wish.note:
        return wish.note
    if wish.ingredient is None:
        return None
    allergy = ALLERGY_NOTE.format(ingredient=wish.ingredient) + ". "
    if not wish.text.startswith(allergy):
        return None
    removal = wish.text[len(allergy) :]
    return removal[0].lower() + removal[1:]


def _with_allergy(
    text: str,
    tokens: list[re.Match[str]],
    words: list[str],
    named: list[int],
    groups: list[OptionGroup],
) -> Wish | None:
    """The guest's own allergy, and the wish said before it (issue #252).

    The phrase of the allergy runs to the end of the text; what comes before
    it is classified on its own, and only a removal from it stays next to an
    allergy: after the allergy on the kitchen note, or in `note` while the agent
    still asks what the allergy is against. An addition next to an allergy is
    not taken yet (docs/01_STATUS.md). The ingredient is read from the whole
    text, so a list that begins before the phrase keeps all of it ("Milch, Ei
    und Nussallergie"); a removal said right before it may be listed as an
    allergy too, the safe side.

    Polarity as at `_KEIN`: a phrase that only denies an allergy gives the
    wishes said around it ("ich habe keine Allergie, aber bitte ohne
    Koriander"), None without one; a negation in the phrase mixed with more
    gives an allergy without an ingredient."""
    start = _clause_start(words, named[0], nearest=True)
    head = _clean(_drop_trailing_glue(text[: tokens[start].start()]))
    after = _denial(words, named, start)
    if after is not None:
        tail = text[tokens[after].start() :] if after < len(tokens) else ""
        said = _clean(", ".join(part for part in (head, tail) if part))
        if not (_REMOVE | _ADD | _INSTEAD) & set(_words(said)):
            return None
        around = _classify(said, groups)
        # A wish the menu does not know may hide an intolerance ("aber mit
        # Gluten habe ich Probleme"): then it is not a plain denial.
        if around.kind in ("note", "option"):
            return around
    wish = _classify(head, groups) if head else None
    removal = None
    if wish is not None:
        removal = wish.text if wish.kind == "note" else wish.note
    # A negation in front of the phrase ("die Sosse nicht auf den Reis") may
    # name a denied ingredient: then only the phrase is read.
    ingredient = None
    if not _negated(words[start:]):
        ingredient = _ingredient(
            text[tokens[start].start() :] if _negated(words[:start]) else text
        )
    if ingredient is None:
        if removal is None:
            return Wish(text=text, kind="allergy")
        return Wish(text=text[tokens[start].start() :], kind="allergy", note=removal)
    return Wish(
        text=kitchen_note(removal, ALLERGY_NOTE.format(ingredient=ingredient)),
        kind="allergy",
        ingredient=ingredient,
    )


def _classify(text: str, groups: list[OptionGroup]) -> Wish:
    """A wish without an allergy in it, `text` already cleaned."""
    words = _words(text)
    lead = next((w for w in words if w not in _LEAD_FILLER), None)
    if lead in _REMOVE:
        left_out, added = _split_addition(text)
        if added is None:
            return Wish(text=text, kind="note")
        if not _REMOVE & set(_words(left_out)):
            # "ohne statt Reis": the word in front of "statt" is the removal
            # word itself, so nothing left out is named and the addition would
            # be the same text again (endless recursion). Not understood, and
            # never a free note.
            return Wish(text=text, kind="unknown")
        return _classify(_clean(added), groups).model_copy(update={"note": left_out})
    addition, removal = _split_removal(text)
    if removal is not None:
        # "mit Nudeln ohne Zwiebeln": die Option und der Hinweis, wie umgekehrt
        # (Codex PR #139). Nur eine sichere Option nimmt den Hinweis mit.
        wish = _classify(_clean(addition), groups)
        if wish.kind != "option" or wish.note:
            # Die Zugabe wird abgelehnt, das Weglassen bleibt - wie in "ohne
            # Zwiebeln, dafuer mit Pommes" (Review PR #139).
            return Wish(text=addition, kind="unknown", note=removal)
        return wish.model_copy(update={"text": text, "note": removal})
    # Bei "Nudeln statt Reis" gilt, was vor "statt" steht.
    cut = next((i for i, w in enumerate(words) if w in _INSTEAD), len(words))
    wanted = set(words[:cut])
    matches = [
        (group, option, used)
        for group in groups
        for option in group.options
        if (used := _names(option.name, group.group, wanted))
    ]
    if not matches:
        return Wish(text=text, kind="unknown")
    if len({option.name for _, option, _ in matches}) == 1 and len(matches) > 1:
        # Dieselbe Option in zwei Gruppen (Reis als Beilage und als Extra): nie
        # selbst waehlen, nachfragen (Review PR #139).
        return Wish(
            text=text,
            kind="open",
            option=matches[0][1].name,
            groups=[group.group for group, _, _ in matches],
        )
    if len(matches) != 1:
        return Wish(text=text, kind="unknown")
    group, option, used = matches[0]
    # Was die Option nicht erklaert, darf nicht still wegfallen: "mit Nudeln und
    # Pommes" ist kein "mit Nudeln" (Codex PR #139).
    if wanted - used - _ADD - _LEAD_FILLER - _GLUE:
        return Wish(text=text, kind="unknown")
    return Wish(
        text=text,
        kind="option",
        group=group.group,
        option=option.name,
        price_delta_cents=option.price_delta_cents,
        reason=option.reason,
    )


def _split_addition(text: str) -> tuple[str, str | None]:
    """ "ohne Zwiebeln, dafür mit Nudeln" -> ("ohne Zwiebeln", "mit Nudeln")."""
    tokens, words = _tokens(text)
    at = next(
        (
            i
            for i, w in enumerate(words)
            if i > 0 and (_adds(words, i) or w in _INSTEAD)
        ),
        None,
    )
    if at is None:
        return text, None
    if words[at] in _INSTEAD:
        at -= 1
    removal = text[: tokens[at].start()]
    removal = _cut_tail(_JOINER_AT_END, removal)
    return removal.strip(" ,.;"), text[tokens[at].start() :].strip(" ,.;")


def _split_removal(text: str) -> tuple[str, str | None]:
    """ "mit Nudeln, aber ohne Zwiebeln" -> ("mit Nudeln", "ohne Zwiebeln")."""
    tokens, words = _tokens(text)
    at = next((i for i, w in enumerate(words) if i > 0 and w in _REMOVE), None)
    if at is None:
        return text, None
    addition = text[: tokens[at].start()]
    addition = _cut_tail(_JOINER_AT_END, addition)
    return addition.strip(" ,.;"), text[tokens[at].start() :].strip(" ,.;")


def _names(option: str, group: str, wanted: set[str]) -> set[str]:
    """Die Woerter des Wunsches, die die Option nennen - leer, wenn sie es nicht
    tun. Als eigenes Wort ("mit Erdnuss") oder zusammengesetzt mit dem
    Gruppennamen ("Erdnusssauce" = Erdnuss + Sauce). Kein beliebiger
    Wortanfang: "Reisnudeln" ist nicht die Option Reis."""
    words = _words(option)
    if set(words) <= wanted:
        return set(words)
    compound = words[0] + "".join(_words(group)) if len(words) == 1 else None
    return {compound} if compound in wanted else set()


def open_wish(text: str) -> Wish | None:
    """Bei mehreren Treffern: Weglassen und Allergie stehen schon fest, eine
    Option erst, wenn der Gast das Gericht gewaehlt hat. None: a denied
    allergy and nothing else, no wish."""
    wish = classify_wish(text, [])
    if wish is None or wish.kind in ("note", "allergy"):
        return wish
    return Wish(text=_clean(text), kind="open")


# Womit ein Satzteil zur eigenen Allergie anfangen kann, bevor das Allergie-Wort
# kommt: "und einer Sesamallergie", "ich bin gegen Sesam allergisch".
_ALLERGY_LEAD = (
    _ARTICLES
    | _CLAUSE_OPENERS
    | frozenset({"und", "auch", "noch", "habe", "hab", "bin"})
)


def opens_with_allergy(text: str) -> bool:
    """Beginnt der Teil mit einer eigenen Allergie statt mit einem Gericht?
    Dann gehoert er zur Position davor (Codex PR #139, P1)."""
    for word in _words(text):
        if word in _ALLERGY_LEAD:
            continue
        return _is_allergy(word) or word == "gegen"
    return False


def has_number(wish: str) -> bool:
    """Steht eine Zahl im Wunsch ("mit 2 Soßen")? Dann gilt Regel A: eine zweite
    Zahl neben der Nummer wird nachgefragt, nie als Wunsch abgetrennt. Artikel
    ("ohne eine Zwiebel") zaehlen nicht."""
    return any(
        w.isdigit() or (w not in _ARTICLES and parse_cardinal(w) is not None)
        for w in _words(wish)
    )


def names_it(name: str, wish: str) -> bool:
    """Gehoert der "Wunsch" zum Namen des Gerichts? "mit Garnelen" in
    "Sommerrollen mit Garnelen" ist kein Wunsch, sondern der Name - auch mit
    Satzzeichen im Namen ("Sommerrollen (mit Garnelen)", Review PR #139)."""
    words = _words(wish)
    # "ohne Garnelen" gehoert nie zum Namen, auch wenn die Garnelen darin stehen:
    # es ist genau der Hinweis fuer die Kueche (Codex PR #139).
    lead = next((w for w in words if w not in _LEAD_FILLER), None)
    if lead in _REMOVE:
        return False
    # Der Satzteil steht so im Namen, samt "mit": "extra Garnelen" nennt die
    # Garnelen aus "Sommerrollen mit Garnelen", ist aber ein Wunsch (Codex PR
    # #139).
    said = [w for w in words if w not in _LEAD_FILLER]
    named = _words(name)
    return bool(said) and any(
        named[i : i + len(said)] == said for i in range(len(named) - len(said) + 1)
    )
