"""Telefonnummern nach E.164 normalisieren (CLAUDE.md §8), deutsche Schreibweisen zuerst."""

import re

from api.core.errors import InvalidInput

DEFAULT_COUNTRY_CODE = "49"
E164 = re.compile(r"^\+[1-9][0-9]{6,14}$")  # ASCII: `\d` takes any script's digits
TRUNK_IN_BRACKETS = re.compile(r"\(\s*0\s*\)")
# Country codes after which a said 0 is the national trunk prefix and is dropped
# ("+33 06 12 34 56 78" is +33612345678; #683, review #207). Each country's trunk
# prefix is a single 0 that the international form omits, and none of its
# national numbers begins with a single 0 (Bulgaria's 00800 keeps both zeros by
# the guard in `normalize_phone`), so dropping the zero never turns a number that
# exists into another. Verified 15.11.2026 against the per-country
# `national_prefix` and national number pattern in libphonenumber's metadata
# (release 9.0.43, kept from the ITU-T E.164 national plans), Lithuania also
# against its regulator RRT and LRT (0 replaced 8 in 2024 and 2025). Poland has no
# trunk prefix since its closed plan of 2009, but its 0 was one: the numbers kept
# their digits without it, so a guest's old habit "+48 0 600 ..." means +48600...
# Left out on purpose, so a 0 there stays as said: Italy, San Marino, Vatican,
# Cote d'Ivoire, Benin (their numbers begin with 0, `ZERO_LEADING`); Hungary
# (trunk prefix 06, `OTHER_TRUNK`);
# Russia, Kazakhstan, Belarus (8); Norway (numbers with a 0); the countries
# without a trunk prefix (Spain, Portugal, Greece, Czechia, Denmark, Luxembourg,
# Latvia, Estonia, Malta, Cyprus, Iceland); every code not checked yet.
TRUNK_ZERO_AFTER = frozenset(
    {
        "31",  # Netherlands
        "32",  # Belgium
        "33",  # France
        "40",  # Romania
        "41",  # Switzerland
        "43",  # Austria
        "44",  # United Kingdom
        "46",  # Sweden
        "48",  # Poland (former trunk prefix, see above)
        "49",  # Germany
        "90",  # Turkey
        "353",  # Ireland
        "355",  # Albania
        "358",  # Finland
        "359",  # Bulgaria
        "370",  # Lithuania
        "373",  # Moldova
        "380",  # Ukraine
        "381",  # Serbia
        "382",  # Montenegro
        "383",  # Kosovo
        "385",  # Croatia
        "386",  # Slovenia
        "387",  # Bosnia and Herzegovina
        "389",  # North Macedonia
        "421",  # Slovakia
        "423",  # Liechtenstein
    }
)
# A trunk prefix other than 0, dropped after its country code like the zero (#804).
# Hungary: 06, and no national number begins with 0. Lithuania: 8 until 2024; a
# national number that begins with 8 continues with 0 (800, 809), which the guard
# in `normalize_phone` leaves alone. Russia's 8 stays: 812 is St Petersburg.
OTHER_TRUNK = {"36": ("06",), "370": ("8",)}
# Codes whose national numbers begin with 0 (Italy, San Marino, Vatican, Cote
# d'Ivoire, Benin): a bracketed "(0)" behind one of them is part of the number.
ZERO_LEADING = frozenset({"39", "378", "379", "225", "229"})
# Italian mobiles: 3, then 9 digits (libphonenumber 9.0.43: 3[2-9] and 31, ten
# digits; the nine-digit ones clash with area codes like Varese's 0332 and are not
# read as mobiles). They have no leading 0, so a "(0)" before one is the trunk
# digit and falls away. Brescia's 030 begins with 3 as well, and no mobile begins
# with 30. Left as said: Varese-like area codes with a seven-digit number, ten
# digits that are a landline, told by length only. San Marino (mobile 6x), Cote
# d'Ivoire (landline 2x) and Benin (all 01) have the same shape, with no caller
# for it yet: not built (#849).
DIGITS_BEHIND = re.compile(r"[0-9\s\-/.]*")  # the number that goes on after a "(0)"
ITALIAN_MOBILE = re.compile(r"3[2-9][0-9]{8}|31[0-9]{8}")
# Codes whose national numbers all have this many digits, without the trunk zero.
# A number said in pairs with pauses is complete at that length (`_is_complete` in
# `agent/guest_details.py`); a code not listed keeps the count of a German number,
# since no digit count fits every country (a short number must not take what
# follows it). France: 9 digits, "+33 6 12 34 56 78". #804.
FIXED_LENGTH = {"33": 9}


def wrong_length(number: str) -> bool:
    """Has the E.164 number of a `FIXED_LENGTH` country another count of digits
    behind its code than all its numbers have? Then nobody said it."""
    for size in (1, 2, 3):
        if length := FIXED_LENGTH.get(number[1 : 1 + size]):
            return len(number) - 1 - size != length
    return False


SAY_INVALID_PHONE = (
    "Die Rufnummer habe ich nicht verstanden. Können Sie sie noch einmal sagen?"
)


def read_bracketed_zero(text: str) -> str:
    """The text with each "(0)" read as the number reads it. "(0)" is the national
    trunk digit: behind a country code it falls away ("+49 (0)7221 ..." has one
    digit too many with it), except behind a code in `ZERO_LEADING`, where it is
    the number's own 0 ("+39 (0)2 ..." as "+39 02 ...", #804). Without a country
    code it is the leading zero of the national form."""
    plus = text.startswith("+")
    if not plus and not text.startswith("00"):
        return TRUNK_IN_BRACKETS.sub("0", text)

    def read(found: re.Match[str]) -> str:
        said = re.sub(r"[^0-9]", "", text[: found.start()])
        code = said if plus else said[2:]
        if code not in ZERO_LEADING:
            return ""
        behind = re.sub(r"[^0-9]", "", DIGITS_BEHIND.match(text, found.end())[0])  # type: ignore[index]
        return "" if code == "39" and ITALIAN_MOBILE.fullmatch(behind) else "0"

    return TRUNK_IN_BRACKETS.sub(read, text)


def normalize_phone(raw: str, country_code: str = DEFAULT_COUNTRY_CODE) -> str:
    """„0721 / 555-1234" → „+497215551234". Unbrauchbare Eingabe → InvalidInput."""
    digits = re.sub(r"[\s\-/().]", "", read_bracketed_zero(raw.strip()))
    if digits.startswith("00"):
        digits = "+" + digits[2:]
    elif digits.startswith("0"):
        digits = f"+{country_code}{digits[1:]}"
    elif digits and not digits.startswith("+"):
        digits = f"+{digits}"
    # "+49 0171 ...": the trunk zero said after the country code. Where it is
    # one, it is dropped (#683); Italy keeps its 0. Two zeros stay: the
    # placeholder +49000000000 and Bulgaria's 00800. Country codes are prefix
    # free, so at most one length names a code.
    for size in (1, 2, 3):
        code, rest = digits[1 : 1 + size], digits[1 + size :]
        trunks: tuple[str, ...] = ("0",) if code in TRUNK_ZERO_AFTER else ()
        trunks += OTHER_TRUNK.get(code, ())
        if trunks:
            for trunk in trunks:
                after = rest[len(trunk) : len(trunk) + 1]
                if rest.startswith(trunk) and after not in ("", "0"):
                    digits = f"+{code}{rest[len(trunk) :]}"
                    break
            break
    if not E164.match(digits):
        raise InvalidInput(f"Rufnummer ungültig: {raw!r}", say=SAY_INVALID_PHONE)
    return digits


def number_or_none(raw: str | None) -> str | None:
    """E.164, or None: withheld, or no number. The message of a refused number
    holds the number, so it goes nowhere."""
    if not raw:
        return None
    try:
        return normalize_phone(raw)
    except InvalidInput:
        return None


# Numbers that cost the caller more than a call: German premium and service
# ranges, international shared-cost and satellite codes (#691). Allowed as a
# callback number (Maxi decided B), but never dialled with one tap.
COSTLY_PREFIXES = (
    "+49900",
    "+49137",
    "+49180",
    "+49118",
    "+4912",
    "+870",
    "+881",
    "+882",
    "+883",
)


def costly(number: str | None) -> bool:
    """Is the E.164 number in a premium-rate, service or satellite range?"""
    return bool(number) and str(number).startswith(COSTLY_PREFIXES)
