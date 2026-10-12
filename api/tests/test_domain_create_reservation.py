"""domain/reservations: create_reservation als Entwurf mit readback, Idempotenz und Grenzfällen."""

import threading
import uuid
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from psycopg.errors import QueryCanceled
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from api.core.errors import Conflict, InvalidInput, NotFound, TeamDecides
from api.domain.calls import end_call
from api.domain.confirm import confirm
from api.domain.reservations import capacity as reservations_capacity
from api.domain.reservations import create_reservation
from api.models import (
    AuditLog,
    Call,
    Capacity,
    OpeningHours,
    Reservation,
    ServiceConfig,
)
from api.schemas.calls import EndCallRequest
from api.schemas.confirm import ConfirmRequest
from api.schemas.reservations import CreateReservationRequest
from api.tests.conftest import lift_party_limits
from scripts.seed import seed

BERLIN = ZoneInfo("Europe/Berlin")
# Seed: Montag Ruhetag; Kapazität 11:30-14:00 mit 30 und 17:00-22:00 mit 40 Gästen.
DIENSTAG = date(2026, 9, 15)
MITTWOCH = date(2026, 9, 16)
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


def request(tenant_id, call_id, **overrides) -> CreateReservationRequest:
    body = {
        "call_id": call_id,
        "tenant_id": tenant_id,
        "idempotency_key": uuid.uuid4().hex,
        "guest_name": "Müller",
        "phone": "+4972215551234",
        "party_size": 4,
        "reserved_for": berlin(DIENSTAG, 18, 30),
        "note": None,
        **overrides,
    }
    return CreateReservationRequest(**body)


def test_entwurf_mit_readback_und_audit(session, tenant_id, call_id):
    draft = create_reservation(session, request(tenant_id, call_id), now=NOW)

    assert draft.status == "draft"
    assert draft.readback == (
        "Ein Tisch für vier Personen heute um halb sieben, auf den Namen Müller. Passt das so?"
    )
    row = session.get(Reservation, draft.reservation_id)
    assert row is not None and row.status == "draft" and row.call_id == call_id
    assert row.reserved_for == berlin(DIENSTAG, 18, 30)

    audit = session.scalars(select(AuditLog)).all()
    assert len(audit) == 1
    assert audit[0].action == "reservation.draft_created"
    assert audit[0].entity_id == draft.reservation_id
    assert audit[0].payload["call_id"] == str(call_id)


def test_gleicher_schluessel_gleiche_antwort_kein_zweiter_vorgang(
    session, tenant_id, call_id
):
    key = "k-1"
    first = create_reservation(
        session, request(tenant_id, call_id, idempotency_key=key), now=NOW
    )
    second = create_reservation(
        session,
        request(
            tenant_id, call_id, idempotency_key=key, guest_name="Anders", party_size=2
        ),
        now=NOW,
    )
    assert second == first
    assert session.scalar(select(func.count()).select_from(Reservation)) == 1
    assert session.scalar(select(func.count()).select_from(AuditLog)) == 1


def test_schluessel_eines_anderen_mandanten_ist_conflict(session, tenant_id, call_id):
    key = "k-2"
    create_reservation(
        session, request(tenant_id, call_id, idempotency_key=key), now=NOW
    )
    other = uuid.UUID(
        seed(session, tenant_name="Anderer", timezone="Europe/Berlin").tenant_id
    )
    with pytest.raises(Conflict):
        create_reservation(
            session, request(other, call_id, idempotency_key=key), now=NOW
        )


def test_schluessel_eines_anderen_anrufs_ist_conflict(session, tenant_id, call_id):
    """Codex PR #127, P1: wie bei Bestellungen - ein Schluessel eines anderen
    Anrufs liefert nie dessen Reservierung."""
    key = "gleicher-schluessel-anderer-anruf"
    create_reservation(
        session, request(tenant_id, call_id, idempotency_key=key), now=NOW
    )
    other_call = Call(
        tenant_id=tenant_id,
        external_session_id="ext-2",
        started_at=NOW,
        delete_after=DIENSTAG,
    )
    session.add(other_call)
    session.commit()
    with pytest.raises(Conflict):
        create_reservation(
            session, request(tenant_id, other_call.id, idempotency_key=key), now=NOW
        )


def test_voller_abend_ist_conflict_ohne_mittag(session, tenant_id, call_id):
    lift_party_limits(session, tenant_id)
    # T-1.14: Alternativen nur im selben Service; der Abend ist ganz voll.
    create_reservation(session, request(tenant_id, call_id, party_size=40), now=NOW)
    with pytest.raises(Conflict) as exc:
        create_reservation(session, request(tenant_id, call_id, party_size=1), now=NOW)
    assert "halb sieben ist leider nichts frei" in exc.value.say
    assert session.scalar(select(func.count()).select_from(Reservation)) == 1


def test_ruhetag_ist_conflict(session, tenant_id, call_id):
    montag = berlin(date(2026, 9, 21), 18, 30)
    with pytest.raises(Conflict):
        create_reservation(
            session, request(tenant_id, call_id, reserved_for=montag), now=NOW
        )


def test_vergangenheit_ist_invalid_input(session, tenant_id, call_id):
    with pytest.raises(InvalidInput):
        create_reservation(
            session,
            request(tenant_id, call_id, reserved_for=berlin(DIENSTAG, 7, 0)),
            now=NOW,
        )


def test_unbekannter_anruf_ist_not_found(session, tenant_id):
    with pytest.raises(NotFound) as exc:
        create_reservation(session, request(tenant_id, uuid.uuid4()), now=NOW)
    assert exc.value.say is not None
    assert session.scalar(select(func.count()).select_from(Reservation)) == 0


def test_anruf_eines_anderen_mandanten_ist_not_found(session, tenant_id, call_id):
    other = uuid.UUID(
        seed(session, tenant_name="Anderer", timezone="Europe/Berlin").tenant_id
    )
    with pytest.raises(NotFound):
        create_reservation(session, request(other, call_id), now=NOW)


def test_unbekannter_mandant_ist_not_found(session, call_id):
    with pytest.raises(NotFound):
        create_reservation(session, request(uuid.uuid4(), call_id), now=NOW)


def test_rufnummer_wird_normalisiert(session, tenant_id, call_id):
    draft = create_reservation(
        session, request(tenant_id, call_id, phone="0721 / 555-1234"), now=NOW
    )
    assert draft.phone == "+497215551234"
    assert session.get(Reservation, draft.reservation_id).phone == "+497215551234"


def test_ungueltige_rufnummer_ist_invalid_input(session, tenant_id, call_id):
    with pytest.raises(InvalidInput) as exc:
        create_reservation(session, request(tenant_id, call_id, phone="12345"), now=NOW)
    assert exc.value.say is not None


def test_leerer_name_ist_invalid_input(session, tenant_id, call_id):
    with pytest.raises(InvalidInput):
        create_reservation(
            session, request(tenant_id, call_id, guest_name="   "), now=NOW
        )


def test_readback_morgen_eine_person_mit_hinweis(session, tenant_id, call_id):
    draft = create_reservation(
        session,
        request(
            tenant_id,
            call_id,
            party_size=1,
            reserved_for=berlin(MITTWOCH, 12, 0),
            note="  Kinderstuhl ",
        ),
        now=NOW,
    )
    assert draft.note == "Kinderstuhl"
    assert draft.readback == (
        "Ein Tisch für eine Person morgen um zwölf Uhr, auf den Namen Müller"
        ", mit dem Hinweis: Kinderstuhl. Passt das so?"
    )


def test_readback_mit_wochentag_und_datum(session, tenant_id, call_id):
    lift_party_limits(session, tenant_id)
    draft = create_reservation(
        session,
        request(
            tenant_id,
            call_id,
            party_size=15,
            reserved_for=berlin(date(2026, 9, 25), 19, 15),
        ),
        now=NOW,
    )
    assert draft.readback == (
        "Ein Tisch für 15 Personen am Freitag, den 25. September um viertel nach sieben"
        ", auf den Namen Müller. Passt das so?"
    )


def test_entwurf_zaehlt_sofort_gegen_die_kapazitaet(session, tenant_id, call_id):
    lift_party_limits(session, tenant_id)
    create_reservation(session, request(tenant_id, call_id, party_size=39), now=NOW)
    create_reservation(session, request(tenant_id, call_id, party_size=1), now=NOW)
    with pytest.raises(Conflict):
        create_reservation(session, request(tenant_id, call_id, party_size=1), now=NOW)


# --- A draft holds its seats only while its call can still confirm it (#250) ---


def _other_call(session, tenant_id) -> uuid.UUID:
    call = Call(
        tenant_id=tenant_id,
        external_session_id="ext-2",
        started_at=NOW,
        delete_after=DIENSTAG,
    )
    session.add(call)
    session.commit()
    return call.id


def _call_limit(session, tenant_id) -> timedelta:
    """From the database, like the code under test: no number of its own."""
    return timedelta(seconds=session.get(ServiceConfig, tenant_id).max_call_seconds)


def _hang_up(session, tenant_id, call_id, at: datetime) -> None:
    end_call(
        session,
        EndCallRequest(call_id=call_id, tenant_id=tenant_id, outcome="abandoned"),
        now=at,
    )


def test_a_draft_of_a_live_call_keeps_its_seats(session, tenant_id, call_id):
    """While the guest is on the line nobody else gets the table."""
    lift_party_limits(session, tenant_id)
    create_reservation(session, request(tenant_id, call_id, party_size=40), now=NOW)
    other = _other_call(session, tenant_id)

    with pytest.raises(Conflict):
        create_reservation(session, request(tenant_id, other, party_size=2), now=NOW)


def test_a_draft_of_an_ended_call_gives_its_seats_back(session, tenant_id, call_id):
    """Forty seats asked for, then hung up at the read-back: the evening must
    not stay full for everyone who calls after."""
    lift_party_limits(session, tenant_id)
    create_reservation(session, request(tenant_id, call_id, party_size=40), now=NOW)
    _hang_up(session, tenant_id, call_id, NOW + timedelta(minutes=1))
    other = _other_call(session, tenant_id)

    later = NOW + timedelta(minutes=2)
    draft = create_reservation(
        session, request(tenant_id, other, party_size=40), now=later
    )

    assert draft.status == "draft"


def test_a_draft_past_the_call_limit_gives_its_seats_back(session, tenant_id, call_id):
    """A call that never got its end event (process died, line dropped) must not
    hold seats for good either: no call lasts longer than the limit."""
    lift_party_limits(session, tenant_id)
    create_reservation(session, request(tenant_id, call_id, party_size=40), now=NOW)
    other = _other_call(session, tenant_id)
    limit = _call_limit(session, tenant_id)

    with pytest.raises(Conflict):
        create_reservation(
            session,
            request(tenant_id, other, party_size=2),
            now=NOW + limit - timedelta(seconds=1),
        )
    draft = create_reservation(
        session,
        request(tenant_id, other, party_size=2),
        now=NOW + limit + timedelta(seconds=1),
    )

    assert draft.status == "draft"


def test_a_confirmed_table_keeps_its_seats_after_the_call(session, tenant_id, call_id):
    """Only drafts expire. A yes is a booking, whatever the call did after."""
    lift_party_limits(session, tenant_id)
    draft = create_reservation(
        session, request(tenant_id, call_id, party_size=40), now=NOW
    )
    confirm(
        session,
        ConfirmRequest(
            call_id=call_id,
            tenant_id=tenant_id,
            entity="reservation",
            entity_id=draft.reservation_id,
            idempotency_key=uuid.uuid4().hex,
        ),
        now=NOW,
    )
    _hang_up(session, tenant_id, call_id, NOW + timedelta(minutes=1))
    other = _other_call(session, tenant_id)
    much_later = NOW + _call_limit(session, tenant_id) * 3

    with pytest.raises(Conflict):
        create_reservation(
            session, request(tenant_id, other, party_size=2), now=much_later
        )


def test_replay_behaelt_readback_ueber_mitternacht(session, tenant_id, call_id):
    """Gleicher Schlüssel, gleiche Antwort — auch wenn der Tag zwischen den Aufrufen wechselt."""
    kurz_vor_mitternacht = datetime(2026, 9, 15, 23, 50, tzinfo=BERLIN)
    nach_mitternacht = datetime(2026, 9, 16, 0, 30, tzinfo=BERLIN)
    req = request(
        tenant_id,
        call_id,
        idempotency_key="k-mitternacht",
        reserved_for=berlin(MITTWOCH, 18, 30),
    )

    first = create_reservation(session, req, now=kurz_vor_mitternacht)
    assert "morgen" in first.readback

    second = create_reservation(session, req, now=nach_mitternacht)
    assert second.readback == first.readback
    assert second == first


def test_parallele_anlagen_ueberbuchen_nicht(migrated_db_url):
    """Acht gleichzeitige Anfragen auf ein Fenster mit 40 Plätzen: nur vier dürfen durchkommen."""
    engine = create_engine(migrated_db_url)
    with Session(engine) as s:
        tenant = uuid.UUID(
            seed(s, tenant_name="Testbetrieb", timezone="Europe/Berlin").tenant_id
        )
        lift_party_limits(s, tenant)
        call = Call(
            tenant_id=tenant,
            external_session_id="ext",
            started_at=NOW,
            delete_after=DIENSTAG,
        )
        s.add(call)
        s.commit()
        call = call.id

    start = threading.Barrier(8)
    results: list[object] = []
    lock = threading.Lock()

    def book(n: int) -> None:
        start.wait(timeout=10)
        with Session(engine) as own:
            try:
                create_reservation(
                    own,
                    request(
                        tenant, call, idempotency_key=f"parallel-{n}", party_size=10
                    ),
                    now=NOW,
                )
                outcome: object = "ok"
            except Conflict as exc:
                outcome = exc
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=book, args=(n,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    with Session(engine) as s:
        booked = s.scalar(
            select(func.coalesce(func.sum(Reservation.party_size), 0)).where(
                Reservation.tenant_id == tenant
            )
        )
    engine.dispose()

    assert results.count("ok") == 4, f"erwartet 4 Buchungen, bekam {results}"
    assert booked == 40, f"Fenster mit 40 Plätzen ist mit {booked} Gästen überbucht"


def test_sperre_gilt_fuer_das_ganze_fenster_ueber_mitternacht(migrated_db_url):
    """Review PR #152: Montag 23:30 und Dienstag 00:30 liegen im selben Fenster
    18:00-01:00. Haelt ein Anruf die Sperre fuer Montag 23:30, muss ein zweiter fuer
    Dienstag 00:30 warten; mit einer Sperre je Kalendertag liefe er parallel und
    ueberbuchte das Fenster. Deterministisch statt mit Threads: B bekommt 300 ms."""
    engine = create_engine(migrated_db_url)
    montag = date(2026, 9, 14)
    with Session(engine) as s:
        tenant = uuid.UUID(
            seed(s, tenant_name="Testbetrieb", timezone="Europe/Berlin").tenant_id
        )
        s.add_all(
            [
                OpeningHours(
                    tenant_id=tenant,
                    weekday=0,
                    opens_at=time(18),
                    closes_at=time(1),
                    service="dinein",
                ),
                Capacity(
                    tenant_id=tenant,
                    weekday=0,
                    slot_start=time(18),
                    slot_end=time(1),
                    max_guests=20,
                ),
            ]
        )
        call = Call(
            tenant_id=tenant,
            external_session_id="ext",
            started_at=NOW,
            delete_after=DIENSTAG,
        )
        s.add(call)
        s.commit()
        call = call.id

    with Session(engine) as a, Session(engine) as b:
        # A prueft gerade Montag 23:30, die Transaktion ist offen.
        reservations_capacity.lock_capacity_days(a, tenant, montag)
        b.execute(text("SET LOCAL statement_timeout = 300"))
        with pytest.raises(OperationalError) as waited:
            create_reservation(
                b,
                request(
                    tenant,
                    call,
                    idempotency_key="nacht-b",
                    reserved_for=berlin(DIENSTAG, 0, 30),
                ),
                now=berlin(montag, 8),
            )
        # Genau das Warten auf die Sperre, nicht irgendein anderer DB-Fehler.
        assert isinstance(waited.value.orig, QueryCanceled)
        b.rollback()
        a.rollback()
    engine.dispose()


@pytest.mark.parametrize(
    ("day", "party_size", "heard"),
    [
        (DIENSTAG, 6, "sechs Personen noch heute"),  # same day, above 5
        (MITTWOCH, 9, "neun Personen kann ich"),  # any day, above 8
        (date(2026, 12, 15), 2, "höchstens 90 Tage im Voraus"),  # day 91
    ],
)
def test_beyond_the_limits_of_the_restaurant_nothing_is_drafted(
    session, tenant_id, call_id, day, party_size, heard
):
    wish = request(
        tenant_id, call_id, party_size=party_size, reserved_for=berlin(day, 18, 30)
    )
    with pytest.raises(Conflict) as exc:
        create_reservation(session, wish, now=NOW)
    assert heard in exc.value.say
    # A table for the team is told apart from a wish nobody can book.
    assert isinstance(exc.value, TeamDecides) is ("Personen" in heard)
    assert session.scalar(select(func.count()).select_from(Reservation)) == 0


@pytest.mark.parametrize(("day", "party_size"), [(DIENSTAG, 5), (MITTWOCH, 8)])
def test_at_the_limits_the_agent_drafts_the_table(
    session, tenant_id, call_id, day, party_size
):
    wish = request(
        tenant_id, call_id, party_size=party_size, reserved_for=berlin(day, 18, 30)
    )
    assert create_reservation(session, wish, now=NOW).party_size == party_size
