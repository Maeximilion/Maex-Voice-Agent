"""Telefonnummern nach E.164 normalisieren (CLAUDE.md §8), deutsche Schreibweisen zuerst."""

import re

from api.core.errors import InvalidInput

DEFAULT_COUNTRY_CODE = "49"
E164 = re.compile(r"^\+[1-9]\d{6,14}$")
SAY_INVALID_PHONE = (
    "Die Rufnummer habe ich nicht verstanden. Können Sie sie noch einmal sagen?"
)


def normalize_phone(raw: str, country_code: str = DEFAULT_COUNTRY_CODE) -> str:
    """„0721 / 555-1234" → „+497215551234". Unbrauchbare Eingabe → InvalidInput."""
    digits = re.sub(r"[\s\-/().]", "", raw.strip())
    if digits.startswith("00"):
        digits = "+" + digits[2:]
    elif digits.startswith("0"):
        digits = f"+{country_code}{digits[1:]}"
    elif digits and not digits.startswith("+"):
        digits = f"+{digits}"
    if not E164.match(digits):
        raise InvalidInput(f"Rufnummer ungültig: {raw!r}", say=SAY_INVALID_PHONE)
    return digits
