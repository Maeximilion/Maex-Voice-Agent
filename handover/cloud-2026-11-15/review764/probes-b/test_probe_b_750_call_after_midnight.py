"""Probe (review #764, area B, PR #750): the night tail is applied to the
table's hour, not to the call's own hour. A draft created at 00:15 reads a
table for the coming evening as "heute" and the next evening as "morgen",
which a caller still in "tonight" hears a day early (the mirror of #742)."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from api.domain.reservations.create import readback
from api.models import Reservation

BERLIN = ZoneInfo("Europe/Berlin")
# Wednesday 16.09.2026, 00:15 local: the restaurant's evening runs until 01:00.
CALLED = datetime(2026, 9, 16, 0, 15, tzinfo=BERLIN)


@pytest.mark.parametrize(
    ("table", "ambiguous"),
    [
        # Wednesday 19:00, read as "heute": at 00:15 "heute" is still Tuesday's.
        (datetime(2026, 9, 16, 19, 0, tzinfo=BERLIN), "heute"),
        # Thursday 19:00, read as "morgen": at 00:15 "morgen" means Wednesday to
        # most callers; a caller who said "morgen" for Wednesday hears his own
        # word back and the yes books Thursday, 24 hours late.
        (datetime(2026, 9, 17, 19, 0, tzinfo=BERLIN), "morgen"),
    ],
)
def test_a_call_after_midnight_names_the_day_by_weekday(table, ambiguous):
    r = Reservation(
        reserved_for=table,
        created_at=CALLED,
        party_size=2,
        guest_name="Müller",
        note=None,
    )
    said = readback(r, BERLIN)
    assert f" {ambiguous} " not in said, said
