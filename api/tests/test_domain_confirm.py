"""domain/confirm: draft nach confirmed, Idempotenz, Outbox-Eintrag und Grenzfälle."""

import threading
import uuid
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from api.core.errors import Conflict, NotFound
from api.domain.confirm import confirm
from api.domain.reservations import create_reservation
from api.models import AuditLog, Call, OutboxEvent, Reservation, ServiceConfig
from api.schemas.confirm import ConfirmRequest
from api.schemas.reservations import CreateReservationRequest
from scripts.seed import seed

BERLIN = ZoneInfo("Europe/Berlin")
DIENSTAG = date(2026, 9, 15)
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


def make_draft(session, tenant_id, call_id, at=None, **overrides) -> uuid.UUID:
    body = {
        "call_id": call_id,
        "tenant_id": tenant_id,
        "idempotency_key": uuid.uuid4().hex,
        "guest_name": "Müller",
        "phone": "+4972215551234",
        "party_size": 4,
        "reserved_for": berlin(DIENSTAG, 18, 30),
        "note": "Kinderstuhl",
        **overrides,
    }
    return create_reservation(
        session, CreateReservationRequest(**body), now=at or NOW
    ).reservation_id


def request(tenant_id, call_id, entity_id, **overrides) -> ConfirmRequest:
    return ConfirmRequest(
        **{
            "call_id": call_id,
            "tenant_id": tenant_id,
            "entity": "reservation",
            "entity_id": entity_id,
            "idempotency_key": uuid.uuid4().hex,
            **overrides,
        }
    )


def test_entwurf_wird_bestaetigt_mit_audit_und_outbox(session, tenant_id, call_id):
    draft_id = make_draft(session, tenant_id, call_id)

    result = confirm(session, request(tenant_id, call_id, draft_id))

    assert result.status == "confirmed"
    assert result.handover == "queued"
    assert result.pickup_code is None
    assert session.get(Reservation, draft_id).status == "confirmed"

    event = session.scalars(select(OutboxEvent)).one()
    assert event.event_type == "reservation.confirmed"
    assert event.status == "pending"
    assert event.tenant_id == tenant_id
    assert event.payload["reservation_id"] == str(draft_id)
    assert event.payload["party_size"] == 4
    assert event.payload["note"] == "Kinderstuhl"
    # In der Datenbank steht UTC (CLAUDE.md §8), 18:30 Berlin sind 16:30 UTC.
    assert event.payload["reserved_for"] == "2026-09-15T16:30:00+00:00"

    actions = [a.action for a in session.scalars(select(AuditLog)).all()]
    assert actions == ["reservation.draft_created", "reservation.confirmed"]


def test_zweiter_aufruf_ist_idempotent_und_legt_kein_zweites_ereignis_an(
    session, tenant_id, call_id
):
    draft_id = make_draft(session, tenant_id, call_id)
    first = confirm(session, request(tenant_id, call_id, draft_id))

    # Anderer Schlüssel, derselbe Vorgang: der Zustand entscheidet, nicht der Schlüssel.
    second = confirm(session, request(tenant_id, call_id, draft_id))

    assert second == first
    assert session.scalar(select(func.count()).select_from(OutboxEvent)) == 1
    assert (
        session.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.action == "reservation.confirmed")
        )
        == 1
    )


def test_stornierte_reservierung_ist_conflict(session, tenant_id, call_id):
    draft_id = make_draft(session, tenant_id, call_id)
    session.get(Reservation, draft_id).status = "cancelled"
    session.commit()

    with pytest.raises(Conflict) as exc:
        confirm(session, request(tenant_id, call_id, draft_id))
    assert exc.value.say is not None
    assert session.scalar(select(func.count()).select_from(OutboxEvent)) == 0


def test_weich_geloeschte_reservierung_ist_not_found(session, tenant_id, call_id):
    draft_id = make_draft(session, tenant_id, call_id)
    session.get(Reservation, draft_id).deleted_at = NOW
    session.commit()

    with pytest.raises(NotFound):
        confirm(session, request(tenant_id, call_id, draft_id))


def test_unbekannte_reservierung_ist_not_found(session, tenant_id, call_id):
    with pytest.raises(NotFound) as exc:
        confirm(session, request(tenant_id, call_id, uuid.uuid4()))
    assert exc.value.say is not None


def test_reservierung_eines_anderen_mandanten_ist_not_found(
    session, tenant_id, call_id
):
    draft_id = make_draft(session, tenant_id, call_id)
    other = uuid.UUID(
        seed(session, tenant_name="Anderer", timezone="Europe/Berlin").tenant_id
    )
    other_call = Call(
        tenant_id=other,
        external_session_id="ext-2",
        started_at=NOW,
        delete_after=DIENSTAG,
    )
    session.add(other_call)
    session.commit()

    with pytest.raises(NotFound):
        confirm(session, request(other, other_call.id, draft_id))
    assert session.get(Reservation, draft_id).status == "draft"


def test_entwurf_aus_einem_anderen_anruf_ist_not_found(session, tenant_id, call_id):
    """Das Ja gehört dem Gast in der Leitung: ein zweiter Anruf darf den Tisch
    des ersten nicht bestätigen, auch nicht im selben Betrieb."""
    draft_id = make_draft(session, tenant_id, call_id)
    zweiter_anruf = Call(
        tenant_id=tenant_id,
        external_session_id="ext-zweiter",
        started_at=NOW,
        delete_after=DIENSTAG,
    )
    session.add(zweiter_anruf)
    session.commit()

    with pytest.raises(NotFound):
        confirm(session, request(tenant_id, zweiter_anruf.id, draft_id))

    assert session.get(Reservation, draft_id).status == "draft"
    assert session.scalar(select(func.count()).select_from(OutboxEvent)) == 0


def test_unbekannter_anruf_ist_not_found(session, tenant_id, call_id):
    draft_id = make_draft(session, tenant_id, call_id)
    with pytest.raises(NotFound) as exc:
        confirm(session, request(tenant_id, uuid.uuid4(), draft_id))
    assert exc.value.say is not None


def test_unbekannter_mandant_ist_not_found(session, tenant_id, call_id):
    draft_id = make_draft(session, tenant_id, call_id)
    with pytest.raises(NotFound):
        confirm(session, request(uuid.uuid4(), call_id, draft_id))


# --- A draft that lost its seats is not confirmed over someone else (#250) ------


def _second_call(session, tenant_id) -> uuid.UUID:
    call = Call(
        tenant_id=tenant_id,
        external_session_id="ext-2",
        started_at=NOW,
        delete_after=DIENSTAG,
    )
    session.add(call)
    session.commit()
    return call.id


def _after_the_limit(session, tenant_id) -> datetime:
    limit = session.get(ServiceConfig, tenant_id).max_call_seconds
    return NOW + timedelta(seconds=limit + 1)


def test_a_full_table_confirms_itself(session, tenant_id, call_id):
    """The draft counts its own seats; confirming must not trip over them."""
    draft_id = make_draft(session, tenant_id, call_id, party_size=40)

    result = confirm(session, request(tenant_id, call_id, draft_id), now=NOW)

    assert result.status == "confirmed"


def test_an_old_draft_is_confirmed_while_its_seats_are_free(
    session, tenant_id, call_id
):
    """Past the limit the draft no longer holds the seats, but nobody took
    them: a late yes is still a yes."""
    draft_id = make_draft(session, tenant_id, call_id, party_size=40)

    late = _after_the_limit(session, tenant_id)
    result = confirm(session, request(tenant_id, call_id, draft_id), now=late)

    assert result.status == "confirmed"


def test_a_draft_whose_seats_went_to_someone_else_is_refused(
    session, tenant_id, call_id
):
    """The seats were given away once the draft stopped holding them. A yes
    that comes after must not put two parties on the same seats."""
    old = make_draft(session, tenant_id, call_id, party_size=40)
    late = _after_the_limit(session, tenant_id)
    other_call = _second_call(session, tenant_id)
    taken = make_draft(session, tenant_id, other_call, at=late, party_size=40)
    confirm(session, request(tenant_id, other_call, taken), now=late)

    with pytest.raises(Conflict) as refused:
        confirm(session, request(tenant_id, call_id, old), now=late)
    session.rollback()

    assert "vergeben" in refused.value.say
    assert session.get(Reservation, old).status == "draft"
    confirmed = session.scalars(select(OutboxEvent)).all()
    assert [e.payload["reservation_id"] for e in confirmed] == [str(taken)]


def test_two_old_drafts_for_the_same_seats_confirm_only_once(migrated_db_url):
    """Both drafts stopped holding the seats, both guests say yes at the same
    moment. Without the day lock both checks would see the window empty."""
    engine = create_engine(migrated_db_url)
    with Session(engine) as s:
        tenant = uuid.UUID(
            seed(s, tenant_name="Testbetrieb", timezone="Europe/Berlin").tenant_id
        )
        limit = timedelta(seconds=s.get(ServiceConfig, tenant).max_call_seconds + 1)
        calls = [_second_call(s, tenant), _second_call(s, tenant)]
        # The second draft is only possible because the first one has expired.
        first = make_draft(s, tenant, calls[0], party_size=40)
        second = make_draft(s, tenant, calls[1], at=NOW + limit, party_size=40)
    late = NOW + limit * 2

    start = threading.Barrier(2)
    results: list[object] = []
    lock = threading.Lock()

    def run(call_id: uuid.UUID, draft_id: uuid.UUID) -> None:
        start.wait(timeout=10)
        with Session(engine) as own:
            try:
                confirm(own, request(tenant, call_id, draft_id), now=late)
                outcome: object = "ok"
            except Conflict:
                own.rollback()
                outcome = "refused"
            except Exception as exc:  # noqa: BLE001 - im Test soll jeder Fehler auffallen
                outcome = exc
        with lock:
            results.append(outcome)

    threads = [
        threading.Thread(target=run, args=pair)
        for pair in zip(calls, (first, second), strict=True)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    with Session(engine) as s:
        statuses = sorted(s.get(Reservation, d).status for d in (first, second))
    engine.dispose()

    assert sorted(map(str, results)) == ["ok", "refused"], results
    assert statuses == ["confirmed", "draft"]


def test_parallele_bestaetigung_legt_nur_ein_ereignis_an(migrated_db_url):
    """Acht gleichzeitige confirm-Aufrufe auf denselben Entwurf: ein Ereignis, ein Audit."""
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
        call_id = call.id
        draft_id = make_draft(s, tenant, call_id)

    start = threading.Barrier(8)
    results: list[object] = []
    lock = threading.Lock()

    def run(n: int) -> None:
        start.wait(timeout=10)
        with Session(engine) as own:
            try:
                confirm(own, request(tenant, call_id, draft_id))
                outcome: object = "ok"
            except Exception as exc:  # noqa: BLE001 - im Test soll jeder Fehler auffallen
                outcome = exc
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=run, args=(n,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    with Session(engine) as s:
        events = s.scalar(select(func.count()).select_from(OutboxEvent))
        audits = s.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.action == "reservation.confirmed")
        )
    engine.dispose()

    assert results.count("ok") == 8, f"alle Aufrufe sollen bestätigen, bekam {results}"
    assert events == 1, f"erwartet ein Ereignis, bekam {events}"
    assert audits == 1, f"erwartet einen Audit-Eintrag, bekam {audits}"
