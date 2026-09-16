"""Zeit-Helfer: Mitternacht, Betriebstag, Sommerzeit-Umstellung."""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from api.core import time as t

BERLIN = ZoneInfo("Europe/Berlin")


def test_to_local_und_to_utc_sind_umkehrbar():
    utc = datetime(2026, 9, 16, 18, 30, tzinfo=UTC)
    local = t.to_local(utc, "Europe/Berlin")
    assert local.hour == 20 and local.tzinfo == BERLIN
    assert t.to_utc(local) == utc


def test_naive_werte_gelten_als_ortszeit_beim_speichern():
    assert t.to_utc(datetime(2026, 9, 16, 20, 0), "Europe/Berlin") == datetime(
        2026, 9, 16, 18, 0, tzinfo=UTC
    )


def test_to_local_lehnt_naive_werte_ab():
    with pytest.raises(ValueError):
        t.to_local(datetime(2026, 9, 16, 20, 0))


def test_betriebstag_kurz_nach_mitternacht_ist_der_vortag():
    kurz_nach_mitternacht = datetime(2026, 9, 17, 0, 30, tzinfo=BERLIN)
    assert t.business_day(kurz_nach_mitternacht, "Europe/Berlin") == date(2026, 9, 16)


def test_betriebstag_beginnt_genau_um_fuenf():
    assert t.business_day(
        datetime(2026, 9, 17, 4, 59, tzinfo=BERLIN), "Europe/Berlin"
    ) == date(2026, 9, 16)
    assert t.business_day(
        datetime(2026, 9, 17, 5, 0, tzinfo=BERLIN), "Europe/Berlin"
    ) == date(2026, 9, 17)


def test_betriebstag_rechnet_in_ortszeit_nicht_utc():
    # 23:30 UTC ist 01:30 Ortszeit am 17.09. und gehört damit zum 16.09.
    assert t.business_day(
        datetime(2026, 9, 16, 23, 30, tzinfo=UTC), "Europe/Berlin"
    ) == date(2026, 9, 16)


def test_betriebstag_grenzen_bei_sommerzeit_beginn_sind_23_stunden():
    # 29.03.2026 02:00 wird 03:00. Das liegt vor 05:00, also verliert der
    # Betriebstag 28.03. (05:00 CET bis 05:00 CEST) die Stunde.
    start, end = t.business_day_bounds_utc(date(2026, 3, 28), "Europe/Berlin")
    assert start == datetime(2026, 3, 28, 4, 0, tzinfo=UTC)
    assert end == datetime(2026, 3, 29, 3, 0, tzinfo=UTC)
    assert end - start == timedelta(hours=23)


def test_betriebstag_grenzen_bei_sommerzeit_ende_sind_25_stunden():
    # 25.10.2026 03:00 wird 02:00. Die Stunde fällt in den Betriebstag 24.10.
    # (05:00 CEST bis 05:00 CET).
    start, end = t.business_day_bounds_utc(date(2026, 10, 24), "Europe/Berlin")
    assert start == datetime(2026, 10, 24, 3, 0, tzinfo=UTC)
    assert end == datetime(2026, 10, 25, 4, 0, tzinfo=UTC)
    assert end - start == timedelta(hours=25)


def test_betriebstag_anfang_ist_konfigurierbar():
    dt = datetime(2026, 9, 17, 3, 0, tzinfo=BERLIN)
    assert t.business_day(dt, "Europe/Berlin", day_starts_at=time(2, 0)) == date(
        2026, 9, 17
    )
