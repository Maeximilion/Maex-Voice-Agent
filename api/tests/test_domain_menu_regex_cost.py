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

The fix only adds look-behinds that say where a match may begin. What a pattern
matches must not change with that, so the second test keeps each pattern in its
plain form as the reference and compares the two on generated sentences.
"""

import random
import re
import time

import pytest

from api.domain.menu import split, wishes
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
]


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


# name -> (the plain pattern, the pattern in the code, words that make it match)
_ALLERGY_NOUNS = r"allergie|intoleranz|unvertr(?:ä|ae)glichkeit"
REFERENCE = {
    "_SEPARATOR": (
        r"\s*,\s*|\s+(?:und|sowie)\s+",
        split._SEPARATOR,
        ["und", "Und", "sowie", "oder", "die", "23"],
    ),
    "_TRAILING_GLUE": (
        r"(?:[\s,]+(?:aber|und|dafür|dafuer|dann|bitte))+[\s,]*$",
        wishes._TRAILING_GLUE,
        ["aber", "und", "Und", "dafür", "dafuer", "dann", "bitte", "Hund"],
    ),
    "_TRAILING_PLEASE": (
        r"[\s,]*bitte[\s.!?]*$",
        wishes._TRAILING_PLEASE,
        ["bitte", "Bitte", "und", "?"],
    ),
    "_TRAILING_JOINER": (
        r"[\s,]*(?:dafür|dafuer|aber|und)[\s,]*$",
        wishes._TRAILING_JOINER,
        ["dafür", "dafuer", "aber", "und", "Und", "Hund"],
    ),
    "_TIMES": (
        r"[\s,]*\b(\w+?)mal\b",
        wishes._TIMES,
        ["mal", "zweimal", "einmal", "normal", "2", "_"],
    ),
    "_COUNTED": (
        r"[\s,]*\b\d+\s*(?:x|portionen?|stück|stueck)\b",
        wishes._COUNTED,
        ["2", "23", "x", "X", "portion", "portionen", "stück", "stueck"],
    ),
    "_COMPOUND": (
        rf"(\w+?)-?(?:{_ALLERGY_NOUNS})|(\w+)-(?=\s*(?:,|und|oder|sowie)\s)",
        wishes._COMPOUND,
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
}
_FILLER = [" ", " ", " ", "\t", "\n", ",", ",", ".", "!", "-", "a", "ä", "x"]
SENTENCES_PER_PATTERN = 4000
MATCHING_SENTENCES_AT_LEAST = 150


@pytest.mark.parametrize("name", list(REFERENCE))
def test_look_behind_changes_no_match(name):
    plain, hardened, words = REFERENCE[name]
    reference = re.compile(plain, re.IGNORECASE)
    rng = random.Random(len(name))
    atoms = _FILLER + words
    matched = 0
    for _ in range(SENTENCES_PER_PATTERN):
        text = "".join(rng.choice(atoms) for _ in range(rng.randint(0, 10)))
        expected = [(m.span(), m.groups()) for m in reference.finditer(text)]
        found = [(m.span(), m.groups()) for m in hardened.finditer(text)]
        assert found == expected, repr(text)
        matched += bool(expected)
    # The comparison means nothing if the sentences never match.
    assert matched >= MATCHING_SENTENCES_AT_LEAST
