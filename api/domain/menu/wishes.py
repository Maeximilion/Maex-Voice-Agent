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
from typing import Literal

from api.domain.menu.numberwords import (
    CardFormat,
    canonical_card,
    fold,
    parse_cardinal,
    sole_item_number,
)
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
        # "Sesam vertrage ich auch nicht": the negation behind the subject and
        # its adverbs (#361). Its ingredient stands before the verb and is
        # not read, so the guest is asked "Wogegen?" (`_with_allergy`).
        k = i + 1
        if k < len(words) and words[k] in _PRONOUNS:
            k = _skip(words, k + 1, _ADVERBS | {"leider"})
        return any(
            w in _REMOVE or w in ("nicht", "weder")
            for w in [*words[i + 1 : i + 3], *words[k : k + 1]]
        )
    return _is_allergy(word)


def _ingredient(text: str) -> str | None:
    """Die Zutaten, wie der Gast sie sagt: "Erdnussallergie" -> "Erdnuss",
    "allergisch gegen Sesam" -> "Sesam", mehrere in Reihenfolge ("Milch und
    Nuss"). None: nicht erkennbar, nachfragen."""
    text = _without_second_preposition(text)
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
    if not pieces or _left_unread(text, pieces):
        return None
    if len(pieces) == 1:
        return pieces[0]
    return ", ".join(pieces[:-1]) + " und " + pieces[-1]


def _left_unread(text: str, pieces: list[str]) -> bool:
    """Would the note leave out part of what the guest named (issue #207,
    PR #361)? Then nothing is noted and the agent asks "Wogegen?"; the
    words the readers above did not take are never read here.

    - An allergy word that names an allergen no reader takes:
      "laktoseintolerant", "Erdnussallergiker".
    - A "gegen" with one word behind the first allergy word that no piece
      holds: "und meine Frau gegen Sesam", "Erdnussallergie. Und gegen
      Sesam".
    - "aber auch" with an item no piece holds: "allergisch gegen Erdnuesse,
      aber auch gegen Sesam".
    - More behind an allergy word that closes its phrase, in its sentence:
      "gegen Erdnuesse allergisch und gegen Sesam", "Erdnussallergie,
      ausserdem gegen Sesam", "Erdnussallergie und Sesam",
      "Erdnussallergie, leider". A bare word counts only behind a word that
      names an allergen: "mit Allergie und Fruehlingsrollen" names a dish."""
    tokens, words = _tokens(text)
    if any(
        _allergy_word(word)
        and not _generic(word)
        and _ALLERGY_NOUN.search(word, 1) is None
        for word in words
    ):
        return True
    first = next((i for i, word in enumerate(words) if _is_allergy(word)), len(words))
    read = {word for piece in pieces for word in _words(piece)}

    def unread(k: int) -> bool:
        return (
            k < len(words)
            and words[k] not in read
            and words[k] not in _GOES_ON
            and not _ends_list(words[k])
        )

    def in_sentence(at: int, k: int) -> bool:
        return _SENTENCE_END.search(text, tokens[at].end(), tokens[k].start()) is None

    for i in range(first + 1, len(words) - 1):
        if (
            words[i] == "gegen"
            and unread(i + 1)
            and _item_ends(text, tokens, words, i + 1)
        ):
            return True
        if words[i] == "aber" and words[i + 1] in _LIST_MODIFIERS:
            k = _skip(words, i + 1, _LIST_MODIFIERS)
            k += k < len(words) and words[k] in _PREPOSITIONS
            if unread(k) and in_sentence(i, k):
                return True
    last = -1
    for n, word in enumerate(words):
        if not _allergy_word(word):
            continue
        k = n + 1
        comma = k < len(words) and words[k] == ","
        k += comma
        joined = k < len(words) and words[k] in ("und", "sowie", "oder")
        k += joined
        k = _skip(words, k, _LIST_MODIFIERS)
        prep = k < len(words) and words[k] in _PREPOSITIONS
        k += prep
        # Each "gegen" belongs to the next allergy word after it: read back
        # only to the one before, so a long text is not read again for each.
        lo, last = last, n
        if not (joined or comma) or not unread(k) or not in_sentence(n, k):
            continue
        names = (
            not _generic(word)
            # "Erdnuss-Allergie"
            or (n > 0 and text[tokens[n - 1].end() : tokens[n].start()] == "-")
            or _prep_before(words, n, lo) is not None
        )
        if prep or names:
            return True
    return False


# What follows a "gegen" or "auf" that carries on no list of ingredients: a
# time ("und gegen halb sieben", "gegen Abend"), "auf jeden Fall", "auf
# Wiedersehen".
_NOT_LISTED = frozenset(
    {"halb", "viertel", "abend", "mittag", "jeden", "keinen", "alle"}
    | {"wiedersehen", "wiederhoeren"}
)
_LIST_JOINERS = frozenset({",", "und", "sowie", "oder"})


def _without_second_preposition(text: str) -> str:
    """ "gegen Erdnuesse und gegen Sesam" -> "gegen Erdnuesse und Sesam": a
    "gegen" or "auf" that carries on the list of the one before it, so its
    ingredient is read with the others; before, only the first reached the
    kitchen (review of PR #330). Only behind a list that the rules of
    `_until_new_clause` would read on, never before a time ("und gegen 18
    Uhr"). Also "gegen Erdnuesse gegen Sesam", where the ear wrote no comma.
    The second one names one word, and the list ends or goes on right after
    it: "und auf dem Weg zu euch" or "und auf Antibiotika angewiesen" is a
    clause of its own, not an allergen (Codex PR #338)."""
    tokens, words = _tokens(text)
    kept: list[str] = []
    last = 0
    for i in range(1, len(words) - 1):
        follows = words[i + 1]
        if (
            words[i] not in _PREPOSITIONS
            or follows in _NOT_LISTED
            or follows in _NO_INGREDIENT
            or follows in _NEGATION
            or _ends_list(follows)
            or not _item_ends(text, tokens, words, i + 1)
        ):
            continue
        j = i - 1
        while j > 0 and words[j] in _LIST_MODIFIERS:
            j -= 1
        first = next(
            (p for p in range(j - 1, -1, -1) if words[p] in _PREPOSITIONS), None
        )
        listed = [] if first is None else words[first + 1 : j]
        # Not across a full stop: "gegen Erdnuesse. Gegen Sesam bin ich
        # allergisch" is two phrases, each read on its own. And only the
        # same preposition again, as a list repeats it: "gegen Erdnuesse und
        # auf Diaet" is a clause of its own (Codex PR #338).
        if (
            first is None
            or words[first] != words[i]
            or _SENTENCE_END.search(text, tokens[first].end(), tokens[i].start())
        ):
            continue
        joiner = ""
        if words[j] not in _LIST_JOINERS:
            # No joiner at all: only one word between the two, as said.
            if j != i - 1 or first != j - 1 or _ends_list(words[j]):
                continue
            joiner = "und "
        elif not listed or not all(
            w in _LIST_JOINERS or not _ends_list(w) for w in listed
        ):
            continue
        kept.append(text[last : tokens[i].start()] + joiner)
        last = tokens[i + 1].start()
    kept.append(text[last:])
    return "".join(kept)


def _item_ends(
    text: str, tokens: list[re.Match[str]], words: list[str], k: int
) -> bool:
    """Does the list item of one word at `k` end there: at the end, before a
    joiner, a full stop, a word that ends a list, or "danke"?"""
    k += 1
    return (
        k >= len(words)
        or words[k] in _LIST_JOINERS
        or words[k] in _AFTER_DENIAL
        or _ends_list(words[k])
        or _SENTENCE_END.search(text, tokens[k - 1].end(), tokens[k].start())
        is not None
    )


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
        # An addition is no ingredient: "allergisch gegen Erdnuesse, mit
        # Nudeln" must not take the noodles into the note unpriced (#252).
        or word in _ADD
        # A verb begins a clause of its own: "gegen Erdnuesse und habe eine
        # Sesamallergie" keeps no "und" in the note (Codex PR #330).
        or word in _DENIAL_VERBS
        or word in _AFFIRMATIONS
        or _is_allergy(word)
        or word.endswith("mal")
        or parse_cardinal(word) is not None
    )


def _until_new_clause(rest: str) -> str:
    """Die Zutaten bis zum naechsten Satzteil, mit "und" und Komma dazwischen."""
    # Read piece by piece up to the end of the clause, not the whole rest:
    # a long text with many allergy phrases is not tokenised again for each
    # of them (own review, #252).
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
        # Nor is the verb at the end of a clause ("weil ich allergisch gegen
        # Erdnuesse bin", #252).
        if (
            piece in ".;!?"
            or word == "bitte"
            or _is_allergy(word)
            or word in _DENIAL_VERBS
        ):
            break
        if piece == "," or word in ("und", "sowie", "oder"):
            nxt = word_at(i + 1)
            after = word_at(i + 2)
            if nxt in _LIST_MODIFIERS and not _ends_list(after):
                skip.add(i + 1)
                nxt = after
            if _ends_list(nxt):
                break
            # "und moechte bestellen" goes on with the order; "und moechte
            # (auch) keine Zwiebeln" stays in, so the kitchen still reads it.
            at = i + 3 if i + 1 in skip else i + 2
            follows = [word_at(k) for k in range(at, at + 3)]
            if nxt in _ORDER_VERBS and not any(
                w in _REMOVE or w in _NEGATION for w in follows
            ):
                break
        kept.append(piece)
    text = ""
    for piece in kept:
        text += piece if piece == "," else f" {piece}"
    # No comma left at the end: "gegen Erdnuesse, und moechte ...".
    return text.strip().rstrip(",")


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
    or at the "gegen" before it in "gegen Erdnuesse bin ich allergisch", else
    right after that comma, else where its phrase begins. `nearest`: at the
    last such word, not the first ("ohne Zwiebeln fuer meine Frau, ich habe
    ..." keeps "fuer meine Frau" with the removal)."""
    comma = max((j for j in range(i) if words[j] == ","), default=-1)
    openers = [j for j in range(comma + 1, i) if words[j] in _CLAUSE_OPENERS]
    if not openers:
        return comma + 1 if comma >= 0 else _phrase_start(words, i)
    j = openers[-1] if nearest else openers[0]
    if nearest:
        # Back over the words that open the clause with it: "weil ich".
        while j > comma + 1 and words[j - 1] in _OPENING:
            j -= 1
    # "gegen Erdnuesse bin ich (nicht) allergisch": the clause begins at the
    # "gegen" (Codex PR #330).
    prep = _prep_before(words, i, comma)
    return prep if prep is not None and prep < j else j


# Polarity of an allergy (issue #252). A clause that only denies one ("ich
# habe keine Erdnussallergie") gives no allergy. A negation mixed with more
# ("keine Allergie gegen Erdnuesse, aber gegen Sesam") gives an allergy whose
# ingredient is asked for, never one read from the words: the denied one would
# go to the kitchen and the real one could be missing. Strict on purpose,
# anything the grammar below does not know keeps the allergy.
_KEIN = (_REMOVE - {"ohne"}) | {"keiner", "keinerlei"}
# Every word that may deny an allergy word right behind it.
_NEGATION = _KEIN | {"nicht", "weder"}
# Words between a negation and the allergy noun that change nothing about the
# denial: "keine bekannten Allergien" (Codex PR #330).
# "echte" or "richtige" are none: "keine richtige Allergie gegen Nuesse" is
# a reaction to nuts.
_MODIFIERS = frozenset(
    {"bekannte", "bekannten", "bekannter", "bekanntes", "weitere", "weiteren"}
    | {"sonstige", "sonstigen", "andere", "anderen"}
)
_PREPOSITIONS = frozenset({"gegen", "auf"})
# The verb behind a denial in a clause: "weil ich keine Allergie habe".
_DENIAL_VERBS = frozenset({"habe", "hab", "haben", "bin", "sind"})
# Verbs that go on with the order after an allergy, without "ich": "und
# moechte bestellen", "und haette gern die 13" (Codex PR #338). No allergen is
# called so. Not "kann" or "mag": "und kann keine Milch trinken" names one.
_ORDER_VERBS = frozenset(
    {"moechte", "moechten", "haette", "haetten", "will", "wollen", "wuerde"}
    | {"wuerden", "nehme", "nehmen", "bestelle", "bestellen", "hole", "holen"}
    | {"komme", "kommen"}
)
# "Keine Allergien sind mir bekannt"
_DENIAL_TRAIL = _DENIAL_VERBS | {"ich", "wir", "mir", "uns", "bekannt", "ist"}
# Who has the allergy, in "habe ich nicht".
_PRONOUNS = frozenset({"ich", "wir"})
# Between "habe ich" and its "nicht": "habe ich gar nicht".
_ADVERBS = frozenset({"gar", "auch", "eigentlich", "ueberhaupt", "wirklich", "doch"})
# What may stand in front of a denial or an allergy phrase: "ich habe doch gar
# keine Allergie".
_LEAD_WORDS = _DENIAL_VERBS | (
    {"ich", "wir", "auch", "doch", "gar", "ueberhaupt", "eigentlich", "wirklich"}
    | {"also", "aber", "und", "nein", "weil", "wegen", "denn"}
)
# Before a denial only: "Mir ist keine Allergie bekannt". Not before an
# allergy phrase, where "fuer uns" may end a removal.
_DENIAL_LEAD = _LEAD_WORDS | {"mir", "uns", "ist"}
# Words of an allergy phrase in front of its allergy word, when no comma or
# "ich" marks where the phrase begins: "ohne Zwiebeln hab eine Erdnussallergie".
_PHRASE_LEAD = _ARTICLES | _NEGATION | _LEAD_WORDS | {"hat", "ist", "noch"}
# The words that begin a wish.
_WISHED = _REMOVE | _ADD | _INSTEAD
# What may stand around a denial without saying anything of its own: "Nein,
# ich habe keine Allergie", "Doch, keine Allergie". Anything else may name an
# allergen ("Erdnuesse, eine Sesamallergie habe ich nicht"), so the denial is
# not taken.
_FILLERS = (
    _LEAD_WORDS
    | _LEAD_FILLER
    | _ADVERBS
    | {",", "nein", "ja", "naja", "hm", "aeh", "oh", "gut", "okay", "ok", "danke"}
    | {"mir", "uns", "ist"}
)
# What opens an allergy clause together with "ich": "weil ich", "und ich".
_OPENING = _PHRASE_LEAD | _CLAUSE_OPENERS
# Between a denial and the wish after it: "keine Allergie, danke. Ohne ...".
_AFTER_DENIAL = frozenset(
    {",", "danke", "dankeschoen", "nur", "das", "wars", "ist", "alles", "sonst"}
    | {"nichts", "oder", "so"}
)
# What goes on with the call behind an allergy word, not with its list:
# "und gegen 18 Uhr", "und moechte bestellen", ", danke", ", weitere
# Allergien habe ich keine", "und meine Frau" (`_left_unread`).
_GOES_ON = (
    _CLAUSE_OPENERS
    | _NOT_LISTED
    | _NO_INGREDIENT
    | _NEGATION
    | _REMOVE
    | _ORDER_VERBS
    | _AFTER_DENIAL
    | _MODIFIERS
)
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
    nichts"? Only the closing "sonst keine (weiteren) Allergien" after a named
    allergy does not count: it denies no ingredient and none follows it."""
    for n, word in enumerate(words[:-1]):
        if word in _NEGATION:
            at = _noun_after(words, n + 1, word)
            denies = at < len(words) and _allergy_word(words[at])
            if denies and not _closing(words, n, at):
                return True
        if _allergy_word(word):
            behind = _negation_behind(words, _after_object(words, n + 1))
            closing = _generic(word) and n and words[n - 1] in _MODIFIERS | {"sonst"}
            if behind is not None and not closing:
                return True
        if word in ("nicht", "weder") and words[n + 1] in _PREPOSITIONS:
            return True
        if word in _PREPOSITIONS and (
            words[n + 1] in ("nichts", "gar") or words[n + 1] in _KEIN
        ):
            return True
    return False


def _skip(words: list[str], i: int, allowed: frozenset[str]) -> int:
    while i < len(words) and words[i] in allowed:
        i += 1
    return i


def _negation_behind(words: list[str], i: int) -> int | None:
    """Behind the negation that follows a noun or a list at `i` with its verb:
    "(eine Erdnussallergie) habe ich (gar) nicht", "(Allergien) habe ich
    keine" (Codex PR #330). None when there is none, also for "habe ich nicht
    nur" and for a removal "habe ich keine Erdnuesse"."""
    if not (
        i + 1 < len(words)
        and words[i] in _DENIAL_VERBS
        and words[i + 1] in _PRONOUNS | {"mir", "uns"}
    ):
        return None
    k = _skip(words, i + 2, _ADVERBS)
    if k >= len(words) or words[k] not in _NEGATION:
        return None
    follows = words[k + 1] if k + 1 < len(words) else ""
    if follows == "nur":
        return None
    if words[k] in _KEIN and follows and follows not in _AFTER_DENIAL | _DENIAL_TRAIL:
        return None
    return k + 1


def _prep_before(words: list[str], i: int, lo: int) -> int | None:
    """The "gegen" after `lo` whose ingredients run up to the "allergisch" at
    `i`, also across its verb: "gegen Nuesse allergisch", "gegen Nuesse bin ich
    (nicht) allergisch". Not the "auf" of "Ente auf Thai Art", whose
    "allergisch" has its own "gegen" behind it."""
    if not words[i].startswith("allergisch"):
        return None
    if i + 1 < len(words) and words[i + 1] in _PREPOSITIONS:
        return None
    prep = next((k for k in range(i - 1, lo, -1) if words[k] in _PREPOSITIONS), None)
    listed = None if prep is None else _object_end(words, prep + 1)
    if listed is None:
        return None
    listed = _skip(words, listed, _DENIAL_VERBS | _PRONOUNS | _ADVERBS | {"nicht"})
    return prep if listed == i else None


def _closing(words: list[str], n: int, at: int) -> bool:
    """Is the negation at `n` of the generic word at `at` the closing "sonst
    keine (weiteren) Allergien (bekannt, danke)"? It denies no ingredient and
    none follows it."""
    back = n
    while back > 0 and words[back - 1] in _DENIAL_LEAD:
        back -= 1
    if back == 0 or words[back - 1] not in ("sonst", "ansonsten"):
        return False
    end = _denial_end(words, back)
    return (
        end is not None
        and all(_generic(w) for w in words[back:end] if _allergy_word(w))
        and all(w in _DENIAL_TRAIL or w in _AFTER_DENIAL for w in words[end:])
    )


def _object_end(words: list[str], i: int) -> int | None:
    """After what follows "gegen" or "auf" at `i`: "nichts", "keine Nuesse",
    or a list of ingredients; None if none of them."""
    if i + 1 < len(words) and words[i] == "gar" and words[i + 1] == "nichts":
        return i + 2
    if i < len(words) and words[i] == "nichts":
        return i + 1
    if i < len(words) and words[i] in _KEIN:
        return _list_end(words, i + 1)
    return _list_end(words, i)


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
    "nicht gegen Erdnuesse allergisch", "gegen nichts allergisch", "gegen
    keine Nuesse allergisch", and after the noun: "eine Erdnussallergie habe
    ich nicht", "gegen Erdnuesse bin ich nicht allergisch". None: no such
    denial."""
    while i < len(words) and words[i] in _DENIAL_LEAD:
        i += 1
    word, after = [*words[i : i + 2], "", ""][:2]
    end: int | None = None
    noun = _noun_after(words, i + 1, word)
    if word in _NEGATION and noun < len(words) and _allergy_word(words[noun]):
        end = noun + 1
        # The last allergy word was reached over a bare "und" by a negation
        # that cannot govern it: "keine" governs a second noun ("keine
        # Allergien und Unvertraeglichkeiten gegen Laktose" denies both),
        # never an adjective ("keine Allergie und allergisch auf
        # Erdnuesse"), and "nicht" no noun ("nicht allergisch und
        # Unvertraeglichkeit gegen Laktose"). Only the last counts: "... und
        # keine Intoleranz" negates its own (Codex PR #338).
        bare_und = False
        governor = word
        # The denial goes on only where the next allergy word is denied too:
        # "oder keine Unvertraeglichkeiten", "weder (eine) Allergie noch
        # (eine) Unvertraeglichkeit" (Codex PR #330), or a bare generic word
        # ("keine Allergien und Unvertraeglichkeiten"). Never "und eine
        # Sesamallergie", "oder eine Allergie gegen Sesam", "und weitere
        # Allergien": those name an allergy, and it is asked about.
        while end < len(words) and words[end] in ("und", "oder", "noch"):
            joiner, nxt = words[end], end + 1
            bare = False
            if nxt < len(words) and words[nxt] in _KEIN:
                joiner, nxt = words[nxt], nxt + 1
                nxt = _noun_after(words, nxt, joiner)
                governor = joiner
            elif joiner == "noch":
                nxt = _noun_after(words, nxt, joiner)
            else:
                nxt = _noun_after(words, nxt, joiner)
                # "keine Nussallergie und Allergien gegen Sesam" names sesame.
                if not (
                    nxt < len(words) and _generic(words[nxt]) and _generic(words[noun])
                ):
                    break
                bare = joiner == "und" and not (
                    governor in _KEIN and _allergy_noun(words[nxt])
                )
            if nxt >= len(words) or not _allergy_word(words[nxt]):
                break
            end, bare_und = nxt + 1, bare
        if end < len(words) and words[end] in _PREPOSITIONS:
            if bare_und:
                # "keine Allergie und allergisch gegen Erdnuesse": an
                # ingredient behind a word the negation may not reach is no
                # denial; it is asked about (review of PR #330).
                return None
            end = _list_end(words, end + 1)
    elif word in ("nicht", "weder") and after in _PREPOSITIONS:
        # "nicht gegen Erdnuesse allergisch", "weder gegen Erdnuesse noch
        # gegen Soja allergisch"
        end = _list_end(words, i + 2)
        while (
            end is not None
            and end + 1 < len(words)
            and words[end] == "noch"
            and words[end + 1] in _PREPOSITIONS
        ):
            end = _list_end(words, end + 2)
        if end is not None and end < len(words) and _allergy_word(words[end]):
            end += 1
        else:
            end = None
    elif word in _PREPOSITIONS:
        # "gegen (gar) nichts (bin ich) allergisch", "gegen keine Nuesse
        # allergisch", "gegen Erdnuesse bin ich nicht allergisch" (Codex PR
        # #330)
        listed = _object_end(words, i + 1)
        k = (
            None
            if listed is None
            else _skip(words, listed, _DENIAL_VERBS | _PRONOUNS | _ADVERBS)
        )
        denied = after in ("nichts", "gar") or after in _KEIN
        if k is not None and not denied and k < len(words) and words[k] == "nicht":
            k += 1
            denied = True
        if denied and k is not None and k < len(words) and _allergy_word(words[k]):
            end = k + 1
    else:
        # "eine Erdnussallergie habe ich nicht", "Allergien habe ich keine",
        # "eine Allergie gegen Erdnuesse habe ich nicht" (Codex PR #330)
        noun = i + 1 if word in _ARTICLES else i
        k = noun + 1
        if noun >= len(words) or not _allergy_word(words[noun]):
            pass
        elif (
            k + 1 < len(words)
            and words[k] in _PREPOSITIONS
            and words[k + 1] in ("nichts", "gar")
        ):
            # "allergisch gegen (gar) nichts" (Codex PR #330); not "Allergie
            # gegen keine ...", which may be "keine Ahnung"
            end = _object_end(words, k + 1)
        else:
            end = _negation_behind(words, _after_object(words, k))
    # "Keine Allergie habe ich"
    while end is not None and end < len(words) and words[end] in _DENIAL_TRAIL:
        end += 1
    return end


def _after_object(words: list[str], i: int) -> int:
    """Past "gegen Erdnuesse" at `i`, if it stands there."""
    if i < len(words) and words[i] in _PREPOSITIONS:
        listed = _list_end(words, i + 1)
        return i if listed is None else listed
    return i


def _noun_after(words: list[str], i: int, joiner: str) -> int:
    """Where the allergy word behind `joiner` ("keine", "weder", "noch")
    would stand, from `i`: past an article after "weder" or "noch" ("weder
    eine Allergie"), and past the modifiers ("keine bekannten Allergien")."""
    if joiner in ("weder", "noch") and i < len(words) and words[i] in _ARTICLES:
        i += 1
    while i < len(words) and words[i] in _MODIFIERS:
        i += 1
    return i


# What stands before an allergy word that names no ingredient.
_GENERIC_PREFIXES = _GENERIC_STEMS | {"", "hoch", "schwer", "stark", "sehr", "extrem"}


def _allergy_noun(word: str) -> bool:
    """An allergy noun ("Allergien", "Unvertraeglichkeit", "Intoleranz",
    "Allergiker"), not an adjective ("allergisch", "intolerant")."""
    return word.endswith(
        ("allergie", "allergien", "allergiker", "keit", "keiten", "intoleranz")
    ) or word.endswith("intoleranzen")


def _generic(word: str) -> bool:
    """An allergy word that names no ingredient: "Allergien", "allergisch",
    "Unvertraeglichkeit", "Lebensmittelallergie"; not "Erdnussallergie" or
    "laktoseintolerant". A word without one of the keys counts as naming one,
    the safe side."""
    found = [
        word.find(key)
        for key in ("allerg", "intoleran", "unvertraeglich")
        if key in word
    ]
    return bool(found) and word[: min(found)] in _GENERIC_PREFIXES


def _denial(words: list[str], named: list[int], start: int) -> int | None:
    """Does the allergy phrase at `start` only deny an allergy? Then where a
    wish after it begins (`len(words)` without one); None when it does not."""
    end = _denial_end(words, start)
    if end is None or named[-1] >= end:
        return None
    after = _wish_after(words, end)
    # What is said around the denial must not name an allergen either: no
    # "gegen", "auf" or "nichts" around it ("gegen Sesam schon, aber keine
    # Allergie gegen Nuesse"; "sonst nichts" is fine), and no "vertrage" too
    # far from its "nicht" to be an allergy word ("Laktose vertrage ich
    # ueberhaupt nicht, ich bin aber nicht allergisch").
    if (
        after is None
        or _MAY_NAME & set(words[:start])
        or _MAY_NAME & _but_sonst_nichts(words[after:])
    ):
        return None
    if any(_is_allergy(word) for word in words[:start] + words[after:]):
        return None
    head = words[:start]
    if head and not _WISHED & set(head) and not set(head) <= _FILLERS:
        return None
    return after


def _but_sonst_nichts(words: list[str]) -> set[str]:
    """The words, without the "nichts" of "sonst nichts"."""
    return {
        word
        for n, word in enumerate(words)
        if not (word == "nichts" and n and words[n - 1] == "sonst")
    }


def mentions_allergy(text: str) -> bool:
    """Does the guest speak of an allergy of their own, in the words
    `classify_wish` reads ("Erdnussallergie", "laktoseintolerant", "ich
    vertrage keine Erdnuesse")? Not "Allergene" or "allergiefrei", the
    question about a dish."""
    return bool(_allergy_words(_tokens(_clean(text))[1]))


def allergies_named(text: str) -> int:
    """How many allergy words the guest says ("Erdnussallergie, meine Frau
    eine Sesamallergie": two; "Nuss- und Sesamallergie": one)."""
    return len(_allergy_words(_tokens(_clean(text))[1]))


def denies_allergy(text: str) -> bool:
    """Does the guest only deny an allergy of their own ("ich habe keine
    Allergie", "nicht allergisch")? As `classify_wish` decides without the
    dish's options: the allergy words give no allergy."""
    if not mentions_allergy(text):
        return False
    wish = classify_wish(text, [])
    return wish is None or wish.kind != "allergy"


def _denial_phrase(text: str) -> bool:
    """Is the allergy phrase of `text` a denial, whatever is said around it?"""
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
    prep = _prep_before(words, i, -1)
    if prep is not None:
        i = prep
    # "keine bekannten Allergien", "weder eine bekannte": the modifiers only
    # after a negation, since "Bekannten" may also be a person ("fuer
    # meinen Bekannten").
    j = i
    while j > 0 and words[j - 1] in _MODIFIERS:
        j -= 1
    k = j - 1 if j > 0 and words[j - 1] in _ARTICLES else j
    if j < i and k > 0 and words[k - 1] in _NEGATION:
        i = j
    while i > 0 and words[i - 1] in _PHRASE_LEAD:
        i -= 1
    return i


# Words that join a removal to the allergy before it: ", aber ohne", "also ohne",
# "aber auch keine" (#331).
_CONNECTORS = frozenset(
    {",", "aber", "und", "also", "deshalb", "daher", "dann", "bitte", "dafuer", "auch"}
)
# What joins a removal to what follows it.
_JOINERS = frozenset({",", "und", "oder", "sowie", "aber"})
# What begins after a removal: a new dish ("und die Sommerrollen", "und zwei
# Pho") or the next allergy ("und eine Sesamallergie", "und gegen Sesam").
_AFTER_REMOVAL = _ARTICLES | _PREPOSITIONS | {"die", "der", "das", "den", "noch"}


def _removal_inside(words: list[str], named: list[int]) -> tuple[int, int, int] | None:
    """A removal said after the first allergy word ("allergisch gegen
    Erdnuesse, aber ohne Zwiebeln", Codex PR #330): where its connectors begin,
    where "ohne" stands, and where it ends, at a new dish or allergy or at the
    end. "ohne", or "keine" that an "aber" or "bitte" leads to ("aber keine
    Zwiebeln", "aber bitte keine", #331); a "keine" alone may still belong to
    the allergy ("ich vertrage keine Erdnuesse, keine Mandeln"), and one that
    denies an allergy word leaves out no food ("aber keine weiteren
    Allergien", Codex PR #361)."""
    at = next(
        (
            j
            for j in range(named[0] + 1, len(words))
            if words[j] == "ohne"
            or (
                words[j] in _KEIN
                and _asked_to_leave_out(words, j, named[0])
                and not _denies_allergy_word(words, j)
            )
        ),
        None,
    )
    if at is None:
        return None
    begin = at
    while begin > named[0] + 1 and words[begin - 1] in _CONNECTORS:
        begin -= 1
    # It ends before the phrase of any later allergy word, whatever joins it
    # ("sowie eine Sesamallergie", ". Ich habe auch eine ...", Codex PR #330).
    limit = next((j for j in range(at + 1, len(words)) if _is_allergy(words[j])), None)
    if limit is not None:
        limit = max(_phrase_start(words, limit), at + 1)
        while limit > at + 1 and words[limit - 1] in _JOINERS:
            limit -= 1
    else:
        limit = len(words)
    end = at + 1
    while end < limit:
        follows = words[end + 1] if end + 1 < len(words) else ""
        if words[end] in _JOINERS and (
            follows in _AFTER_REMOVAL
            or _is_allergy(follows)
            or follows.isdigit()
            or parse_cardinal(follows) is not None
        ):
            break
        end += 1
    return begin, at, end


def _asked_to_leave_out(words: list[str], j: int, lo: int) -> bool:
    """Does an "aber" or "bitte" lead to the "keine" at `j` over the
    connectors after `lo`?"""
    k = j - 1
    while k > lo and words[k] in _CONNECTORS:
        if words[k] in ("aber", "bitte"):
            return True
        k -= 1
    return False


def _denies_allergy_word(words: list[str], j: int) -> bool:
    """Does the negation at `j` deny an allergy word: "keine (weiteren)
    Allergien"?"""
    at = _noun_after(words, j + 1, words[j])
    return at < len(words) and _allergy_word(words[at])


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
    """The wish for one dish, judged against its options. None: nothing to
    note, the guest denied an allergy ("ich habe keine Allergie")."""
    text = _clean(text)
    tokens, words = _tokens(text)
    named = _allergy_words(words)
    if named:
        return _with_allergy(text, tokens, words, named, groups)
    return _classify(text, groups)


def kitchen_note(removal: str | None, allergy: str) -> str:
    """The kitchen note for an allergy and the removal the guest said with it,
    the allergy first as the kitchen reads it (E14): "WICHTIG: Keine Erdnuss.
    Grund: Allergie. Ohne Zwiebeln". One too long for an order line keeps the
    allergy alone: the removal is the part to lose."""
    if not removal:
        return allergy
    note = f"{allergy}. {removal[0].upper()}{removal[1:]}"
    return note if len(note) <= NOTE_MAX else allergy


def removal_with(wish: Wish) -> str | None:
    """The removal a wish carries: a note itself, the one next to an addition,
    or the one kept next to an allergy, to repeat it to the guest ("Nummer 13
    Pho Bo, ohne Zwiebeln")."""
    if wish.kind == "note":
        return wish.text
    if wish.kind != "allergy" or wish.note:
        return wish.note
    if wish.ingredient is None:
        return None
    allergy = ALLERGY_NOTE.format(ingredient=wish.ingredient) + ". "
    if not wish.text.startswith(allergy):
        return None
    removal = wish.text[len(allergy) :]
    return removal[0].lower() + removal[1:]


# Whose allergy it is (legal check L13). The caller's "ja" counts for themselves
# and their own child; for another adult it is no consent of that person, so
# that allergy is never noted with its reason. Read on the words alone: a person
# who is not the caller's child, after "mein", "unser" and the like ("meine
# Frau"), on their own ("ein Freund", "jemand"), or as the subject of the
# sentence ("er hat", "wir sind"). Anything else is the caller's own.
# ponytail: closed lists; a person they do not name keeps the allergy as the
# caller's own. Add the word when the call log shows it.
_POSSESSIVES = frozenset(
    {"mein", "meine", "meinem", "meinen", "meiner", "meines"}
    | {"unser", "unsere", "unserem", "unseren", "unserer", "unseres"}
    | {"sein", "seine", "seinem", "seinen", "seiner", "seines"}
)
_PERSONS = frozenset(
    {"frau", "mann", "ehefrau", "ehemann", "partner", "partnerin", "verlobte"}
    | {"verlobter", "lebensgefaehrte", "lebensgefaehrtin", "mutter", "vater"}
    | {"mama", "papa", "mutti", "eltern", "oma", "opa", "schwester", "bruder"}
    | {"schwager", "schwaegerin", "schwiegermutter", "schwiegervater", "tante"}
    | {"onkel", "cousin", "cousine", "neffe", "nichte", "enkel", "enkelin"}
    | {"nachbar", "nachbarin", "chef", "chefin", "gast", "gaeste", "besuch"}
)
# Persons that are someone else without a possessive in front.
_OTHERS = frozenset(
    {"freund", "freundin", "freunde", "kollege", "kollegin", "kollegen"}
    | {"begleitung", "jemand", "jemanden", "jemandem"}
)
# "er hat", "sie ist", "wir sind": the allergy of someone the caller speaks of.
# "wir" holds another adult too, and the caller cannot consent for them. Only
# the singular verbs after "sie": "Sie haben" is how the caller addresses the
# restaurant.
_THIRD_SUBJECTS = frozenset(
    {
        (subject, verb)
        for subject in ("er", "sie")
        for verb in ("hat", "ist", "vertraegt", "reagiert")
    }
    | {("wir", verb) for verb in ("haben", "sind", "vertragen", "reagieren")}
)


def of_another_adult(text: str) -> bool:
    """Does `text` speak of the allergy of an adult other than the caller?"""
    for word, follows in pairwise([*_words(text), ""]):
        if word in _OTHERS or (word in _POSSESSIVES and follows in _PERSONS):
            return True
        if (word, follows) in _THIRD_SUBJECTS:
            return True
    return False


# Where a part of an answer to "Wogegen?" begins: "gegen" or "auf" at the
# start or after "und", "aber", "sondern" ("Ohne Zwiebeln und auf
# Erdnuesse", "Ohne Zwiebeln, aber gegen Erdnuesse", Codex PR #330),
# "gegen" after a comma. An "auf" after a comma still ends the list
# ("Erdnuesse, auf jeden Fall"), and "auf jeden/keinen Fall" is no "auf".
_AGAINST = re.compile(
    r"^\s*(?:gegen|auf(?!\s+(?:jeden|keinen)\s+fall\b))\b|,\s*gegen\b"
    r"|\b(?:und|aber|sondern)\s+(?:gegen|auf(?!\s+(?:jeden|keinen)\s+fall\b))\b",
    re.IGNORECASE,
)
_BUT = re.compile(r"\baber\b", re.IGNORECASE)
# Before a removal in an answer: "Bitte ohne Zwiebeln", "Und ohne Zwiebeln".
_ANSWER_FILLERS = frozenset({"bitte", "aber", "und", "dann", "dafuer"})
# An answer to "Wogegen?" that names nothing: "Keine.", "Gar keine.", "Gegen
# nichts." ("Nein." stays a question: no to what is unclear).
_NOTHING = frozenset(
    {"kein", "keine", "keinen", "keinem", "keiner", "keinerlei", "nichts"}
)
_NOTHING_FILL = frozenset(
    {"gar", "gegen", "auf", "doch", "ueberhaupt", "nein", "danke"}
)


def _withdraws(text: str) -> bool:
    """ "Keine.", "Gar keine.", "Gegen nichts.": no allergy after all."""
    words = set(_words(text))
    return bool(words & _NOTHING) and words <= _NOTHING | _NOTHING_FILL


def _ingredient_answer(text: str) -> tuple[str, str | None]:
    """An answer to "Wogegen?" in its parts: the ingredient, and a removal
    said with it. "Ohne Zwiebeln und gegen Erdnuesse" -> ("Erdnuesse", "Ohne
    Zwiebeln"); "Erdnuesse und gegen Sesam" -> ("Erdnuesse und Sesam", None);
    "Keine Erdnuesse" -> ("Erdnuesse", None)."""
    parts = [part.strip(" ,.") for part in _AGAINST.split(text)]
    parts = [part for part in parts if part]
    # "Bitte ohne Zwiebeln": the removal behind a polite filler (Codex PR #330).
    removals = [
        p
        for p in parts
        if len(parts) > 1
        and [w for w in _words(p) if w not in _ANSWER_FILLERS][:1] == ["ohne"]
    ]
    bare = " und ".join(p for p in parts if p not in removals)
    # "Keine Erdnuesse", also "Ohne Zwiebeln." alone: the ingredient.
    bare = re.sub(r"^\s*(?:keine[nm]?|kein|ohne)\s+", "", bare, flags=re.IGNORECASE)
    return bare, ", ".join(removals) or None


# The parts of a sentence, with what joins them kept.
_PARTS = re.compile(r"(,|\bund\b)", re.IGNORECASE)


def only_denies(text: str) -> bool:
    """Does the sentence deny an allergy and name none ("Pho Bo, ich habe
    keine Allergie", "Ich habe keine Allergie und moechte Pho Bo zum
    Abholen")? Read on the wish behind the dish, or on every run of
    neighbouring parts with allergy words, each judged whole: split at its
    "und", "keine Allergien und Unvertraeglichkeiten gegen Laktose" would
    leave a part that reads as an intolerance (Codex PR #330, #338). As the
    stand-in model judges it (`sim/scripted_llm.py`)."""
    clauses = wish_candidates(text)
    if clauses and denies_allergy(clauses[-1][1]):
        return True
    parts = _PARTS.split(text)
    runs: list[list[int]] = []
    for n in range(0, len(parts), 2):
        if mentions_allergy(parts[n]):
            if runs and runs[-1][1] == n - 2:
                runs[-1][1] = n
            else:
                runs.append([n, n])
    return bool(runs) and all(
        denies_allergy("".join(parts[first : last + 1])) for first, last in runs
    )


# What an answer to "Wogegen?" may say besides its ingredient: "Ich bin gegen
# Sesam allergisch", "Ja, Erdnüsse", "Ich habe doch keine Allergie".
_ANSWER_WORDS = (
    _FILLERS
    | _NOTHING
    | _NOTHING_FILL
    | _ARTICLES
    | _NEGATION
    | _PREPOSITIONS
    | _AFFIRMATIONS
    | frozenset({"und", "oder", "sowie", "nur"})
)


def answers_only(text: str) -> bool:
    """Does the answer to "Wogegen?" give its ingredient, or that there is
    none, and nothing else? "Erdnüsse, aber bitte erst übermorgen" changes the
    booking as well, and "Erdnüsse, aber ohne Zwiebeln" the dish: the model
    has to read such a sentence (Codex PR #368)."""
    answer, removals = which_answer(text)
    if answer is None or removals:
        return False
    read = set(_words(answer.ingredient or "")) if isinstance(answer, Wish) else set()
    return all(
        word in read or word in _ANSWER_WORDS or _is_allergy(word)
        for word in _words(text)
    )


# Who speaks of an allergy in an answer: "Mein Mann hat eine Sesamallergie",
# "Mein Sohn verträgt keine Milch".
_SPEAKERS = (
    _POSSESSIVES
    | _PERSONS
    | _OTHERS
    | frozenset({"sohn", "tochter", "kind", "kinder", "baby", "er", "sie"})
    | frozenset({"hat", "ist", "vertraegt", "vertragen", "reagiert", "reagieren"})
)


def read_in_full(text: str, ingredient: str) -> bool:
    """Does `ingredient` hold every word of the answer to "Wogegen?" that may
    name one? "Erdnüsse, und mein Mann hat eine Sesamallergie" read as
    "Sesam" leaves the caller's peanut out (Codex PR #368)."""
    read = set(_words(ingredient))
    return all(
        word in read or word in _ANSWER_WORDS or word in _SPEAKERS or _is_allergy(word)
        for word in _words(text)
    )


# What an order starts with, never an ingredient ("eine Cola", "Nummer 12").
_ORDER_LEADS = frozenset({"und", "ein", "eine", "einen", "einmal", "nummer", "noch"})


def names_an_order(text: str, card: CardFormat, numbers: frozenset[str]) -> bool:
    """Is the answer to "Wogegen?" an order instead of an ingredient?

    The active menu decides what a card number is, not the import grammar
    (Codex PR #155, P2): `card` is its format, `numbers` its numbers as
    `canonical_card` writes them. "S12" and "Sushi zwölf" are numbers only
    where the menu has the prefix S, so "B12", "Vitamin B12" and the
    additives "E621" and "E 621" stay the ingredient. An order is

    - a sentence that names a number by rule A in the menu's card format
      ("S12", "S zwölf", "Nummer 12", "die 25g", also one the menu lacks or a
      number next to more: "S13", "12 oder 13", "S12 Lachs"),
    - any word that is a number on the menu ("die 12 mit Reis", "Milch und
      S12"), or
    - a sentence that opens like an order ("Eine Cola bitte").

    A number word or a number the menu does not have, next to another word,
    belongs to the ingredient ("Fünf-Gewürze-Pulver", "Zwei Sachen: Milch",
    "Gegen E 621").
    """
    words = re.findall(r"[^\W_]+", text.lower())
    ref, unclear = sole_item_number(text, card)
    return (
        ref is not None
        or unclear
        or any(canonical_card(w) in numbers for w in words)
        or (bool(words) and words[0] in _ORDER_LEADS)
    )


# Words of an answer to "Wogegen?" that is a sentence of its own, not a list
# of ingredients: someone speaks ("glaube ich", "muss ich ueberlegen"), or
# asks the agent to wait.
# ponytail: closed list as `_NO_INGREDIENT`; such an answer that slips
# through is read back with the draft, where the guest hears it.
_NOT_AN_INGREDIENT = frozenset(
    {"ich", "wir", "du", "er", "sie", "es", "man", "mir", "mich", "uns"}
    | {"moment", "augenblick", "sekunde", "warten", "ueberlegen", "nachschauen"}
)
# The answer to "Wogegen?" names no allergy after all.
WITHDRAWN: Literal["withdrawn"] = "withdrawn"


def which_answer(text: str) -> tuple[Wish | Literal["withdrawn"] | None, list[str]]:
    """The answer to "Wogegen sind Sie allergisch?", and the removals said
    with it ("Erdnuesse, aber ohne Zwiebeln"). Read as the stand-in model has
    read it since PR #139 (`sim/scripted_order.py`), so the core and the
    stand-in agree on it (T-11.1). A whole answer ("ich bin gegen Erdnuesse
    allergisch") carries its ingredient itself; only the bare word
    ("Erdnuesse", "gegen Erdnuesse") gets the sentence opening (Codex PR #139,
    P1). `WITHDRAWN` for "Keine.", "Gegen nichts." or "Ich habe doch keine
    Allergie" (#252); the allergy with its ingredient; None when the answer
    gives none, and the question stays open.

    Whether the answer orders something instead ("Eine Cola bitte") is for
    the caller to judge first: what a card number is depends on the menu."""
    wish = classify_wish(text, [])
    # "Keine, aber ohne Zwiebeln": nothing, and a removal (Codex PR #330).
    but = _BUT.search(text)
    nothing = _withdraws(text[: but.start()] if but else text)
    behind = classify_wish(text[but.end() :], []) if but and nothing else None
    if behind is not None and behind.kind == "allergy":
        # "Keine, aber ich bin gegen Sesam allergisch", "Keine, aber meine
        # Frau ...": whose allergy and for which dish is not clear, so the
        # question is asked again (#207, Codex PR #361).
        return None, []
    said: list[str | None] = []
    if wish is None or nothing or (wish.kind != "allergy" and denies_allergy(text)):
        said = [removal_with(behind) if behind is not None else None]
        if wish is not None and not nothing:
            said.append(removal_with(wish))
        return WITHDRAWN, [removal for removal in said if removal]
    if wish.kind != "allergy":
        # "Keine Erdnuesse" would otherwise give "Keine Keine Erdnuesse". A
        # removal said with it is no ingredient ("Ohne Zwiebeln und gegen
        # Erdnuesse"), nor is the "gegen" in front of one (#252).
        bare, removal = _ingredient_answer(text)
        said += [removal, wish.note]
        wish = classify_wish(f"allergisch gegen {bare}", [])
        if _NOT_AN_INGREDIENT & set(_words(bare)):
            # A bare answer is read up to its end: "Moment, da muss ich kurz
            # ueberlegen" would be an allergy against all of it (T-11.1).
            wish = None
    if wish is not None:
        said.append(removal_with(wish))
    removals = list(dict.fromkeys(removal for removal in said if removal))
    if wish is None or wish.kind != "allergy" or not wish.ingredient:
        return None, removals
    return wish, removals


def _with_allergy(
    text: str,
    tokens: list[re.Match[str]],
    words: list[str],
    named: list[int],
    groups: list[OptionGroup],
) -> Wish | None:
    """The guest's own allergy, and the wish said before it (issue #252).

    The phrase of the allergy runs to the end of the text, but for a removal
    with "ohne" said after its first allergy word (`_removal_inside`). What
    comes before the phrase is classified on its own. Only removals stay next
    to an allergy: after the allergy on the kitchen note, or in `note` while
    the agent still asks what the allergy is against. An addition next to an
    allergy is not taken yet (docs/01_STATUS.md). The ingredient is read from
    the whole text without the removal after it, so a list that begins before
    the phrase keeps all of it ("Milch, Ei und Nussallergie"); a removal said
    right before it may be listed as an allergy too, the safe side.

    Polarity as at `_KEIN`: a phrase that only denies an allergy gives the
    wishes said around it ("ich habe keine Allergie, aber bitte ohne
    Koriander"), None without one; a negation in the phrase mixed with more
    gives an allergy without an ingredient."""
    start = _clause_start(words, named[0], nearest=True)
    heard = words  # as said: `named` points into it
    # Whose allergy: read in its clause, as said ("ohne Zwiebeln fuer meine
    # Frau und ich habe eine Erdnussallergie" is the caller's own). A clause
    # that names no subject of its own is read from the comma or full stop
    # before its first word, or "er hat eine Nussallergie" would lose the
    # "er" (Codex PR #368).
    # The clause ends at the first comma or sentence end behind the allergy
    # word: "er hat eine Nussallergie, ich möchte Besteck dazu" stays his
    # allergy, the later "ich" is another request (Codex PR #368).
    word_at, word_end = tokens[named[0]].start(), tokens[named[0]].end()
    ends = [i for i in (text.find(mark, word_end) for mark in ",.;!?") if i != -1]
    clause_end = min(ends, default=len(text))
    clause = text[tokens[start].start() : clause_end]
    if not _CLAUSE_OPENERS & set(_words(clause)):
        before = max(text.rfind(mark, 0, word_at) for mark in ",.;!?")
        clause = text[before + 1 : clause_end]
    head = _clean(_drop_trailing_glue(text[: tokens[start].start()]).strip())
    after = _denial(words, named, start)
    if after is not None:
        tail = text[tokens[after].start() :] if after < len(tokens) else ""
        said = _clean(", ".join(part for part in (head, tail) if part))
        if not _WISHED & set(_words(said)):
            return None
        around = _classify(said, groups)
        # A wish the menu does not know may hide an intolerance ("aber mit
        # Gluten habe ich Probleme"): then it is not a plain denial, but the
        # removal said with it stays.
        if around.kind in ("note", "option", "open"):
            return around
        removal = removal_with(around)
    else:
        removal = removal_with(_classify(head, groups)) if head else None
        # A removal after the allergy ("allergisch gegen Erdnuesse, aber ohne
        # Zwiebeln", Codex PR #330) joins the one before it; the allergy is
        # read without it.
        inside = _removal_inside(words, named)
        if inside is not None:
            begin, at, end = inside
            stop = tokens[end].start() if end < len(tokens) else len(text)
            behind = _clean(text[tokens[at].start() : stop])
            removal = (
                ", ".join(
                    r for r in (removal, removal_with(_classify(behind, groups))) if r
                )
                or None
            )
            text = _clean(f"{text[: tokens[begin].start()]} {text[stop:]}")
            words = words[:begin] + words[end:]
    # A negation in front of the phrase ("die Sosse nicht auf den Reis") may
    # name a denied ingredient: then only the phrase is read. A "vertrage"
    # whose ingredient comes before it ("Sesam vertrage ich nicht") is not
    # read at all, so the whole is asked for, or that allergen would be lost.
    ingredient = None
    unread = any(
        heard[i].startswith(("vertrag", "vertraeg"))
        and not (i + 1 < len(heard) and heard[i + 1] in _KEIN)
        for i in named
    )
    if not unread and not _negated(words[start:]):
        ingredient = _ingredient(
            text[tokens[start].start() :] if _negated(words[:start]) else text
        )
    # Never noted with its reason, whatever the caller answers (L13).
    other = of_another_adult(clause) or None
    if ingredient is None:
        # The removal waits for "Wogegen?" and goes on the order line as it is
        # when the allergy is withdrawn: one too long for the line is lost, as
        # in `kitchen_note` (#331).
        if removal is None or len(removal) > NOTE_MAX:
            return Wish(text=text, kind="allergy", another_adult=other)
        return Wish(
            text=text[tokens[start].start() :],
            kind="allergy",
            note=removal,
            another_adult=other,
        )
    return Wish(
        text=kitchen_note(removal, ALLERGY_NOTE.format(ingredient=ingredient)),
        kind="allergy",
        ingredient=ingredient,
        another_adult=other,
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
    """With several hits: a removal and an allergy are settled already, an
    option only once the guest has chosen the dish. None: a denied allergy and
    nothing else, no wish."""
    wish = classify_wish(text, [])
    if wish is None or wish.kind == "note":
        return wish
    # A denial next to an addition is decided with the options of the dish
    # ("mit Nudeln, ich habe keine Allergie"): until then it is open.
    if wish.kind == "allergy" and not _denial_phrase(text):
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
