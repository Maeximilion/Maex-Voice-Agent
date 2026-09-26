"""domain/reservations: check_slot mit Kapazität, Öffnungszeiten, Alternativen und Grenzfällen."""

import uuid
from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from api.core.errors import InvalidInput, NotFound
from api.domain.reservations import check_slot
from api.domain.reservations.spoken import spoken_time
from api.models import Call, Capacity, OpeningHours, Reservation
from scripts.seed import seed

BERLIN = ZoneInfo("Europe/Berlin")
# Seed: Montag Ruhetag; Kapazität 11:30-14:00 mit 30 und 17:00-22:00 mit 40 Gästen, Raster 30 min.
DIENSTAG = date(2026, 9, 15)
MONTAG = date(2026, 9, 14)
NOW = datetime(2026, 9, 15, 8, 0, tzinfo=BERLIN)


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


@pytest.fixture
def book(session, tenant_id):
    call = Call(
        tenant_id=tenant_id,
        external_session_id="ext",
        started_at=NOW,
        delete_after=DIENSTAG,
    )
    session.add(call)
    session.flush()

    def _book(
        at: datetime, guests: int, status: str = "confirmed", deleted: bool = False
    ) -> None:
        session.add(
            Reservation(
                tenant_id=tenant_id,
                call_id=call.id,
                status=status,
                guest_name="Test",
                phone="+4900000000",
                party_size=guests,
                reserved_for=at,
                idempotency_key=uuid.uuid4().hex,
                deleted_at=NOW if deleted else None,
            )
        )
        session.commit()

    return _book


def test_freier_termin_ist_verfuegbar_ohne_say(session, tenant_id):
    result = check_slot(session, tenant_id, berlin(DIENSTAG, 18, 30), 4, now=NOW)
    assert result.available is True
    assert result.alternatives == [] and result.say is None


def test_volles_fenster_liefert_zwei_naechste_alternativen_im_raster(
    session, tenant_id, book
):
    # Abendfenster 17:00-22:00 hat 40 Plätze, 38 sind belegt. Mittags ist frei.
    book(berlin(DIENSTAG, 19, 0), 38)
    result = check_slot(session, tenant_id, berlin(DIENSTAG, 18, 30), 4, now=NOW)
    assert result.available is False
    assert result.alternatives == [
        berlin(DIENSTAG, 13, 30).astimezone(UTC),
        berlin(DIENSTAG, 13, 0).astimezone(UTC),
    ]
    assert result.say == "Um halb sieben ist leider voll. Halb zwei oder ein Uhr ginge."


def test_kleine_gruppe_passt_noch_in_fast_volles_fenster(session, tenant_id, book):
    book(berlin(DIENSTAG, 19, 0), 38)
    assert (
        check_slot(session, tenant_id, berlin(DIENSTAG, 20, 0), 2, now=NOW).available
        is True
    )
    assert (
        check_slot(session, tenant_id, berlin(DIENSTAG, 20, 0), 3, now=NOW).available
        is False
    )


def test_entwurf_zaehlt_storno_und_geloeschte_nicht(session, tenant_id, book):
    book(berlin(DIENSTAG, 18, 0), 38, status="draft")
    assert (
        check_slot(session, tenant_id, berlin(DIENSTAG, 18, 0), 4, now=NOW).available
        is False
    )
    book(berlin(DIENSTAG, 12, 0), 30, status="cancelled")
    book(berlin(DIENSTAG, 12, 30), 30, deleted=True)
    assert (
        check_slot(session, tenant_id, berlin(DIENSTAG, 12, 0), 30, now=NOW).available
        is True
    )


def test_gruppe_groesser_als_jedes_fenster_bekommt_keine_alternativen(
    session, tenant_id
):
    result = check_slot(session, tenant_id, berlin(DIENSTAG, 18, 0), 41, now=NOW)
    assert result.available is False and result.alternatives == []
    assert (
        result.say
        == "Um sechs Uhr ist leider nichts frei, und an dem Tag auch sonst nicht."
    )


def test_ruhetag_hat_keine_termine(session, tenant_id):
    result = check_slot(
        session, tenant_id, berlin(MONTAG, 18, 0), 2, now=berlin(MONTAG, 8)
    )
    assert result.available is False and result.alternatives == []


def test_ruhetag_bietet_keine_termine_vom_vortag_an(session, tenant_id):
    # Befund T-5.2 (reservierung_0027): Die Fenster des Vortags sind nur fuer die
    # Zeit nach Mitternacht geladen. Am Sonntagmorgen gefragt, lagen Sonntag 21:30
    # und 21:00 noch in der Zukunft und wurden fuer Montag als "halb zehn" angeboten.
    sonntag_frueh = berlin(date(2026, 9, 13), 8)
    result = check_slot(session, tenant_id, berlin(MONTAG, 19), 2, now=sonntag_frueh)
    assert result.available is False
    assert result.alternatives == []


def test_alternativen_liegen_am_tag_des_wunschs(session, tenant_id, book):
    # Dienstagabend voll: angeboten wird nur Dienstag, nie der Montag davor
    # (Ruhetag) oder ein anderer Tag, weil die Uhrzeit ohne Tag angesagt wird.
    for hh, mm in ((17, 0), (17, 30), (18, 0), (18, 30), (19, 0), (19, 30)):
        book(berlin(DIENSTAG, hh, mm), 40)
    result = check_slot(session, tenant_id, berlin(DIENSTAG, 19), 2, now=NOW)
    assert result.available is False
    assert result.alternatives
    assert all(a.astimezone(BERLIN).date() == DIENSTAG for a in result.alternatives)


def _nachtfenster(session, tenant_id, weekday: int) -> None:
    """18:00 bis 01:00 mit 20 Plaetzen am Wochentag `weekday` (0 = Montag)."""
    session.add_all(
        [
            OpeningHours(
                tenant_id=tenant_id,
                weekday=weekday,
                opens_at=time(18),
                closes_at=time(1),
                service="dinein",
            ),
            Capacity(
                tenant_id=tenant_id,
                weekday=weekday,
                slot_start=time(18),
                slot_end=time(1),
                max_guests=20,
            ),
        ]
    )
    session.commit()


def test_ruhetag_bietet_nicht_die_nacht_des_vortags_an(session, tenant_id):
    # Review PR #152: Sonntag bis 01:00 geoeffnet. Montag 00:00 und 00:30 haben das
    # Datum des Ruhetags, gehoeren aber zum Sonntagabend, 18 Stunden vor dem Wunsch.
    session.query(OpeningHours).filter_by(tenant_id=tenant_id, weekday=6).delete()
    session.query(Capacity).filter_by(tenant_id=tenant_id, weekday=6).delete()
    _nachtfenster(session, tenant_id, weekday=6)
    sonntag_frueh = berlin(date(2026, 9, 13), 8)
    result = check_slot(session, tenant_id, berlin(MONTAG, 19), 2, now=sonntag_frueh)
    assert result.available is False
    assert result.alternatives == []


def test_wunsch_nach_ladenschluss_nachts_bekommt_termine_desselben_abends(
    session, tenant_id
):
    # Review PR #152: Dienstag 01:00 gehoert zum Montagabend (Betriebstag Montag),
    # das Fenster schliesst um 01:00. Angeboten wird derselbe Abend.
    _nachtfenster(session, tenant_id, weekday=0)
    result = check_slot(
        session, tenant_id, berlin(DIENSTAG, 1, 0), 2, now=berlin(MONTAG, 20)
    )
    assert result.available is False
    assert result.alternatives == [
        berlin(DIENSTAG, 0, 30).astimezone(UTC),
        berlin(DIENSTAG, 0, 0).astimezone(UTC),
    ]


def test_fensterende_ist_exklusiv_und_ausserhalb_der_oeffnung_nicht_buchbar(
    session, tenant_id
):
    assert (
        check_slot(session, tenant_id, berlin(DIENSTAG, 21, 30), 2, now=NOW).available
        is True
    )
    assert (
        check_slot(session, tenant_id, berlin(DIENSTAG, 22, 0), 2, now=NOW).available
        is False
    )
    assert (
        check_slot(session, tenant_id, berlin(DIENSTAG, 15, 0), 2, now=NOW).available
        is False
    )


def test_alternativen_liegen_nie_in_der_vergangenheit(session, tenant_id):
    spaeter = berlin(DIENSTAG, 21, 45)
    result = check_slot(session, tenant_id, berlin(DIENSTAG, 22, 30), 2, now=spaeter)
    assert result.available is False
    assert result.alternatives == []


def test_fenster_ueber_mitternacht_gilt_auch_fuer_check_slot(session, tenant_id, book):
    # Montag 18:00 bis 01:00, 20 Plätze. Wunsch Dienstag 00:30 gehört zu diesem Fenster.
    session.add_all(
        [
            OpeningHours(
                tenant_id=tenant_id,
                weekday=0,
                opens_at=time(18),
                closes_at=time(1),
                service="dinein",
            ),
            Capacity(
                tenant_id=tenant_id,
                weekday=0,
                slot_start=time(18),
                slot_end=time(1),
                max_guests=20,
            ),
        ]
    )
    session.commit()
    montag_abend = berlin(MONTAG, 23)
    wunsch = berlin(DIENSTAG, 0, 30)
    assert check_slot(session, tenant_id, wunsch, 2, now=montag_abend).available is True

    book(berlin(MONTAG, 22), 19)
    assert check_slot(session, tenant_id, wunsch, 1, now=montag_abend).available is True
    result = check_slot(session, tenant_id, wunsch, 2, now=montag_abend)
    assert result.available is False
    # Das Nachtfenster ist voll. Dienstagmittag gehoert zu einem anderen Betriebstag
    # und hiesse ohne Tag "halb zwoelf" - nachts klingt das nach 23:30 (Review PR #152).
    assert result.alternatives == []


def test_vergangener_zeitpunkt_ist_invalid_input(session, tenant_id):
    with pytest.raises(InvalidInput):
        check_slot(session, tenant_id, berlin(DIENSTAG, 7, 0), 2, now=NOW)


def test_unbekannter_mandant_ist_not_found(session, tenant_id):
    with pytest.raises(NotFound):
        check_slot(session, uuid.uuid4(), berlin(DIENSTAG, 18, 0), 2, now=NOW)


@pytest.mark.parametrize(
    ("hh", "mm", "expected"),
    [
        (18, 0, "sechs Uhr"),
        (18, 30, "halb sieben"),
        (12, 30, "halb eins"),
        (13, 0, "ein Uhr"),
        (20, 15, "viertel nach acht"),
        (20, 45, "viertel vor neun"),
        (18, 20, "18 Uhr 20"),
        (0, 0, "zwölf Uhr"),
    ],
)
def test_gesprochene_uhrzeit(hh, mm, expected):
    assert spoken_time(berlin(DIENSTAG, hh, mm)) == expected
