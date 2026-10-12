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
  was spelled letter by letter in one sentence (docs/05 §2, rung 3), and it
  holds nothing but those words;
- a number, when it is the caller ID or a phone number the guest said: one run
  of digits in one sentence that begins with an area code or a country code,
  compared in E.164. A date, a time or a number without an area code is no
  phone number, whatever a model makes of its digits.

Which of the words a guest said is the name stays with the model, and the
readback follows it. Strict on purpose: a name or number that is missed costs
the guest one more question, one that is wrongly taken books under a
stranger.

The sentences behind each rule are the cases of
`api/tests/test_agent_guest_details.py`.

ponytail: words are not understanding. Not heard: a number said in words
("null eins sieben ..."), a number without its area code or with a country
code and no plus, a number with a comma or a spaced dash between its
digits or named in two sentences, letter names when spelling ("Doppel-L", "Ypsilon"),
a name written with another accent than it was said with, and a spelling the
agent proposed and the guest only agreed to ("Maier mit a-i?" - "Ja"). Each
ends in a question for the guest, never in a booking. Digits that follow a
number after a blank only ("0171 2345678 19 Uhr") are read as one number; a
model that writes all of them is not stopped. The ear of the voice layer
decides how numbers and letters arrive as text (T-10.5); read them here once
it shows that.
ponytail: a name is known from guest sentences only. Once a known caller's
name comes from the database (`find_customer`, stage 3), that name has to
count as well, the way the caller ID does for the number.
"""

import re

from api.core.errors import InvalidInput
from api.domain.customers.phone import normalize_phone
from api.domain.menu.numberwords import fold

# Letters only: digits and punctuation belong to no name.
_WORD = re.compile(r"[^\W\d_]+")
# What may stand between two words of a name, and nowhere else in it.
_IN_A_NAME = frozenset({" ", "-", "'", ", ", ". "})
# Digits with everything that can stand between digits. A word ends the run.
_DIGIT_RUN = re.compile(r"\+?\d[\d\s\-/().,]*")
# A period before a blank ends a sentence, and the number with it.
_SENTENCE_END = re.compile(r"\.\s+")
# With digits on both sides, a comma or a dash next to a blank leaves open
# where a number ends: "0171 2345678 - 19 Uhr" and "0721 - 5551234", "0171
# 2345678, 4 Personen" and "0721 555, 1234" look alike.
_UNCLEAR_END = re.compile(r",|\s-|-\s")
# What may stand around a number without being part of it.
_AROUND_A_NUMBER = " \t\r\n\f\v-/().,"
# A date or a time begins with one or two digits and a separator. No phone
# number does: an area code has three digits or more, the zero included.
_DATE_LIKE = re.compile(r"\d{1,2}[./-]")


def words_said(sentence: str) -> set[str]:
    """The words of a guest sentence, folded, and what the guest spelled in
    it."""
    lowered = sentence.lower()
    return {fold(word) for word in _WORD.findall(lowered)} | _spelled(lowered)


def _spelled(sentence: str) -> set[str]:
    """What single letters spell that follow each other: "M E I E R", "M, A,
    I, E, R" or "M wie Martha, A wie Anton", where the word behind "wie" is
    passed over. Any other word and any digit ends a row, and a letter that
    stands at a digit is none of a name: in "die 23a. Mein Name ist M E I E R"
    the guest spelled "meier", not "ameier" (Codex PR #244, P2). One letter
    alone spells nothing."""
    spelled: set[str] = set()
    row: list[str] = []
    after_wie = False
    end = 0
    for match in _WORD.finditer(sentence):
        word, start, stop = match.group(), match.start(), match.end()
        digit_between = any(char.isdigit() for char in sentence[end:start])
        end = stop
        if after_wie and not digit_between:
            after_wie = False
            continue
        after_wie = False
        at_a_digit = (
            sentence[start - 1 : start].isdigit() or sentence[stop : stop + 1].isdigit()
        )
        if digit_between:
            row = _close(row, spelled)
        if word == "wie" and row:
            after_wie = True
        elif len(word) == 1 and not at_a_digit:
            row.append(fold(word))
        else:
            row = _close(row, spelled)
    _close(row, spelled)
    return spelled


def _close(row: list[str], spelled: set[str]) -> list[str]:
    if len(row) > 1:
        spelled.add("".join(row))
    return []


def name_said(name: object, words: set[str]) -> bool:
    """Every word of the name is one the guest said, and the name is nothing
    but those words with one separator between two of them: a digit, a
    bracket or a row of dashes a model adds was not said either, and the tool
    would store it (Codex PR #244, P2 twice). "Dr." may end with its period.
    A name without a letter is none."""
    if not isinstance(name, str):
        return False
    written = " ".join(name.split())
    parts = [fold(word) for word in _WORD.findall(written.lower())]
    if not parts or not all(part in words for part in parts):
        return False
    before, *between, after = _WORD.split(written)
    return (
        before == ""
        and after in ("", ".")
        and all(gap in _IN_A_NAME for gap in between)
    )


def numbers_said(sentence: str) -> set[str]:
    """The phone numbers of a guest sentence in E.164: each run of digits
    whose end is clear. A period that ends a sentence ends the run: in "0171
    2345678. 19 Uhr passt" the number is not twelve digits long, and in "am
    16.09. 0171 2345678" it does not begin with the date.

    A run with a comma or a spaced dash between its digits is no number, as a
    whole or in parts: taken whole, "0171 2345678 - 19 Uhr" is a number
    nobody said; taken apart, "0171 234 - 5678" is one with digits missing
    (Codex PR #244, P1). The guest is asked again and says it plainly."""
    pieces = (
        piece.strip(_AROUND_A_NUMBER)
        for run in _DIGIT_RUN.findall(sentence)
        for piece in _SENTENCE_END.split(run)
    )
    return {
        number
        for piece in pieces
        if not _UNCLEAR_END.search(piece) and (number := e164(piece))
    }


def number_known(number: object, caller: str | None, numbers: set[str]) -> bool:
    """`caller` is the caller ID in E.164, `numbers` what the guest said. A
    model may write the number in another spelling, never with other digits."""
    written = e164(number)
    return bool(written) and (written == caller or written in numbers)


def e164(written: object) -> str:
    """The phone number in E.164, the one form the same number has. Empty for
    what is no phone number as it is said: only digits that begin with the
    zero of an area code or the plus of a country code are one. A date, a
    time, a number of guests or a year are digits too, and `normalize_phone`
    would make "16.09.2026" the number +16092026 (Codex PR #244, P1). A number
    without an area code is none either: it would be stored as an
    international one. Nor is one that is too short or too long: kept by its
    digits, "um 0 Uhr" would be a number a model could put in the place of
    the caller ID."""
    if not isinstance(written, str):
        return ""
    # "(0721) 5551234" begins with the area code as well.
    text = written.strip().lstrip("( ")
    if not text.startswith(("0", "+")) or _DATE_LIKE.match(text):
        return ""
    try:
        return normalize_phone(written)
    except InvalidInput:
        return ""
