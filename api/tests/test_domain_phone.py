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


# --- review #207: the trunk zero of the other countries ----------------------


@pytest.mark.parametrize(
    ("said", "number"),
    [
        ("+33 06 12 34 56 78", "+33612345678"),  # France
        ("0033 06 12 34 56 78", "+33612345678"),
        ("+33 (0)6 12 34 56 78", "+33612345678"),
        ("+44 07700 900123", "+447700900123"),  # United Kingdom
        ("+31 06 12345678", "+31612345678"),  # Netherlands
        ("+32 0470 12 34 56", "+32470123456"),  # Belgium
        ("+48 0 600 123 456", "+48600123456"),  # Poland, its former trunk zero
        ("+353 087 1234567", "+353871234567"),  # Ireland, three-digit code
        ("+370 0612 34567", "+37061234567"),  # Lithuania, 0 since 2024
    ],
)
def test_the_trunk_zero_of_another_country_is_dropped(said: str, number: str):
    """Review #207: the fix of #683 dropped the zero after 49, 43 and 41 only.
    A French guest who said "+33 06 ..." got a callback number that does not
    exist, and the number the guest said beats the caller ID."""
    assert normalize_phone(said) == number


@pytest.mark.parametrize(
    ("said", "number"),
    [
        (
            "+225 07 12 34 56 78",
            "+2250712345678",
        ),  # Cote d'Ivoire, numbers begin with 0
        ("+229 01 23 45 67 89", "+2290123456789"),  # Benin, numbers begin with 01
        ("+36 06 30 123 4567", "+3606301234567"),  # Hungary, trunk prefix 06
        ("+7 8 912 345 67 89", "+789123456789"),  # Russia, trunk prefix 8
        ("+34 0612 345 678", "+340612345678"),  # Spain, no trunk prefix
        ("+47 01234", "+4701234"),  # Norway, a number that begins with 0
        ("+359 00800 1234567", "+359008001234567"),  # Bulgaria, two zeros stay
        ("+49000000000", "+49000000000"),  # the placeholder team number
    ],
)
def test_a_zero_that_is_no_trunk_prefix_stays(said: str, number: str):
    """A country whose numbers begin with 0, whose trunk prefix is another, or
    that has none keeps the digits as said: dropping a 0 there would change a
    number that may exist."""
    assert normalize_phone(said) == number


def test_a_caller_id_of_another_country_stays_as_it_came():
    """A foreign caller ID arrives without its trunk digit (docs/20)."""
    assert normalize_phone("+33612345678") == "+33612345678"
    assert normalize_phone("0033612345678") == "+33612345678"
    assert normalize_phone("+390612345678") == "+390612345678"


@pytest.mark.parametrize(
    ("number", "expected"),
    [
        ("+499001234567", True),
        ("+491371234567", True),
        ("+491801234567", True),
        ("+491181234", True),
        ("+4912345678", True),
        ("+8821234567", True),
        ("+491712345678", False),
        ("+4972215551234", False),
        ("+12685551234", False),
        (None, False),
    ],
)
def test_costly_numbers_are_the_premium_service_and_satellite_ranges(number, expected):
    """#691: such a callback number gets no one-tap link on the tablet."""
    from api.domain.customers.phone import costly

    assert costly(number) is expected
