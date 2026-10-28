"""domain/reservations: check_slot mit Kapazität, Öffnungszeiten, Alternativen und Grenzfällen."""

import uuid
from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from api.core.errors import InvalidInput, NotFound
from api.domain.reservations import check_slot
from api.domain.reservations.spoken import spoken_time
from api.models import (
    Call,
    Capacity,
    OpeningHours,
    Reservation,
    ServiceConfig,
    SpecialDay,
)
from scripts.seed import seed

BERLIN = ZoneInfo("Europe/Berlin")
# Seed: Montag Ruhetag; Kapazität 11:30-14:00 mit 30 und 17:00-22:00 mit 40 Gästen, Raster 30 min.
# service_config: Beginn fruehestens 15 min nach Oeffnung, spaetestens 30 min vor Schluss (D12).
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


def test_volles_fenster_liefert_zwei_naechste_alternativen_desselben_services(
    session, tenant_id, book
):
    # Abend in zwei Turns: 17:00-19:00 und 19:00-22:00 mit je 20 Plaetzen. Der
    # zweite Turn ist voll, mittags ist frei, angeboten wird nur der Abend (T-1.14).
    _evening_in_two_turns(session, tenant_id)
    book(berlin(DIENSTAG, 19, 0), 20)
    result = check_slot(session, tenant_id, berlin(DIENSTAG, 19, 30), 2, now=NOW)
    assert result.available is False
    assert result.alternatives == [
        berlin(DIENSTAG, 18, 30).astimezone(UTC),
        berlin(DIENSTAG, 18, 0).astimezone(UTC),
    ]
    assert (
        result.say == "Um halb acht ist leider voll. Halb sieben oder sechs Uhr ginge."
    )


def test_voller_abend_bietet_keinen_mittag_an(session, tenant_id, book):
    # Anforderung Maxi 26.09.2026: Abendwunsch nur Abend. Vorher kam 13:30 als
    # "halb zwei", 5 h vor dem Wunsch.
    book(berlin(DIENSTAG, 19, 0), 38)
    result = check_slot(session, tenant_id, berlin(DIENSTAG, 18, 30), 4, now=NOW)
    assert result.available is False
    assert result.alternatives == []
    assert (
        result.say
        == "Um halb sieben ist leider nichts frei, auch nicht kurz davor oder danach."
    )


def test_voller_mittag_bietet_keinen_abend_an(session, tenant_id, book):
    book(berlin(DIENSTAG, 12, 0), 30)
    result = check_slot(session, tenant_id, berlin(DIENSTAG, 13, 0), 2, now=NOW)
    assert result.available is False
    assert result.alternatives == []


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
    # A party of 30 probes the whole window; the party limits have their own tests.
    _set_config(
        session,
        tenant_id,
        reservation_max_party_size=30,
        reservation_max_party_size_same_day=30,
    )
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


def test_the_drafts_of_the_asking_call_do_not_count(session, tenant_id, book):
    """#207: a guest who corrects a draft is not told the evening is full by
    the draft itself; for any other call the draft still holds its seats."""
    book(berlin(DIENSTAG, 18, 0), 38, status="draft")
    own = session.scalar(select(Reservation.call_id))
    at = berlin(DIENSTAG, 19, 0)

    assert check_slot(session, tenant_id, at, 4, now=NOW, call_id=own).available
    assert not check_slot(
        session, tenant_id, at, 4, now=NOW, call_id=uuid.uuid4()
    ).available
    assert not check_slot(session, tenant_id, at, 4, now=NOW).available


def test_a_confirmed_table_counts_for_its_own_call_too(session, tenant_id, book):
    """Only drafts are replaced by a correction; a booked table stays booked."""
    book(berlin(DIENSTAG, 18, 0), 38)
    own = session.scalar(select(Reservation.call_id))

    assert not check_slot(
        session, tenant_id, berlin(DIENSTAG, 19, 0), 4, now=NOW, call_id=own
    ).available


def test_gruppe_groesser_als_jedes_fenster_bekommt_keine_alternativen(
    session, tenant_id
):
    result = check_slot(session, tenant_id, berlin(DIENSTAG, 18, 0), 41, now=NOW)
    assert result.available is False and result.alternatives == []
    assert (
        result.say
        == "Um sechs Uhr ist leider nichts frei, auch nicht kurz davor oder danach."
    )


def test_ruhetag_hat_keine_termine(session, tenant_id):
    result = check_slot(
        session, tenant_id, berlin(MONTAG, 18, 0), 2, now=berlin(MONTAG, 8)
    )
    assert result.available is False and result.alternatives == []
    # Review PR #152: der Gast hoert, dass zu ist, statt andere Uhrzeiten zu probieren.
    assert result.say == "Am Montag haben wir leider geschlossen."


def test_ruhetag_bietet_keine_termine_vom_vortag_an(session, tenant_id):
    # Befund T-5.2 (reservierung_0027): Die Fenster des Vortags sind nur fuer die
    # Zeit nach Mitternacht geladen. Am Sonntagmorgen gefragt, lagen Sonntag 21:30
    # und 21:00 noch in der Zukunft und wurden fuer Montag als "halb zehn" angeboten.
    sonntag_frueh = berlin(date(2026, 9, 13), 8)
    result = check_slot(session, tenant_id, berlin(MONTAG, 19), 2, now=sonntag_frueh)
    assert result.available is False
    assert result.alternatives == []


def test_alternativen_im_langen_service_nur_unter_sechs_stunden_abstand(
    session, tenant_id, book
):
    # Ein Service 11:00-23:00 in zwei Turns, der spaete ist voll. Wunsch 21:00:
    # 15:30 liegt 5,5 h davor und ist als "halb vier" eindeutig, 15:00 liegt genau
    # 6 h davor, "drei Uhr" koennte auch 03:00 sein.
    _single_service(session, tenant_id, time(11), time(23), split=time(16))
    book(berlin(DIENSTAG, 21, 0), 20)
    result = check_slot(session, tenant_id, berlin(DIENSTAG, 21), 2, now=NOW)
    assert result.available is False
    assert result.alternatives == [berlin(DIENSTAG, 15, 30).astimezone(UTC)]


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


def _replace_tuesday_capacity(
    session, tenant_id, rows: list[tuple[time, time, int]]
) -> None:
    """Ersetzt Oeffnung (dinein) und Kapazitaet am Dienstag, Kapazitaet aus `rows`."""
    session.query(OpeningHours).filter_by(
        tenant_id=tenant_id, weekday=1, service="dinein"
    ).delete()
    session.query(Capacity).filter_by(tenant_id=tenant_id, weekday=1).delete()
    session.add_all(
        [
            Capacity(
                tenant_id=tenant_id,
                weekday=1,
                slot_start=start,
                slot_end=end,
                max_guests=guests,
            )
            for start, end, guests in rows
        ]
    )
    session.commit()


def _add_tuesday_hours(session, tenant_id, windows: list[tuple[time, time]]) -> None:
    session.add_all(
        [
            OpeningHours(
                tenant_id=tenant_id,
                weekday=1,
                opens_at=opens,
                closes_at=closes,
                service="dinein",
            )
            for opens, closes in windows
        ]
    )
    session.commit()


def _lunch_from_eleven(session, tenant_id) -> None:
    """Dienstag Mittag 11:00-14:00 wie im Pilotbetrieb (D12), Abend wie im Seed."""
    _replace_tuesday_capacity(
        session, tenant_id, [(time(11), time(14), 30), (time(17), time(22), 40)]
    )
    _add_tuesday_hours(session, tenant_id, [(time(11), time(14)), (time(17), time(22))])


def _evening_in_two_turns(session, tenant_id) -> None:
    _replace_tuesday_capacity(
        session,
        tenant_id,
        [
            (time(11, 30), time(14), 30),
            (time(17), time(19), 20),
            (time(19), time(22), 20),
        ],
    )
    _add_tuesday_hours(
        session, tenant_id, [(time(11, 30), time(14)), (time(17), time(22))]
    )


def _single_service(session, tenant_id, opens: time, closes: time, split: time):
    _replace_tuesday_capacity(
        session, tenant_id, [(opens, split, 20), (split, closes, 20)]
    )
    _add_tuesday_hours(session, tenant_id, [(opens, closes)])


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


def test_nachts_kein_mittag_als_alternative(session, tenant_id, book):
    # Review PR #152: Dienstag mit Mittag und Nachtfenster bis 01:00, die Nacht
    # ist voll. Wunsch Mittwoch 00:30: Dienstag 13:30 hiesse "halb zwei" und
    # klaenge nachts nach 01:30.
    session.query(OpeningHours).filter_by(tenant_id=tenant_id, weekday=1).filter(
        OpeningHours.opens_at == time(17)
    ).delete()
    session.query(Capacity).filter_by(tenant_id=tenant_id, weekday=1).filter(
        Capacity.slot_start == time(17)
    ).delete()
    _nachtfenster(session, tenant_id, weekday=1)
    book(berlin(DIENSTAG, 20), 20)
    result = check_slot(
        session, tenant_id, berlin(date(2026, 9, 16), 0, 30), 2, now=NOW
    )
    assert result.available is False
    assert result.alternatives == []


def test_wunsch_nach_ladenschluss_nachts_ist_geschlossen(session, tenant_id):
    # D12: nach dem letzten Service heisst es "geschlossen" plus Frage nach einer
    # anderen Uhrzeit. Dienstag 01:00 liegt nach dem Montagabend bis 01:00 und ist
    # kein Morgen vor der Oeffnung, auch wenn Dienstagmittag noch kommt.
    _nachtfenster(session, tenant_id, weekday=0)
    result = check_slot(
        session, tenant_id, berlin(DIENSTAG, 1, 0), 2, now=berlin(MONTAG, 20)
    )
    assert result.available is False
    assert result.alternatives == []
    assert result.say == (
        "Um ein Uhr haben wir leider geschlossen. "
        "Zu welcher anderen Uhrzeit passt es Ihnen?"
    )


def _is_free(session, tenant_id, at: datetime) -> bool:
    return check_slot(session, tenant_id, at, 2, now=NOW).available


def test_beginn_frueh_und_spaet_nach_oeffnung_und_schluss(session, tenant_id):
    # D12: Mittag 11-14 und Abend 17-22 -> Beginn 11:15-13:30 und 17:15-21:30,
    # im 30-Minuten-Raster angeboten ab 11:30 und 17:30.
    _lunch_from_eleven(session, tenant_id)
    for hh, mm in ((11, 15), (11, 30), (13, 30), (17, 15), (17, 30), (21, 30)):
        assert _is_free(session, tenant_id, berlin(DIENSTAG, hh, mm)), (hh, mm)
    for hh, mm in ((11, 0), (11, 10), (13, 45), (17, 0), (21, 45), (22, 0)):
        assert not _is_free(session, tenant_id, berlin(DIENSTAG, hh, mm)), (hh, mm)


def test_zu_frueh_im_service_bekommt_alternativen_desselben_services(
    session, tenant_id
):
    result = check_slot(session, tenant_id, berlin(DIENSTAG, 17, 0), 2, now=NOW)
    assert result.available is False
    assert result.alternatives == [
        berlin(DIENSTAG, 17, 30).astimezone(UTC),
        berlin(DIENSTAG, 18, 0).astimezone(UTC),
    ]
    assert result.say == (
        "Um fünf Uhr können wir leider keinen Tisch reservieren. "
        "Halb sechs oder sechs Uhr ginge."
    )


def test_kurz_vor_schluss_bekommt_die_letzten_termine(session, tenant_id):
    result = check_slot(session, tenant_id, berlin(DIENSTAG, 21, 45), 2, now=NOW)
    assert result.available is False
    assert result.alternatives == [
        berlin(DIENSTAG, 21, 30).astimezone(UTC),
        berlin(DIENSTAG, 21, 0).astimezone(UTC),
    ]
    assert result.say == (
        "Um viertel vor zehn können wir leider keinen Tisch reservieren. "
        "Halb zehn oder neun Uhr ginge."
    )


def test_zu_frueh_im_vollen_service_fragt_nach_anderer_uhrzeit(
    session, tenant_id, book
):
    # Review PR #158: Abend voll, Wunsch 17:00 vor dem fruehesten Beginn.
    book(berlin(DIENSTAG, 19, 0), 40)
    result = check_slot(session, tenant_id, berlin(DIENSTAG, 17, 0), 2, now=NOW)
    assert result.available is False
    assert result.alternatives == []
    assert result.say == (
        "Um fünf Uhr können wir leider keinen Tisch reservieren. "
        "Zu welcher anderen Uhrzeit passt es Ihnen?"
    )


def test_morgens_ganzer_tag_voll_sagt_das(session, tenant_id, book):
    # Review PR #158: Mittag und Abend voll. Der Gast hoert, dass heute nichts
    # mehr frei ist, statt nur "geschlossen" und eine Frage nach der Uhrzeit.
    book(berlin(DIENSTAG, 12, 0), 30)
    book(berlin(DIENSTAG, 19, 0), 40)
    result = check_slot(session, tenant_id, berlin(DIENSTAG, 9), 2, now=NOW)
    assert result.available is False
    assert result.alternatives == []
    assert result.say == (
        "Um neun Uhr haben wir noch geschlossen, und an dem Tag ist leider nichts mehr frei."
    )


def test_schlusszeit_ist_auch_als_alternative_kein_beginn(session, tenant_id, book):
    # Review PR #158: Kapazitaet reicht ueber den Schluss 22:00 hinaus, Abstand 0.
    # 22:00 liegt im Raster, ist aber kein Beginn; angeboten wird nur 21:30.
    _replace_tuesday_capacity(
        session,
        tenant_id,
        [
            (time(11, 30), time(14), 30),
            (time(17), time(21, 30), 20),
            (time(21, 30), time(23), 20),
        ],
    )
    _add_tuesday_hours(
        session, tenant_id, [(time(11, 30), time(14)), (time(17), time(22))]
    )
    config = session.get(ServiceConfig, tenant_id)
    config.reservation_last_start_minutes = 0
    session.commit()
    book(berlin(DIENSTAG, 19, 0), 20)
    result = check_slot(session, tenant_id, berlin(DIENSTAG, 21), 2, now=NOW)
    assert result.alternatives == [berlin(DIENSTAG, 21, 30).astimezone(UTC)]


def test_abstand_kommt_aus_der_db(session, tenant_id):
    config = session.get(ServiceConfig, tenant_id)
    config.reservation_lead_minutes = 0
    config.reservation_last_start_minutes = 60
    session.commit()
    assert _is_free(session, tenant_id, berlin(DIENSTAG, 17, 0))
    assert _is_free(session, tenant_id, berlin(DIENSTAG, 21, 0))
    assert not _is_free(session, tenant_id, berlin(DIENSTAG, 21, 30))


@pytest.mark.parametrize(("hh", "spoken"), [(15, "drei Uhr"), (23, "elf Uhr")])
def test_zwischen_und_nach_den_services_geschlossen(session, tenant_id, hh, spoken):
    # D12: keine Alternative aus einem anderen Service, der Gast nennt eine Uhrzeit.
    result = check_slot(session, tenant_id, berlin(DIENSTAG, hh), 2, now=NOW)
    assert result.available is False
    assert result.alternatives == []
    assert result.say == (
        f"Um {spoken} haben wir leider geschlossen. "
        "Zu welcher anderen Uhrzeit passt es Ihnen?"
    )


def test_morgens_vor_der_oeffnung_bietet_den_mittag_an(session, tenant_id):
    _lunch_from_eleven(session, tenant_id)
    result = check_slot(session, tenant_id, berlin(DIENSTAG, 9), 2, now=NOW)
    assert result.available is False
    assert result.alternatives == [
        berlin(DIENSTAG, 11, 30).astimezone(UTC),
        berlin(DIENSTAG, 12, 0).astimezone(UTC),
    ]
    assert result.say == (
        "Um neun Uhr haben wir noch geschlossen. Halb zwölf oder zwölf Uhr ginge."
    )


def test_nachts_nach_dem_ruhetag_ist_kein_morgen(session, tenant_id):
    # Review PR #158: ohne Fenster am Vortag zaehlt Mitternacht als letzter Schluss.
    # Dienstag 01:00 nach dem Ruhetag heisst "geschlossen" wie nach jedem Abend.
    result = check_slot(
        session, tenant_id, berlin(DIENSTAG, 1, 0), 2, now=berlin(MONTAG, 20)
    )
    assert result.alternatives == []
    assert result.say == (
        "Um ein Uhr haben wir leider geschlossen. "
        "Zu welcher anderen Uhrzeit passt es Ihnen?"
    )


def test_frueh_morgens_zaehlt_der_echte_schluss_des_vorabends(session, tenant_id):
    # Review PR #158 (Codex): Mitternacht nur ohne Schluss am Vortag. Montag ist
    # Ruhetag, also Mittwoch: Dienstag schliesst 22:00, Mittwoch oeffnet 11:30.
    # 05:00 liegt 6,5 h vor der Oeffnung und 7 h nach dem Schluss: Morgen.
    wednesday = date(2026, 9, 16)
    result = check_slot(session, tenant_id, berlin(wednesday, 5), 2, now=NOW)
    assert result.alternatives == [
        berlin(wednesday, 12, 0).astimezone(UTC),
        berlin(wednesday, 12, 30).astimezone(UTC),
    ]
    assert result.say == (
        "Um fünf Uhr haben wir noch geschlossen. Mittags um zwölf oder um halb eins ginge."
    )


def test_letzter_beginn_null_heisst_bis_vor_schluss(session, tenant_id):
    # Review PR #158 (Codex): 0 Minuten vor Schluss macht die Schlusszeit nicht zum
    # Beginn; um 22:00 ist geschlossen, 21:45 ist buchbar.
    config = session.get(ServiceConfig, tenant_id)
    config.reservation_last_start_minutes = 0
    session.commit()
    assert _is_free(session, tenant_id, berlin(DIENSTAG, 21, 45))
    result = check_slot(session, tenant_id, berlin(DIENSTAG, 22), 2, now=NOW)
    assert result.available is False
    assert result.say.startswith("Um zehn Uhr haben wir leider geschlossen.")


def test_morgens_mittag_voll_bietet_den_abend_mit_tageszeit_an(
    session, tenant_id, book
):
    # Ueber sechs Stunden Abstand: ohne Tageszeit klaenge "halb sechs" nach 05:30.
    book(berlin(DIENSTAG, 12, 0), 30)
    result = check_slot(session, tenant_id, berlin(DIENSTAG, 9), 2, now=NOW)
    assert result.alternatives == [
        berlin(DIENSTAG, 17, 30).astimezone(UTC),
        berlin(DIENSTAG, 18, 0).astimezone(UTC),
    ]
    assert result.say == (
        "Um neun Uhr haben wir noch geschlossen. "
        "Abends um halb sechs oder um sechs ginge."
    )


def test_sondertag_verschiebt_beginn_und_letzten_termin(session, tenant_id):
    # Sonderzeit 12:00-20:00: fruehestens 12:15, spaetestens 19:30.
    session.add(
        SpecialDay(
            tenant_id=tenant_id,
            date=DIENSTAG,
            closed=False,
            opens_at=time(12),
            closes_at=time(20),
        )
    )
    session.commit()
    assert not _is_free(session, tenant_id, berlin(DIENSTAG, 12, 0))
    assert _is_free(session, tenant_id, berlin(DIENSTAG, 12, 30))
    assert _is_free(session, tenant_id, berlin(DIENSTAG, 19, 30))
    assert not _is_free(session, tenant_id, berlin(DIENSTAG, 20, 0))


def _special_opening(session, tenant_id, day: date, opens: time, closes: time):
    session.add(
        SpecialDay(
            tenant_id=tenant_id,
            date=day,
            closed=False,
            opens_at=opens,
            closes_at=closes,
        )
    )
    session.commit()


TEAM_DECIDES_AT_SEVEN = (
    "Um sieben Uhr kann ich den Tisch nicht selbst zusagen, das bespricht das "
    "Team persönlich mit Ihnen."
)


def test_a_special_opening_on_the_day_off_goes_to_the_team(session, tenant_id):
    """Repo audit of 27.10.2026, finding 4: open by a special day, but no
    capacity is kept for the day off. The team decides, the guest never hears
    "nichts frei" from an empty restaurant."""
    _special_opening(session, tenant_id, date(2026, 9, 21), time(12), time(22))

    result = check_slot(session, tenant_id, berlin(date(2026, 9, 21), 19), 2, now=NOW)

    assert (result.available, result.team_callback) == (False, True)
    assert result.alternatives == []
    assert result.say == TEAM_DECIDES_AT_SEVEN


@pytest.mark.parametrize("hh", [10, 15])
def test_special_hours_beyond_the_capacity_go_to_the_team(session, tenant_id, hh):
    # Capacity 11:30-14:00 and 17:00-22:00; the special day opens 09:00-22:00.
    _special_opening(session, tenant_id, DIENSTAG, time(9), time(22))

    result = check_slot(session, tenant_id, berlin(DIENSTAG, hh), 2, now=NOW)

    assert (result.available, result.team_callback) == (False, True)


def test_a_special_opening_inside_the_capacity_is_booked(session, tenant_id):
    _special_opening(session, tenant_id, DIENSTAG, time(9), time(22))

    assert _is_free(session, tenant_id, berlin(DIENSTAG, 19))


def test_alternativen_liegen_nie_in_der_vergangenheit(session, tenant_id):
    spaeter = berlin(DIENSTAG, 21, 35)
    result = check_slot(session, tenant_id, berlin(DIENSTAG, 21, 45), 2, now=spaeter)
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


# -- Limits for a reservation by phone (#252, migration 007) ----------------------
# Seed defaults: 90 days ahead, the agent books up to 8 guests itself, on the
# same day up to 5. Above that the table is free by the numbers and still not
# booked: the team calls back.
SAMSTAG = date(2026, 9, 19)


def _set_config(session, tenant_id, **values) -> None:
    config = session.get(ServiceConfig, tenant_id)
    for name, value in values.items():
        setattr(config, name, value)
    session.commit()


def test_a_party_at_the_limit_is_booked_by_the_agent(session, tenant_id):
    result = check_slot(session, tenant_id, berlin(SAMSTAG, 19), 8, now=NOW)
    assert result.available is True
    assert result.team_callback is False and result.say is None


def test_a_party_above_the_limit_goes_to_the_team(session, tenant_id):
    result = check_slot(session, tenant_id, berlin(SAMSTAG, 19), 9, now=NOW)
    assert result.available is False and result.team_callback is True
    assert result.alternatives == []
    assert result.say.startswith("Für neun Personen kann ich den Tisch nicht selbst")
    assert "heute" not in result.say


def test_on_the_same_day_the_smaller_limit_counts(session, tenant_id):
    five = check_slot(session, tenant_id, berlin(DIENSTAG, 19), 5, now=NOW)
    six = check_slot(session, tenant_id, berlin(DIENSTAG, 19), 6, now=NOW)
    assert five.available is True and five.team_callback is False
    assert six.available is False and six.team_callback is True
    assert six.say.startswith("Für sechs Personen noch heute kann ich den Tisch")


def test_the_same_day_is_the_day_of_the_restaurant_not_of_utc(session, tenant_id):
    # 23:30 on Tuesday in Berlin: lunch on Wednesday is tomorrow.
    late = datetime(2026, 9, 15, 23, 30, tzinfo=BERLIN)
    tomorrow = check_slot(
        session, tenant_id, berlin(date(2026, 9, 16), 12), 6, now=late
    )
    assert tomorrow.available is True
    # 00:30 on Wednesday in Berlin is still Tuesday in UTC: the evening of
    # that Wednesday is today.
    night = datetime(2026, 9, 16, 0, 30, tzinfo=BERLIN)
    today = check_slot(
        session, tenant_id, berlin(date(2026, 9, 16), 19), 6, now=night.astimezone(UTC)
    )
    assert today.team_callback is True


def test_the_party_limits_come_from_the_database(session, tenant_id):
    _set_config(
        session,
        tenant_id,
        reservation_max_party_size=10,
        reservation_max_party_size_same_day=10,
    )
    assert check_slot(session, tenant_id, berlin(SAMSTAG, 19), 10, now=NOW).available
    assert check_slot(session, tenant_id, berlin(DIENSTAG, 19), 10, now=NOW).available
    above = check_slot(session, tenant_id, berlin(SAMSTAG, 19), 11, now=NOW)
    assert above.team_callback is True


def test_a_same_day_limit_above_the_general_one_lifts_nothing(session, tenant_id):
    _set_config(session, tenant_id, reservation_max_party_size_same_day=20)
    result = check_slot(session, tenant_id, berlin(DIENSTAG, 19), 9, now=NOW)
    assert result.team_callback is True
    assert "heute" not in result.say


def test_a_limit_of_zero_sends_every_table_to_the_team(session, tenant_id):
    _set_config(session, tenant_id, reservation_max_party_size=0)
    result = check_slot(session, tenant_id, berlin(SAMSTAG, 19), 1, now=NOW)
    assert result.team_callback is True
    assert result.say.startswith("Für eine Person kann ich")


def test_a_large_party_hears_closed_and_full_like_any_other(session, tenant_id, book):
    # Only a table that is free by the numbers goes to the team: a closed day
    # stays closed, a full evening full, and the guest can name another time.
    closed = check_slot(session, tenant_id, berlin(date(2026, 9, 21), 19), 12, now=NOW)
    assert closed.team_callback is False
    assert closed.say == "Am Montag haben wir leider geschlossen."
    book(berlin(SAMSTAG, 19), 35)
    full = check_slot(session, tenant_id, berlin(SAMSTAG, 19), 12, now=NOW)
    assert full.available is False and full.team_callback is False
    assert "leider nichts frei" in full.say


def test_the_last_day_of_the_horizon_can_be_booked_the_next_one_not(session, tenant_id):
    _set_config(session, tenant_id, reservation_max_days_ahead=4)
    last = check_slot(session, tenant_id, berlin(SAMSTAG, 19), 2, now=NOW)
    assert last.available is True
    beyond = check_slot(session, tenant_id, berlin(date(2026, 9, 20), 19), 2, now=NOW)
    assert beyond.available is False and beyond.team_callback is False
    assert beyond.alternatives == []
    assert "höchstens 4 Tage im Voraus" in beyond.say


def test_the_default_horizon_is_ninety_days(session, tenant_id):
    # 13.12.2026 is day 89 and a Sunday, 15.12. is day 91 and a Tuesday; day 90
    # is a Monday, the day off.
    inside = check_slot(session, tenant_id, berlin(date(2026, 12, 13), 19), 2, now=NOW)
    assert inside.available is True
    beyond = check_slot(session, tenant_id, berlin(date(2026, 12, 15), 19), 2, now=NOW)
    assert beyond.available is False
    assert "höchstens 90 Tage im Voraus" in beyond.say


def test_the_horizon_counts_days_of_the_restaurant_not_hours(session, tenant_id):
    # At 22:00 the evening four days on is less than 96 hours away, the lunch
    # of the fifth day more than 96 and less than 120: the calendar day decides.
    _set_config(session, tenant_id, reservation_max_days_ahead=4)
    late = datetime(2026, 9, 15, 22, 0, tzinfo=BERLIN)
    assert check_slot(session, tenant_id, berlin(SAMSTAG, 21), 2, now=late).available
    fifth = check_slot(session, tenant_id, berlin(date(2026, 9, 20), 12), 2, now=late)
    assert fifth.available is False and "höchstens 4 Tage" in fifth.say


def test_a_horizon_of_zero_days_takes_only_today(session, tenant_id):
    """Repo audit of 27.10.2026, finding 9: 0 means today only (migration
    007), not "höchstens 0 Tage im Voraus"."""
    _set_config(session, tenant_id, reservation_max_days_ahead=0)

    assert check_slot(session, tenant_id, berlin(DIENSTAG, 19), 2, now=NOW).available
    tomorrow = check_slot(session, tenant_id, berlin(date(2026, 9, 16), 19), 2, now=NOW)

    assert tomorrow.available is False
    assert tomorrow.say == "Tische reserviere ich nur für heute. Passt es Ihnen heute?"


def test_beyond_the_horizon_a_large_party_is_not_taken_either(session, tenant_id):
    result = check_slot(session, tenant_id, berlin(date(2026, 12, 15), 19), 12, now=NOW)
    assert result.available is False and result.team_callback is False
    assert "Tage im Voraus" in result.say


def test_a_horizon_of_one_day_is_said_in_the_singular(session, tenant_id):
    _set_config(session, tenant_id, reservation_max_days_ahead=1)
    result = check_slot(session, tenant_id, berlin(date(2026, 9, 17), 19), 2, now=NOW)
    assert "höchstens einen Tag im Voraus" in result.say
