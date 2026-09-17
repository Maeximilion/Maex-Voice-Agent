"""domain/reservations/today: Spalte "Heute" - Auswahl, Reihenfolge, Betriebstag, Fingerabdruck."""

import uuid
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from api.domain.reservations import list_today, today_change_token
from api.models import Call, Reservation
from scripts.seed import seed

BERLIN = ZoneInfo("Europe/Berlin")
TZ = "Europe/Berlin"
DIENSTAG = date(2026, 9, 15)
NOW = datetime(2026, 9, 15, 18, 0, tzinfo=BERLIN)


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
    return uuid.UUID(seed(session, tenant_name="Testbetrieb", timezone=TZ).tenant_id)


@pytest.fixture
def call_id(session, tenant_id) -> uuid.UUID:
    call = Call(
        tenant_id=tenant_id,
        external_session_id="ext",
        started_at=NOW,
        delete_after=DIENSTAG,
    )
    session.add(call)
    session.commit()
    return call.id


def add(session, tenant_id, call_id, when, *, status="confirmed", name="Müller", **kw):
    row = Reservation(
        tenant_id=tenant_id,
        call_id=call_id,
        status=status,
        guest_name=name,
        phone="+4972215551234",
        party_size=kw.pop("party_size", 4),
        reserved_for=when,
        note=kw.pop("note", None),
        idempotency_key=uuid.uuid4().hex,
        **kw,
    )
    session.add(row)
    session.commit()
    return row


def test_bestaetigte_nach_uhrzeit(session, tenant_id, call_id):
    add(session, tenant_id, call_id, berlin(DIENSTAG, 20, 0), name="Spät")
    add(
        session,
        tenant_id,
        call_id,
        berlin(DIENSTAG, 18, 30),
        name="Früh",
        note="Fenster",
    )

    rows = list_today(session, tenant_id, TZ, now=NOW)

    assert [r.guest_name for r in rows] == ["Früh", "Spät"]
    assert rows[0].note == "Fenster" and rows[0].party_size == 4


def test_entwurf_storno_und_geloeschtes_bleiben_draussen(session, tenant_id, call_id):
    """Ein Entwurf ist keine Buchung (CLAUDE.md §2 Regel 3) und gehört nicht aufs Tablet."""
    add(session, tenant_id, call_id, berlin(DIENSTAG, 19, 0), status="draft")
    add(session, tenant_id, call_id, berlin(DIENSTAG, 19, 15), status="cancelled")
    weich = add(session, tenant_id, call_id, berlin(DIENSTAG, 19, 30), name="Weg")
    weich.deleted_at = NOW
    session.commit()

    assert list_today(session, tenant_id, TZ, now=NOW) == []


def test_betriebstag_statt_kalendertag(session, tenant_id, call_id):
    """00:30 gehört noch zum Abend davor, 06:00 schon zum nächsten Tag (core/time.py)."""
    add(
        session,
        tenant_id,
        call_id,
        berlin(DIENSTAG + timedelta(days=1), 0, 30),
        name="Nacht",
    )
    add(
        session,
        tenant_id,
        call_id,
        berlin(DIENSTAG + timedelta(days=1), 6, 0),
        name="Morgen",
    )
    add(session, tenant_id, call_id, berlin(DIENSTAG, 4, 0), name="Vortag")

    assert [r.guest_name for r in list_today(session, tenant_id, TZ, now=NOW)] == [
        "Nacht"
    ]


def test_fremder_mandant_bleibt_draussen(session, tenant_id, call_id):
    fremd = uuid.UUID(seed(session, tenant_name="Anderer", timezone=TZ).tenant_id)
    add(session, tenant_id, call_id, berlin(DIENSTAG, 19, 0), name="Unser")

    assert list_today(session, fremd, TZ, now=NOW) == []


def test_fingerabdruck_aendert_sich_nur_bei_aenderung(session, tenant_id, call_id):
    leer = today_change_token(session, tenant_id, TZ, now=NOW)
    assert today_change_token(session, tenant_id, TZ, now=NOW) == leer

    row = add(session, tenant_id, call_id, berlin(DIENSTAG, 19, 0))
    mit = today_change_token(session, tenant_id, TZ, now=NOW)
    assert mit != leer

    row.status = "cancelled"
    session.commit()
    assert today_change_token(session, tenant_id, TZ, now=NOW) == leer
