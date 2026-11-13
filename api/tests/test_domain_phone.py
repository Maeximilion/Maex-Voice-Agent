"""domain/customers/phone: E.164-Normalisierung deutscher Schreibweisen."""

import pytest

from api.core.errors import InvalidInput
from api.domain.customers import normalize_phone


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("+4972215551234", "+4972215551234"),
        ("07221 555 1234", "+4972215551234"),
        ("0722 1/555-1234", "+4972215551234"),
        ("0049 7221 5551234", "+4972215551234"),
        ("(0)7221 5551234", "+4972215551234"),
        ("+41 44 123 45 67", "+41441234567"),
        ("0176 12345678", "+4917612345678"),
        ("4972215551234", "+4972215551234"),
        # Visitenkarten-Schreibweise: die (0) ist die nationale Verkehrsausscheidungs-
        # ziffer und entfaellt international, sonst waehlt das Team eine Ziffer zu viel.
        ("+49 (0)7221 5551234", "+4972215551234"),
        ("0049 (0)7221 5551234", "+4972215551234"),
        ("+41 (0)44 123 45 67", "+41441234567"),
    ],
)
def test_normalisiert(raw: str, expected: str):
    assert normalize_phone(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "12", "+0123456789", "abc", "+49 12a 34"])
def test_ungueltig(raw: str):
    with pytest.raises(InvalidInput) as exc:
        normalize_phone(raw)
    assert exc.value.say is not None


def test_anderes_land_als_default():
    assert normalize_phone("044 123 45 67", country_code="41") == "+41441234567"


# --- #683: the trunk zero after the country code -----------------------------


@pytest.mark.parametrize(
    ("said", "number"),
    [
        ("+49 0171 2345678", "+491712345678"),
        ("0049 07221 5551234", "+4972215551234"),
        ("+4907221 5551234", "+4972215551234"),
        ("+43 0664 1234567", "+436641234567"),
        ("+41 079 1234567", "+41791234567"),
    ],
)
def test_the_trunk_zero_after_the_country_code_is_dropped(said, number):
    """#683: "+49 0171 ..." kept the zero, and the team called a number that
    does not exist."""
    assert normalize_phone(said) == number


@pytest.mark.parametrize(
    ("said", "number"),
    [("+39 06 1234 5678", "+390612345678"), ("0039 02 12345678", "+390212345678")],
)
def test_an_italian_number_keeps_its_zero(said, number):
    assert normalize_phone(said) == number
