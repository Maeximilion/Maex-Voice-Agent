"""domain/reservations: create_reservation als Entwurf mit readback, Idempotenz und Grenzfällen."""

import threading
import uuid
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from api.core.errors import Conflict, InvalidInput, NotFound
from api.domain.reservations import create_reservation
from api.models import AuditLog, Call, Reservation
from api.schemas.reservations import CreateReservationRequest
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


def test_voller_slot_ist_conflict_mit_alternativen(session, tenant_id, call_id):
    create_reservation(session, request(tenant_id, call_id, party_size=40), now=NOW)
    with pytest.raises(Conflict) as exc:
        create_reservation(session, request(tenant_id, call_id, party_size=1), now=NOW)
    assert "halb sieben ist leider voll" in exc.value.say
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
    create_reservation(session, request(tenant_id, call_id, party_size=39), now=NOW)
    create_reservation(session, request(tenant_id, call_id, party_size=1), now=NOW)
    with pytest.raises(Conflict):
        create_reservation(session, request(tenant_id, call_id, party_size=1), now=NOW)


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
