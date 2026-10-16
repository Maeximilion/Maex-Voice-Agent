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
_OLD_SEPARATOR = re.compile(r"\s*,\s*|\s+(?:und|sowie)\s+", re.IGNORECASE)
_OLD_COMPOUND = re.compile(
    r"(\w+?)-?(?:allergie|intoleranz|unvertr(?:ä|ae)glichkeit)"
    r"|(\w+)-(?=\s*(?:,|und|oder|sowie)\s)",
    re.IGNORECASE,
)


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
        ["und", "Und", "sowie", "oder", "die", "23"],
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
        lambda text: re.sub(r"[\s,]*bitte[\s.!?]*$", "", text, flags=re.IGNORECASE),
        lambda text: wishes._cut_tail(wishes._PLEASE_AT_END, text),
        ["bitte", "Bitte", "und", "?"],
    ),
    # Without IGNORECASE, as it was: "Und" at the end stays.
    "joining word before the next part": (
        lambda text: re.sub(r"[\s,]*(?:dafür|dafuer|aber|und)[\s,]*$", "", text),
        lambda text: wishes._cut_tail(wishes._JOINER_AT_END, text),
        ["dafür", "dafuer", "aber", "und", "Und", "Aber", "Hund"],
    ),
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
