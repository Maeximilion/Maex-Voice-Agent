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

- a name, when each of its words was said as a whole word in this call, as
  often as the name holds it, or was spelled letter by letter in one sentence
  (docs/05 §2, rung 3), and it holds nothing but those words;
- a number, when it is the caller ID or a phone number the guest gave: the
  last digits of a turn that names them as a number to call
  (`names_a_phone_number`), or that answers the agent's question for one.
  They begin with an area code or a country code and are compared in E.164.
  A date, a time or a number without an area code is no phone number,
  whatever a model makes of its digits, and digits next to other digits that
  may belong to them are none either.

The number is not left to the model at all: the one the guest gave goes into
`slots` by the core's own hand (`state.note_guest_details`), and a tool books
under the number that stands there (`guards._phone_counts`). Which of the
words a guest said is the name stays with the model, and the readback follows
it. Strict on purpose: a name or number that is missed costs the guest one
more question, one that is wrongly taken books under a stranger.

The sentences behind each rule are the cases of
`api/tests/test_agent_guest_details.py`.

ponytail: words are not understanding. Not heard: a number said in words
("null eins sieben ..."), a number without its area code or with a country
code and no plus, a number named without a word for it before the agent has
asked, a number that stands next to other digits that may belong to it, one
with a dash or a slash between blanks ("0721 / 5551234") or named in two
turns, an extension of one or two digits behind a hyphen ("0721 5551234-0"),
letter names when spelling ("Doppel-L", "Ypsilon"), a name written with
another accent than it was said with, and a spelling the agent proposed and
the guest only agreed to ("Maier mit a-i?" - "Ja"). Each ends in a question
for the guest, never in a booking.
ponytail: blanks alone do not tell where a number ends. "0171 2345678 19
Uhr" is read as one number of thirteen digits and stands in `slots` as that;
numbers are said in groups, so a blank cannot end one. And a number the
guest names for another reason with a word for it ("meine alte Nummer war
...") is taken for the one to call. Reading the number back would close both
(docs/01_STATUS.md, Open Points). The ear of the voice layer decides how
numbers and letters arrive as text (T-10.5); read them here once it shows
that.
ponytail: a name is known from guest sentences only. Once a known caller's
name comes from the database (`find_customer`, stage 3), that name has to
count as well, the way the caller ID does for the number.
"""

import re
import unicodedata
from collections import Counter
from collections.abc import Mapping

from api.core.errors import InvalidInput
from api.domain.customers.phone import TRUNK_IN_BRACKETS, normalize_phone
from api.domain.menu.numberwords import CARD_SUFFIXES, fold

# Letters only: digits and punctuation belong to no name.
_WORD = re.compile(r"[^\W\d_]+")
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
# This many digits behind a hyphen, a slash or a closing bracket, after what
# is a complete number already, are an hour or a count as likely as an
# extension.
_SHORT_TAIL = 2
# A single letter right behind one of these belongs to the word before it.
_APOSTROPHES = ("'", "\N{RIGHT SINGLE QUOTATION MARK}")
# What may stand between two words of a name, and nowhere else in it. The
# apostrophe in either form: a guest named O'Brien is refused on every try
# when the text arrives with the typographic one (Codex PR #244, P2).
_IN_A_NAME = frozenset({" ", "-", ", ", ". ", *_APOSTROPHES})
# A date or a time begins with one or two digits and a separator. No phone
# number does: an area code has three digits or more, the zero included.
_DATE_LIKE = re.compile(r"\d{1,2}[./-]")
# The same date written with blanks: day, month, year. Told from a number in
# three groups by the day and the month being one.
_SPACED_DATE = re.compile(r"(\d{1,2})\s+(\d{1,2})\s+(\d{2}|\d{4})")
# Words that say the digits of a turn are a number to call, folded. "Nummer"
# only as a word of its own or with what kind of line it is: the number of an
# order, an invoice or a house is none. No word for calling as such: "ich
# rufe an wegen meiner Bestellung 0171 ..." names no number to call.
_PHONE_WORDS = frozenset(
    {
        "nummer",
        "rufnummer",
        "festnetznummer",
        "kontaktnummer",
        "tel",
        "festnetz",
        "vorwahl",
        "durchwahl",
    }
)
_PHONE_STEMS = (
    "telefon",
    "handy",
    "mobil",
    "erreich",
    "rueckruf",
    "zurueckruf",
)


def _nfc(text: str) -> str:
    """One form for the same letter: an "ü" may arrive as u with a mark of its
    own, which is no letter and would cut the word in two (Codex PR #244,
    P2)."""
    return unicodedata.normalize("NFC", text)


def words_said(sentence: str) -> Counter[str]:
    """The words of a guest sentence, folded, each with how often it stands
    there, and what the guest spelled in it."""
    lowered = _nfc(sentence).lower()
    said = Counter(fold(word) for word in _WORD.findall(lowered))
    said.update(_spelled(lowered))
    return said


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


def clean_name(name: str) -> str:
    """The name as it is checked and kept: one form for its letters, without
    the blanks around it, which the tools drop as well."""
    return _nfc(name).strip()


def name_said(name: object, words: Mapping[str, int]) -> bool:
    """Every word of the name is one the guest said, as often as the name
    holds it, and the name is nothing but those words with one separator
    between two of them. A digit, a bracket, a row of dashes, a period at the
    end or a word said once and written twice was not said, and the tool
    would store it (Codex PR #244, P2 five times). A period stands only
    behind a word that another follows ("Dr. Müller"). A name without a
    letter is none; a line break or two blanks inside it are no separator."""
    if not isinstance(name, str):
        return False
    written = clean_name(name)
    parts = Counter(fold(word) for word in _WORD.findall(written.lower()))
    if not parts or any(words.get(part, 0) < count for part, count in parts.items()):
        return False
    before, *between, after = _WORD.split(written)
    return before == "" and after == "" and all(gap in _IN_A_NAME for gap in between)


def names_a_phone_number(sentence: str) -> bool:
    """Does the sentence say that its digits are a number to call? By a word
    for it ("meine Nummer ist", "erreichbar unter", "Handy"), or by "unter"
    right before the digits ("bitte unter 0171 2345678").

    Without that, digits that look like a phone number may be anything: in
    "zu meiner alten Bestellung 0171 2345678 habe ich eine Frage" they are
    no number the team should call back (Codex PR #244, P1). The same check
    reads the agent's own sentence: after a question for the number the
    guest's answer needs no word for it (`state.note_guest_details`)."""
    lowered = _nfc(sentence).lower()
    words = {fold(word) for word in _WORD.findall(lowered)}
    if words & _PHONE_WORDS or any(word.startswith(_PHONE_STEMS) for word in words):
        return True
    runs = list(_DIGIT_RUN.finditer(lowered))
    if not runs:
        return False
    before = lowered[: runs[-1].start()].rstrip(" \t:(")
    return before.endswith("unter") and not before[-6:-5].isalpha()


def numbers_said(sentence: str) -> set[str]:
    """The phone number of what a guest said in one turn, in E.164: the last
    digits in it, when they are a number. At most one. Whether the turn names
    them as a number to call is another question (`names_a_phone_number`).

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
    if _ends_with_other_digits(groups[-1]):
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
    (review of 31a3a81). A date is no head, with its separators or with
    blanks and a full year ("am 01 10 2026, meine Nummer ist ..."; Codex
    PR #244, P2), and neither is an hour: "um 09:30 Uhr, 0171 2345678".
    Digits in pairs with blanks are one, they may be a number said that way
    ("02 11 55, 01 23 45"; review of 8835fa3).
    ponytail: a head is told by how it begins. Two to five digits that begin
    with 1 to 9 are none ("49 171, 0345678"), nor is a head in an earlier
    turn; both leave a tail that begins with 0 standing as a number."""
    text = group.lstrip("( ")
    if _DATE_LIKE.match(text) or _is_date(text, full_year=True):
        return False
    digits = sum(char.isdigit() for char in text)
    if digits >= _HEAD_DIGITS or text.startswith("+"):
        return True
    # An area code has three digits or more, the zero included.
    return digits > 2 and text.startswith("0")


def _ends_with_other_digits(group: str) -> bool:
    """ "0171 2345678-19 Uhr passt": behind a hyphen, a slash or a closing
    bracket stand one or two digits, and what stands before them is a
    complete number already. An extension or an hour, the text does not say
    which (Codex PR #244, P1). "0171/234-5678" and "(0171) 2345678" are no
    such case: there the digits behind the mark are the number itself."""
    cut = max(group.rfind(mark) for mark in "-/)")
    if cut < 0:
        return False
    tail = group[cut + 1 :].strip()
    return tail.isdigit() and len(tail) <= _SHORT_TAIL and bool(e164(group[:cut]))


def _is_date(text: str, *, full_year: bool = False) -> bool:
    """ "01 10 2026": day, month and year with blanks between them (Codex
    PR #244, P1). "07 21 5551" is none, there is no month 21. `full_year`
    asks for a year of this or the last century: "02 11 55" may be the half
    of a number said in pairs."""
    date = _SPACED_DATE.fullmatch(text)
    if not date or not (1 <= int(date[1]) <= 31 and 1 <= int(date[2]) <= 12):
        return False
    return not full_year or (date[3].startswith(("19", "20")) and len(date[3]) == 4)


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
