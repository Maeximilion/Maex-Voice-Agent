"""Telefonnummern nach E.164 normalisieren (CLAUDE.md §8), deutsche Schreibweisen zuerst."""

import re

from api.core.errors import InvalidInput

DEFAULT_COUNTRY_CODE = "49"
E164 = re.compile(r"^\+[1-9]\d{6,14}$")
TRUNK_IN_BRACKETS = re.compile(r"\(\s*0\s*\)")
# Countries whose national numbers never start with 0 after the country code.
TRUNK_ZERO_AFTER = ("49", "43", "41")
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
    # "+49 0171 ...": the trunk zero said after the country code. Here no
    # number starts with 0, so it is dropped (#683); Italy keeps its 0.
    for code in TRUNK_ZERO_AFTER:
        rest = digits[len(code) + 1 :]
        if digits.startswith(f"+{code}") and rest[:1] == "0" and rest[1:2] not in "0":
            digits = f"+{code}{rest[1:]}"
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
