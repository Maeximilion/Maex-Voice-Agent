"""Telefonnummern nach E.164 normalisieren (CLAUDE.md §8), deutsche Schreibweisen zuerst."""

import re

from api.core.errors import InvalidInput

DEFAULT_COUNTRY_CODE = "49"
E164 = re.compile(r"^\+[1-9]\d{6,14}$")
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
# Cote d'Ivoire, Benin (their numbers begin with 0); Hungary (trunk prefix 06);
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
SAY_INVALID_PHONE = (
    "Die Rufnummer habe ich nicht verstanden. Können Sie sie noch einmal sagen?"
)


def normalize_phone(raw: str, country_code: str = DEFAULT_COUNTRY_CODE) -> str:
    """„0721 / 555-1234" → „+497215551234". Unbrauchbare Eingabe → InvalidInput."""
    text = raw.strip()
    # "(0)" ist die nationale Verkehrsausscheidungsziffer. Steht eine Landesvorwahl
    # davor, entfaellt sie; sonst ist sie die fuehrende Null der nationalen Form.
    # Ohne diese Unterscheidung wird aus "+49 (0)7221 5551234" eine Nummer mit einer
    # Ziffer zu viel, und das Team ruft ins Leere.
    international = text.startswith("+") or text.startswith("00")
    text = TRUNK_IN_BRACKETS.sub("" if international else "0", text)
    digits = re.sub(r"[\s\-/().]", "", text)
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
        if digits.startswith("+") and code in TRUNK_ZERO_AFTER:
            if rest[:1] == "0" and rest[1:2] not in ("", "0"):
                digits = f"+{code}{rest[1:]}"
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
