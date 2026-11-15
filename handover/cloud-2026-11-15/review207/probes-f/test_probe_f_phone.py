"""Probe (review #207 area F): numbers normalize_phone turns into a number
that does not exist, after #726."""

import pytest

from api.core.errors import InvalidInput
from api.domain.customers.phone import normalize_phone


@pytest.mark.parametrize(
    ("said", "number"),
    [
        ("+33 06 12 34 56 78", "+33612345678"),  # France, Strasbourg is 60 km away
        ("0033 06 12 34 56 78", "+33612345678"),
        ("+44 07700 900123", "+447700900123"),  # UK
        ("+31 06 12345678", "+31612345678"),  # Netherlands
        ("+48 0 600 123 456", "+48600123456"),  # Poland has no trunk 0 at all
    ],
)
def test_trunk_zero_of_other_countries(said, number):
    assert normalize_phone(said) == number


@pytest.mark.parametrize("typed", ["5551234", "7221 5551234", "171 2345678"])
def test_a_number_without_zero_or_plus_is_no_foreign_number(typed):
    """The tablet's reservation form (manual.py:86) feeds normalize_phone
    directly: a local number without area code becomes +55..., one without
    the area code's zero +7..., a mobile without its zero +1 712..."""
    with pytest.raises(InvalidInput):
        normalize_phone(typed)
