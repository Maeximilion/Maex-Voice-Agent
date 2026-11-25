"""The patterns on caller text stay linear (CodeQL py/polynomial-redos, alerts 3 to 12).

`domain/menu/split.py` and `domain/menu/wishes.py` cut a spoken sentence with
regular expressions. Ten of them took quadratic time on a long run of one
character: the engine entered the run again at every character of it. The
request schema caps `query` at 300 characters, so a tool call stayed in the
millisecond range, but `sim/` and any later caller reach these functions
without that cap.

Each case below feeds one such run through a public function. The length is
chosen so that quadratic code needs several seconds and linear code a few
milliseconds; the budget sits in between with a wide margin on both sides.

The fix takes the repetition off the front of each pattern and removes the
separators in front of a match in plain code instead; the pattern for compound
allergy words became a small scanner. What comes out must not change with that,
so the second test keeps each operation as it was written before, as the
reference, and compares the two on generated sentences.
"""

import random
import re
import time

import pytest

from api.domain.menu import split, wishes
from api.domain.menu.numberwords import (
    fold,
    inside_split,
    parse_cardinal,
    pause_before_und,
    tens_half,
)
from api.domain.menu.split import raw_pieces, separator_pieces, split_positions
from api.domain.menu.wishes import classify_wish, wish_candidates

LENGTH = 20_000
# A chain of joining words repeats a four-character unit and had a smaller
# constant, so it needs a longer text to show.
CHAIN_LENGTH = 100_000
BUDGET_SECONDS = 1.0

CASES = [
    # split.py, _SEPARATOR in _separable (alert 3)
    ("split_positions, spaces", split_positions, "die 23" + " " * LENGTH + "x"),
    ("raw_pieces, tabs", raw_pieces, "die 23" + "\t" * LENGTH + "x"),
    # split.py, _SEPARATOR in _split (alerts 4 and 5): a real separator at the
    # end lets the sentence through to the cut
    (
        "split_positions, spaces before a separator",
        split_positions,
        "die 23" + " " * LENGTH + "x und die 24",
    ),
    ("separator_pieces, spaces", separator_pieces, "Fisch" + " " * LENGTH + "Chips"),
    # wishes.py, _clean and the segment in wish_candidates (alerts 7 to 10)
    ("wish_candidates, tabs", wish_candidates, "die 23 ohne" + "\t" * LENGTH + "x"),
    (
        "wish_candidates, commas",
        wish_candidates,
        "die 23 ohne x" + ", " * (LENGTH // 2) + "y",
    ),
    (
        "wish_candidates, chain of joining words",
        wish_candidates,
        "die 23 mit Salami" + " und" * (CHAIN_LENGTH // 4) + " x",
    ),
    # wishes.py, _COMPOUND in _ingredient (alert 6)
    (
        "classify_wish, one long word after an allergy",
        lambda text: classify_wish(text, []),
        "ich habe eine allergie " + "a" * LENGTH,
    ),
    # wishes.py, the ingredient between "gegen" and "allergisch" (issue #252)
    (
        "classify_wish, spaces after gegen",
        lambda text: classify_wish(text, []),
        "ich bin allergisch, gegen" + " " * LENGTH + "x",
    ),
    (
        "classify_wish, gegen inside words",
        lambda text: classify_wish(text, []),
        "allergie " + "gegen x" * (LENGTH // 7),
    ),
    # wishes.py, the polarity of many allergy words (own review, issue #252)
    (
        "classify_wish, many allergy words after a denial",
        lambda text: classify_wish(text, []),
        "nicht gegen x " + "allergisch " * (LENGTH // 11),
    ),
    (
        "classify_wish, a chain of allergy phrases",
        lambda text: classify_wish(text, []),
        "gegen x allergisch " * (LENGTH // 19),
    ),
    # wishes.py, a list carried on behind its own "gegen" (review of PR #330)
    (
        "classify_wish, a chain of second gegen",
        lambda text: classify_wish(text, []),
        "ich bin allergisch gegen x" + " und gegen x" * (LENGTH // 12),
    ),
    # wishes.py, the list behind a closing allergy word (issue #207)
    (
        "classify_wish, a chain of lists behind allergy words",
        lambda text: classify_wish(text, []),
        "gegen x allergisch und gegen x" * (LENGTH // 30),
    ),
    (
        "classify_wish, a chain of allergy words with a list",
        lambda text: classify_wish(text, []),
        "allergie, auch gegen x " * (LENGTH // 23),
    ),
    (
        "classify_wish, many gegen without a list",
        lambda text: classify_wish(text, []),
        "allergisch " + "gegen " * (LENGTH // 6),
    ),
    # wishes.py, _split_addition (alert 11) and _split_removal (alert 12)
    (
        "classify_wish, tabs before an addition",
        lambda text: classify_wish(text, []),
        "ohne x" + "\t" * LENGTH + "mit y",
    ),
    (
        "classify_wish, tabs before a removal",
        lambda text: classify_wish(text, []),
        "mit x" + "\t" * LENGTH + "ohne y",
    ),
    (
        # One round of `_without_glue` per word: "und." stops the scanner.
        "classify_wish, a chain of joining words before an addition",
        lambda text: classify_wish(text, []),
        "ohne x" + " und." * (CHAIN_LENGTH // 5) + " mit y",
    ),
    (
        # Directly, ten times longer: a copy of the rest per word (Codex PR
        # #1025) took about 8 s here, the walking end index a fraction of one.
        "_without_glue, a long chain of joining words",
        wishes._without_glue,
        "ohne x" + " und." * (CHAIN_LENGTH * 2),
    ),
    # split.py, _split: every piece that opens no position attaches to the part
    # before, and the words of that growing part were folded again at every
    # separator (docs/24). 10,000 pieces took about 30 s and 9 s before, a
    # tenth of a second after.
    (
        "split_positions, a chain of attached pieces",
        split_positions,
        "die 23" + " und süß" * (LENGTH // 2),
    ),
    (
        "cut_at_separators, a chain of wishes",
        split.cut_at_separators,
        "die 23" + ", mit x" * (LENGTH // 2),
    ),
]


# Wall clock: runs serially with the latency tests, not under xdist (docs/13 §6).
@pytest.mark.latency
@pytest.mark.parametrize(
    ("call", "text"),
    [(call, text) for _, call, text in CASES],
    ids=[name for name, _, _ in CASES],
)
def test_long_run_of_one_character_stays_linear(call, text):
    start = time.perf_counter()
    call(text)
    elapsed = time.perf_counter() - start
    assert elapsed < BUDGET_SECONDS, f"{elapsed:.2f} s for {len(text)} characters"


# The operations as they were before the fix. They are the reference here and
# nothing else: do not use them on text a caller controls.
# With the decimal comma of #676: no cut between two digits ("0,5").
_OLD_SEPARATOR = re.compile(
    r"\s*(?<!\d),\s*|\s*,(?!\d)\s*|\s+(?:und|sowie)\s+", re.IGNORECASE
)
_OLD_COMPOUND = re.compile(
    r"(\w+?)-?(?:allergie|intoleranz|unvertr(?:ä|ae)glichkeit)"
    r"|(\w+)-(?=\s*(?:,|und|oder|sowie)\s)",
    re.IGNORECASE,
)


_OLD_AGAINST = re.compile(
    r"gegen\s+((?:(?!\bgegen\b)[^.;!?])+?)\s+allergisch", re.IGNORECASE
)


def _old_against(text):
    return [(found.start(1), found.group(1)) for found in _OLD_AGAINST.finditer(text)]


def _old_pieces(text):
    text = text.strip()
    return _OLD_SEPARATOR.split(text), [
        found.strip() for found in _OLD_SEPARATOR.findall(text)
    ]


def _new_pieces(text):
    text = text.strip()
    return [piece.strip() for piece in split._SEPARATOR.split(text)], [
        found.strip() for found in split._SEPARATOR.findall(text)
    ]


def _old_drop_quantity(text):
    text = re.sub(
        r"[\s,]*\b\d+\s*(?:x|portionen?|stück|stueck)\b", "", text, flags=re.IGNORECASE
    )
    return re.sub(
        r"[\s,]*\b(\w+?)mal\b",
        lambda found: "" if wishes._is_quantity(found.group(1)) else found.group(0),
        text,
        flags=re.IGNORECASE,
    )


def _old_compound_stems(text):
    return [
        (found.start(), found.group(1) or found.group(2), bool(found.group(1)))
        for found in _OLD_COMPOUND.finditer(text)
    ]


# name -> (as it was, as it is, words that make it do something)
REFERENCE = {
    "pieces at the separators": (
        _old_pieces,
        _new_pieces,
        ["und", "Und", "sowie", "oder", "die", "23", "0,5", "8, 50"],
    ),
    "joining words at the end": (
        lambda text: re.sub(
            r"(?:[\s,]+(?:aber|und|dafür|dafuer|dann|bitte))+[\s,]*$",
            "",
            text,
            flags=re.IGNORECASE,
        ),
        wishes._drop_trailing_glue,
        ["aber", "und", "Und", "dafür", "dafuer", "dann", "bitte", "Hund"],
    ),
    "bitte at the end": (
        lambda text: re.sub(r"[\s,]*\bbitte[\s.!?]*$", "", text, flags=re.IGNORECASE),
        lambda text: wishes._cut_tail(wishes._PLEASE_AT_END, text),
        ["bitte", "Bitte", "und", "?", "Ab"],
    ),
    # The word boundary in front of "bitte" is newer than the CodeQL fix:
    # "Abbitte" stays (docs/HISTORY.md). The joining word in front of the next
    # part of a wish is cut by `_without_glue` now, timed in CASES above.
    "quantities": (
        _old_drop_quantity,
        wishes._drop_quantity,
        ["mal", "zweimal", "einmal", "normal", "2", "x", "portion", "stück", "_"],
    ),
    "stems of compound allergy words": (
        _old_compound_stems,
        lambda text: list(wishes._compound_stems(text)),
        [
            "allergie",
            "Allergie",
            "intoleranz",
            "unverträglichkeit",
            "unvertraeglichkeit",
            "Nuss",
            "Milch",
            "s",
            "-",
            "-",
            "und",
            "oder",
        ],
    ),
    "ingredient between gegen and allergisch": (
        _old_against,
        lambda text: list(wishes._against_allergic(text)),
        [
            "gegen",
            "Gegen ",
            "gegen ",
            "entgegen ",
            " allergisch",
            "Allergisch",
            "allergisch ",
            "   ",
            "Nuss",
            "gegen x allergisch",
            ".",
            "?",
        ],
    ),
}
_FILLER = [" ", " ", " ", "\t", "\n", ",", ",", ".", "!", "-", "a", "ä", "x"]
SENTENCES_PER_OPERATION = 4000
SENTENCES_WITH_AN_EFFECT_AT_LEAST = 150


@pytest.mark.parametrize("name", list(REFERENCE))
def test_result_is_the_same_as_before(name):
    before, after, words = REFERENCE[name]
    rng = random.Random(len(name))
    atoms = _FILLER + words
    nothing = (before(""), before("a"))
    with_an_effect = 0
    for _ in range(SENTENCES_PER_OPERATION):
        text = "".join(rng.choice(atoms) for _ in range(rng.randint(0, 10)))
        expected = before(text)
        assert after(text) == expected, repr(text)
        with_an_effect += expected not in (text, *nothing, ([text.strip()], []))
    # The comparison means nothing if the sentences never trigger the operation.
    assert with_an_effect >= SENTENCES_WITH_AN_EFFECT_AT_LEAST


# Inputs the random sentences rarely build: a run of three or more spaces in
# front of "allergisch" (the old pattern matched one of them as the
# ingredient), several phrases in a row, and "gegen" inside a word.
AGAINST_EDGES = [
    "gegen allergisch",
    "gegen  allergisch",
    "gegen   allergisch",
    "gegen \t\n allergisch",
    "gegen    allergisch gegen x allergisch",
    "gegen   allergisch   allergisch",
    "gegen x  allergisch",
    "gegen x allergisch gegen y allergisch",
    "gegen gegen x allergisch",
    "gegen x gegen y allergisch",
    "entgegen x allergisch",
    "gegenx gegen y allergisch",
    "gegen x. allergisch",
    "gegen x, y allergisch",
    "GEGEN Nüsse ALLERGISCH",
    # Dotless i and long s: equal to i and s under IGNORECASE.
    "gegen \u0131x allergi\u017fch",
    "ich bin gegen Nüsse und gegen Sesam allergisch",
    "ich bin nicht gegen Erdnüsse, sondern gegen Sesam allergisch",
]


@pytest.mark.parametrize("text", AGAINST_EDGES)
def test_against_allergic_matches_the_old_pattern(text):
    assert list(wishes._against_allergic(text)) == _old_against(text)


def _words(text):
    return split._WORD.findall(fold(text))


def test_split_words_of_a_joined_part_are_the_words_of_its_pieces():
    # `_split` keeps the words of the growing part instead of folding it
    # again; that is only the same when every run of pieces, joined with its
    # separators, has exactly the words of its pieces and separators in a row.
    atoms = [*_FILLER, "und", "Und", "UND", "sowie", "23", "0,5", "8, 50", "ß"]
    atoms += ["Süß", "İ", "Σ", "drei", "zwanzig", "hundert", "äh", " und ", " Sowie "]
    rng = random.Random(1)
    # Joined runs across a separator that is a word itself ("und", "sowie")
    # and has words on both sides: the case the comma runs do not cover.
    across_a_word = 0
    for _ in range(SENTENCES_PER_OPERATION):
        text = "".join(rng.choice(atoms) for _ in range(rng.randint(0, 12)))
        pieces = split._SEPARATOR.split(text)
        separators = split._SEPARATOR.findall(text)
        for start in range(len(pieces)):
            joined = pieces[start]
            words = _words(joined)
            for sep, piece in zip(separators[start:], pieces[start + 1 :], strict=True):
                across_a_word += bool(words and _words(sep) and _words(piece))
                joined += sep + piece
                words += _words(sep) + _words(piece)
                assert words == _words(joined), repr(text)
    assert across_a_word >= SENTENCES_WITH_AN_EFFECT_AT_LEAST


# `_split` as it was before it kept the words of the growing part (docs/24),
# the reference for the test below and nothing else.
def _old_split(text, *, by_position=True):
    pieces = split._SEPARATOR.split(text)
    separators = split._SEPARATOR.findall(text)
    parts = [pieces[0]]
    for sep, piece in zip(separators, pieces[1:], strict=True):
        left = _words(parts[-1])
        right = _words(piece)
        a = len(left)
        while a and pause_before_und(left[a - 1]):
            a -= 1
        b = 0
        while b < len(right) and inside_split(right[b]):
            b += 1
        und = sep.strip().casefold() == "und"
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
                or (
                    a > 0
                    and left[a - 1].endswith("hundert")
                    and split._only_number(piece)
                )
                or (
                    a > 0
                    and parse_cardinal(left[a - 1]) in split._UNITS
                    and tens_half(right[b])
                )
            )
        )
        if by_position:
            attached = not split._opens_position(piece)
        else:
            attached = split._modifies(piece)
        if number_joined or attached:
            parts[-1] += sep + piece
        else:
            parts.append(piece)
    return parts


SPLIT_ATOMS = [
    *[" ", " ", " ", "\t", ",", ", ", "und,", ".", "x", "Cola", "süß", "Pho Bo"],
    *["und", "Und", "sowie", "drei", "zwanzig", "hundert", "eins", "eine", "die"],
    *["23", "4", "äh", "bitte", "mit", "ohne", "einmal", "zweimal"],
]
SPLITS_COMPARED = 20_000


def test_split_is_the_same_as_before():
    # The random sentences reach every line that keeps the words of the
    # growing part: dropping or changing any one of them made this test fail.
    rng = random.Random(3)
    attached = 0
    for _ in range(SPLITS_COMPARED):
        glue = rng.choice([" ", "\t", ""])
        text = glue.join(rng.choice(SPLIT_ATOMS) for _ in range(rng.randint(1, 10)))
        for by_position in (True, False):
            expected = _old_split(text, by_position=by_position)
            assert split._split(text, by_position=by_position) == expected, repr(text)
            attached += len(expected) <= len(split._SEPARATOR.findall(text))
    assert attached >= SENTENCES_WITH_AN_EFFECT_AT_LEAST
