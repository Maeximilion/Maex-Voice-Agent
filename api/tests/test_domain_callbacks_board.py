"""domain/callbacks/board: offene Rückrufe lesen, erledigen, Fingerabdruck (T-3.4)."""

import threading
import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session

from api.core.errors import NotFound
from api.domain.callbacks import list_open, mark_done, open_change_token
from api.models import AuditLog, Call, Callback
from scripts.seed import seed

NOW = datetime(2026, 9, 18, 16, 0, tzinfo=UTC)


@pytest.fixture
def engine(migrated_db_url):
    engine = create_engine(migrated_db_url)
    yield engine
    engine.dispose()


@pytest.fixture
def session(engine):
    with Session(engine) as s:
        yield s


@pytest.fixture
def tenant_id(session) -> uuid.UUID:
    return uuid.UUID(
        seed(session, tenant_name="Testbetrieb", timezone="Europe/Berlin").tenant_id
    )


def rueckruf(session, tenant_id, *, reason="not_understood", minutes=0, **kw):
    call = Call(
        tenant_id=tenant_id,
        external_session_id=uuid.uuid4().hex,
        started_at=NOW,
        delete_after=date(2028, 9, 18),
    )
    session.add(call)
    session.flush()
    cb = Callback(
        tenant_id=tenant_id,
        call_id=call.id,
        phone="+4972215551234",
        reason=reason,
        summary="Moechte zurueckgerufen werden.",
        created_at=NOW + timedelta(minutes=minutes),
        **kw,
    )
    session.add(cb)
    session.commit()
    return cb.id


def test_beschwerden_oben_dann_der_aelteste(session, tenant_id):
    spaet = rueckruf(session, tenant_id, minutes=10)
    frueh = rueckruf(session, tenant_id, minutes=0)
    beschwerde = rueckruf(session, tenant_id, reason="complaint", minutes=20)

    ids = [c.callback_id for c in list_open(session, tenant_id)]

    assert ids == [beschwerde, frueh, spaet]


def test_erledigte_und_geloeschte_fehlen(session, tenant_id):
    offen = rueckruf(session, tenant_id)
    rueckruf(session, tenant_id, status="done")
    rueckruf(session, tenant_id, deleted_at=NOW)

    assert [c.callback_id for c in list_open(session, tenant_id)] == [offen]


def test_fremder_mandant_bleibt_draussen(session, tenant_id):
    rueckruf(session, tenant_id)

    assert list_open(session, uuid.uuid4()) == []


def test_erledigt_setzt_zustand_und_audit(session, tenant_id):
    cb_id = rueckruf(session, tenant_id, reason="complaint")

    cb = mark_done(session, tenant_id, cb_id)

    assert cb.status == "done" and cb.done_by == "gui:tablet" and cb.done_at
    [eintrag] = session.scalars(
        select(AuditLog).where(AuditLog.action == "callback.done")
    ).all()
    assert eintrag.entity_id == cb_id and eintrag.payload["reason"] == "complaint"
    assert list_open(session, tenant_id) == []


def test_doppelt_erledigt_schreibt_einmal(session, tenant_id):
    cb_id = rueckruf(session, tenant_id)
    erstes = mark_done(session, tenant_id, cb_id).done_at

    assert mark_done(session, tenant_id, cb_id).done_at == erstes
    anzahl = session.scalar(
        select(func.count(AuditLog.id)).where(AuditLog.action == "callback.done")
    )
    assert anzahl == 1


def test_zwei_tablets_gleichzeitig_ein_eintrag(engine, session, tenant_id):
    cb_id = rueckruf(session, tenant_id)
    barrier = threading.Barrier(2)

    def tap():
        with Session(engine) as s:
            barrier.wait()
            mark_done(s, tenant_id, cb_id)

    threads = [threading.Thread(target=tap) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    anzahl = session.scalar(
        select(func.count(AuditLog.id)).where(AuditLog.action == "callback.done")
    )
    assert anzahl == 1


@pytest.mark.parametrize("fremd", ["id", "mandant", "geloescht"])
def test_unbekannter_rueckruf_klare_meldung(session, tenant_id, fremd):
    cb_id = rueckruf(
        session, tenant_id, deleted_at=NOW if fremd == "geloescht" else None
    )
    ziel_tenant = uuid.uuid4() if fremd == "mandant" else tenant_id
    ziel_id = uuid.uuid4() if fremd == "id" else cb_id

    with pytest.raises(NotFound):
        mark_done(session, ziel_tenant, ziel_id)


def test_token_folgt_der_menge_auch_bei_rohem_update(session, tenant_id):
    """Wie an der Kopfzeile (Codex PR #111): nicht allein auf updated_at verlassen."""
    leer = open_change_token(session, tenant_id)
    cb_id = rueckruf(session, tenant_id)
    eins = open_change_token(session, tenant_id)
    assert eins != leer

    session.execute(
        text("UPDATE callbacks SET status = 'done' WHERE id = :id"), {"id": cb_id}
    )
    session.commit()

    assert open_change_token(session, tenant_id) == leer
