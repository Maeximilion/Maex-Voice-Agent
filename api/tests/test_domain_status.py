"""domain/status: offen, geschlossen, Mitternacht, Sondertag schlägt Wochentag, Sommerzeit."""

import uuid
from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from api.core.errors import NotFound
from api.domain.status import get_service_status
from api.models import OpeningHours, ServiceConfig, SpecialDay
from scripts.seed import seed

BERLIN = ZoneInfo("Europe/Berlin")

# Seed: Montag Ruhetag, sonst 11:30-14:00 und 17:00-22:00 (siehe scripts/seed.py).
DIENSTAG = date(2026, 9, 15)
MONTAG = date(2026, 9, 14)


def berlin(day: date, hh: int, mm: int = 0) -> datetime:
    return datetime.combine(day, time(hh, mm), tzinfo=BERLIN)


@pytest.fixture
def session(migrated_db_url):
    engine = create_engine(migrated_db_url)
    with Session(engine) as s:
        yield s
    engine.dispose()


@pytest.fixture
def tenant_id(session) -> uuid.UUID:
    return uuid.UUID(
        seed(session, tenant_name="Testbetrieb", timezone="Europe/Berlin").tenant_id
    )


def test_dienstag_abend_ist_offen(session, tenant_id):
    status = get_service_status(session, tenant_id, now=berlin(DIENSTAG, 18, 30))
    assert status.is_open is True
    assert status.closes_at == berlin(DIENSTAG, 22).astimezone(UTC)
    assert status.pickup_enabled is True and status.delivery_enabled is True
    assert (status.pickup_wait_minutes, status.delivery_wait_minutes) == (20, 45)
    assert status.call_mode == "shadow"
    assert status.sold_out == [] and status.say is None


def test_montag_ist_ruhetag_und_say_nennt_die_naechste_oeffnung(session, tenant_id):
    status = get_service_status(session, tenant_id, now=berlin(MONTAG, 12))
    assert status.is_open is False and status.closes_at is None
    assert status.pickup_enabled is False and status.delivery_enabled is False
    assert (
        status.say
        == "Wir haben gerade geschlossen. Wir öffnen wieder morgen um 11:30 Uhr."
    )


def test_zwischen_den_fenstern_sagt_heute(session, tenant_id):
    status = get_service_status(session, tenant_id, now=berlin(DIENSTAG, 15))
    assert status.is_open is False
    assert (
        status.say == "Wir haben gerade geschlossen. Wir öffnen wieder heute um 17 Uhr."
    )


def test_kurz_nach_mitternacht_ist_geschlossen_und_say_nennt_heute(session, tenant_id):
    status = get_service_status(session, tenant_id, now=berlin(DIENSTAG, 0, 30))
    assert status.is_open is False
    assert (
        status.say
        == "Wir haben gerade geschlossen. Wir öffnen wieder heute um 11:30 Uhr."
    )


def test_fenster_ueber_mitternacht_gilt_am_folgetag(session, tenant_id):
    session.add(
        OpeningHours(
            tenant_id=tenant_id,
            weekday=0,
            opens_at=time(18),
            closes_at=time(1),
            service="pickup",
        )
    )
    session.commit()
    status = get_service_status(session, tenant_id, now=berlin(DIENSTAG, 0, 30))
    assert status.is_open is True
    assert status.pickup_enabled is True and status.delivery_enabled is False
    assert status.closes_at == berlin(DIENSTAG, 1).astimezone(UTC)


def test_sondertag_geschlossen_schlaegt_wochentag(session, tenant_id):
    session.add(
        SpecialDay(
            tenant_id=tenant_id, date=DIENSTAG, closed=True, note="Betriebsausflug"
        )
    )
    session.commit()
    status = get_service_status(session, tenant_id, now=berlin(DIENSTAG, 18, 30))
    assert status.is_open is False
    assert (
        status.say
        == "Wir haben gerade geschlossen. Wir öffnen wieder morgen um 11:30 Uhr."
    )


def test_sondertag_mit_sonderzeiten_oeffnet_den_ruhetag(session, tenant_id):
    session.add(
        SpecialDay(
            tenant_id=tenant_id,
            date=MONTAG,
            closed=False,
            opens_at=time(12),
            closes_at=time(15),
        )
    )
    session.commit()
    status = get_service_status(session, tenant_id, now=berlin(MONTAG, 13))
    assert status.is_open is True
    assert status.closes_at == berlin(MONTAG, 15).astimezone(UTC)
    assert (
        get_service_status(session, tenant_id, now=berlin(MONTAG, 16)).is_open is False
    )


@pytest.mark.parametrize(
    ("day", "utc_offset_hours"),
    [
        (date(2026, 3, 29), 2),
        (date(2026, 10, 25), 1),
    ],  # Sommerzeit beginnt / endet, beide Sonntage
)
def test_sommerzeit_umstellung_rechnet_schliesszeit_in_utc_richtig(
    session, tenant_id, day, utc_offset_hours
):
    status = get_service_status(session, tenant_id, now=berlin(day, 12))
    assert status.is_open is True
    assert status.closes_at == datetime(
        day.year, day.month, day.day, 14 - utc_offset_hours, tzinfo=UTC
    )


def test_lieferung_pausiert_bleibt_offen_mit_hinweis(session, tenant_id):
    session.get(ServiceConfig, tenant_id).delivery_enabled = False
    session.commit()
    status = get_service_status(session, tenant_id, now=berlin(DIENSTAG, 18, 30))
    assert status.is_open is True and status.pickup_enabled is True
    assert status.delivery_enabled is False
    assert status.say == "Lieferung ist gerade pausiert. Abholung ist möglich."


def test_unbekannter_mandant_ist_not_found(session, tenant_id):
    with pytest.raises(NotFound):
        get_service_status(session, uuid.uuid4(), now=berlin(DIENSTAG, 18))
