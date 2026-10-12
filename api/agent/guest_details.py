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
- a number, when it is the caller ID or a phone number the guest said: the
  last digits of a turn, when they begin with an area code or a country code,
  compared in E.164. A date, a time or a number without an area code is no
  phone number, whatever a model makes of its digits, and digits that are
  followed by more digits are none either: the number may go on.

Which of the words a guest said is the name stays with the model, and the
readback follows it. Strict on purpose: a name or number that is missed costs
the guest one more question, one that is wrongly taken books under a
stranger.

The sentences behind each rule are the cases of
`api/tests/test_agent_guest_details.py`.

ponytail: words are not understanding. Not heard: a number said in words
("null eins sieben ..."), a number without its area code or with a country
code and no plus, a number that is followed by other digits in the same
turn, one with a dash or a slash between blanks ("0721 / 5551234") or named
in two turns, letter names when spelling ("Doppel-L", "Ypsilon"),
a name written with another accent than it was said with, and a spelling the
agent proposed and the guest only agreed to ("Maier mit a-i?" - "Ja"). Each
ends in a question for the guest, never in a booking.
ponytail: blanks alone do not tell where a number ends. "0171 2345678 19
Uhr" is read as one number of thirteen digits; a model that writes exactly
those digits is not stopped. The ear of the voice layer
decides how numbers and letters arrive as text (T-10.5); read them here once
it shows that.
ponytail: a name is known from guest sentences only. Once a known caller's
name comes from the database (`find_customer`, stage 3), that name has to
count as well, the way the caller ID does for the number.
"""

import re

from api.core.errors import InvalidInput
from api.domain.customers.phone import TRUNK_IN_BRACKETS, normalize_phone
from api.domain.menu.numberwords import CARD_SUFFIXES, fold

# Letters only: digits and punctuation belong to no name.
_WORD = re.compile(r"[^\W\d_]+")
# What may stand between two words of a name, and nowhere else in it.
_IN_A_NAME = frozenset({" ", "-", "'", ", ", ". "})
# Digits with everything that can stand between digits. A word ends the run.
_DIGIT_RUN = re.compile(r"\+?\d[\d\s\-/().,]*")
# Where one number ends inside such a run and other digits begin: a comma, a
# period before a blank, a dash or a slash next to a blank, a bracket that
# opens behind a blank.
_NUMBER_END = re.compile(r",|\.\s|\s[-/(]|[-/]\s")
# What may stand around a number without being part of it.
_AROUND_A_NUMBER = " \t\r\n\f\v-/().,"
# What may stand between two letters a guest spells.
_BETWEEN_LETTERS = " \t,-."
# Other digits of a turn with this many digits may be the head of the number.
_HEAD_DIGITS = 6
# A single letter right behind one of these belongs to the word before it.
_APOSTROPHES = ("'", "\N{RIGHT SINGLE QUOTATION MARK}")
# A date or a time begins with one or two digits and a separator. No phone
# number does: an area code has three digits or more, the zero included.
_DATE_LIKE = re.compile(r"\d{1,2}[./-]")
# The same date written with blanks: day, month, year. Told from a number in
# three groups by the day and the month being one.
_SPACED_DATE = re.compile(r"(\d{1,2})\s+(\d{1,2})\s+(?:\d{2}|\d{4})")


def words_said(sentence: str) -> set[str]:
    """The words of a guest sentence, folded, and what the guest spelled in
    it."""
    lowered = sentence.lower()
    return {fold(word) for word in _WORD.findall(lowered)} | _spelled(lowered)


def _spelled(sentence: str) -> set[str]:
    """What single letters spell that follow each other: "M E I E R", "M, A,
    I, E, R" or "M wie Martha, A wie Anton", where the word behind "wie" is
    passed over. Any other word ends a row, and so does anything between two
    letters but a blank, a comma, a hyphen or a period. A letter that belongs
    to a card number or hangs on an apostrophe is none of a name: the a of
    "23a" and of "die 23 a, M E I E R", the s of "ich bin's" (Codex PR #244,
    P2 twice, and the reviews between them). One letter alone spells nothing.
    ponytail: a single letter that is a word of its own right before the row
    is joined to it ("Menü B. M E I E R" spells "bmeier"), and a name that
    begins with a to g, spelled right behind digits, loses that letter. Both
    end in a question for the guest."""
    spelled: set[str] = set()
    row: list[str] = []
    after_wie = False
    end = 0
    for match in _WORD.finditer(sentence):
        word, start, stop = match.group(), match.start(), match.end()
        gap = sentence[end:start]
        clean_gap = all(char in _BETWEEN_LETTERS for char in gap)
        end = stop
        if after_wie and clean_gap:
            after_wie = False
            continue
        after_wie = False
        if not clean_gap:
            row = _close(row, spelled)
        # The letter of a card number, at its digits or a blank apart: a
        # suffix the card knows behind them ("23a", "23 a"), any letter that
        # opens a row before them ("S 12"). And the s of "ich bin's". A
        # spelled name next to digits keeps its letters: "für 4 M E I E R",
        # "M E I E R 19 Uhr".
        hangs = (
            (gap.rstrip()[-1:].isdigit() and word in CARD_SUFFIXES)
            or (not row and sentence[stop : stop + 3].lstrip()[:1].isdigit())
            or sentence[start - 1 : start] in _APOSTROPHES
        )
        if word == "wie" and row:
            after_wie = True
        elif len(word) == 1 and not hangs:
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
    A name without a letter is none. Blanks around the name do not count, the
    tools drop them; a line break or two blanks inside it do."""
    if not isinstance(name, str):
        return False
    written = name.strip()
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
    """The phone number of what a guest said in one turn, in E.164: the last
    digits in it, when they are a number. At most one.

    Only the last, because digits that are followed by more digits may be a
    number that goes on: "0171 234 äh 5678", "0171 234. 5678" and "0721 555
    und dann 1234" begin with a valid number that has digits missing, and
    "0171 2345678 - 19 Uhr" would be a number nobody said if read as one
    (Codex PR #244, P1 twice, and the review after it). And only when no
    other digits of the turn may be its head (`_may_be_a_head`). A count, a
    date or a dish before the number does no harm: "für 4 am 16.09., meine
    Nummer ist 0171 2345678". A guest whose number stands next to other such
    digits is asked again and says it alone."""
    groups = [
        piece.strip(_AROUND_A_NUMBER)
        for run in _DIGIT_RUN.findall(sentence)
        for piece in _NUMBER_END.split(_without_trunk_zero(run))
        if _has_digit(piece)
    ]
    if not groups or any(_may_be_a_head(group) for group in groups[:-1]):
        return set()
    number = e164(groups[-1])
    return {number} if number else set()


def _without_trunk_zero(run: str) -> str:
    """ "+43 (0)664 1234567" is one number. Read at the bracket as two, its
    tail became a German one. As `normalize_phone` reads the "(0)"."""
    international = run.startswith(("+", "00"))
    return TRUNK_IN_BRACKETS.sub("" if international else "0", run)


def _may_be_a_head(group: str) -> bool:
    """Other digits of the turn that may be the first part of the number: they
    begin like one, or are too many for a count, a time or a dish. Then the
    last digits may be only its tail: "0171 20, 034567", "0043, 0664 1234567"
    and "0171 20 äh 034567" end with a valid German number nobody said
    (review of 31a3a81). A date is no head."""
    text = group.lstrip("( ")
    if _DATE_LIKE.match(text) or _is_date(text):
        return False
    digits = sum(char.isdigit() for char in text)
    return digits >= _HEAD_DIGITS or (digits > 1 and text.startswith(("0", "+")))


def _is_date(text: str) -> bool:
    """ "01 10 2026": day, month and year with blanks between them (Codex
    PR #244, P1). "07 21 5551" is none, there is no month 21."""
    date = _SPACED_DATE.fullmatch(text)
    return bool(date) and 1 <= int(date[1]) <= 31 and 1 <= int(date[2]) <= 12


def _has_digit(text: str) -> bool:
    return any(char.isdigit() for char in text)


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
    if not text.startswith(("0", "+")) or _DATE_LIKE.match(text) or _is_date(text):
        return ""
    try:
        return normalize_phone(written)
    except InvalidInput:
        return ""
