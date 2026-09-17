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
