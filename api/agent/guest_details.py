"""Did the guest say this name, this phone number? (CLAUDE.md §2 rule 2)

A model writes the guest's name and number in two places: into `slots`, and
into the arguments of the tools that book under them. On a real model both
were invented to get a draft through ("Max Mustermann", "Gast", a number
nobody had said), and once the caller ID came back with digits missing
(07.10.2026). The name is read back, the number is not: an order can end
under a number that reaches a stranger.

So the core hears both itself, in the guest's own sentences and before the
model is asked, the way `intent.py` hears the wish: plain words, no
understanding of language, no tokens. It keeps no sentence, only the words
and the numbers of the call (`state.note_guest_details`), and takes from a
model only what stands in there:

- a name, when each of its words was said as a whole word in this call, or
  was spelled letter by letter in one sentence (docs/05 §2, rung 3);
- a number, when it is the caller ID or a number the guest said: one run of
  digits in one sentence, compared in E.164.

Which of the words a guest said is the name stays with the model, and the
readback follows it. Strict on purpose: a name or number that is missed costs
the guest one more question, one that is wrongly taken books under a
stranger.

The sentences behind each rule are the cases of
`api/tests/test_agent_guest_details.py`.

ponytail: words are not understanding. Not heard: a number said in words
("null eins sieben ..."), digit groups a guest separates with a comma or
names in two sentences, letter names when spelling ("Doppel-L", "Ypsilon"),
a name written with another accent than it was said with, and a spelling the
agent proposed and the guest only agreed to ("Maier mit a-i?" - "Ja"). Each
ends in a question for the guest, never in a booking. The ear of the voice layer
decides how numbers and letters arrive as text (T-10.5); read them here once
it shows that.
"""

import re

from api.core.errors import InvalidInput
from api.domain.customers.phone import normalize_phone
from api.domain.menu.numberwords import fold

# Letters only: digits and punctuation belong to no name.
_WORD = re.compile(r"[^\W\d_]+")
# One number as it is said or typed: digits with the separators
# `normalize_phone` drops between them. A comma or a word ends it.
_DIGIT_RUN = re.compile(r"\+?\d[\d\s\-/().]*")


def words_said(sentence: str) -> set[str]:
    """The words of a guest sentence, folded, and what its single letters
    spell when read in a row: "M wie Martha, A wie Anton" spells "ma", the
    words of a spelling alphabet are no single letters."""
    words = _WORD.findall(sentence.lower())
    spelled = "".join(fold(word) for word in words if len(word) == 1)
    # Without a single letter `spelled` is empty, which is no part of any name.
    return {fold(word) for word in words} | {spelled}


def name_said(name: object, words: set[str]) -> bool:
    """Every word of the name is one the guest said. A name without a letter
    is none."""
    if not isinstance(name, str):
        return False
    parts = [fold(word) for word in _WORD.findall(name.lower())]
    return bool(parts) and all(part in words for part in parts)


def numbers_said(sentence: str) -> set[str]:
    """The numbers of a guest sentence, each run of digits as one."""
    return {_key(run.group()) for run in _DIGIT_RUN.finditer(sentence)}


def number_known(number: object, caller: str | None, numbers: set[str]) -> bool:
    """`caller` is the caller ID in E.164, `numbers` what the guest said. A
    model may write the number in another spelling, never with other digits."""
    if not isinstance(number, str):
        return False
    key = _key(number)
    return bool(key) and (key == caller or key in numbers)


def _key(written: str) -> str:
    """One form for the same number: E.164. What is no phone number keeps its
    digits, so a number the guest gave too short still counts as said: the
    tool then answers that it was not understood, in its own sentence, where
    dropping it here would let the booking go on under the caller ID."""
    try:
        return normalize_phone(written)
    except InvalidInput:
        return "".join(filter(str.isdigit, written))
