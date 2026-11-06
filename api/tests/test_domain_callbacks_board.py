"""domain/callbacks/board: offene Rückrufe lesen, erledigen, Fingerabdruck (T-3.4)."""

import threading
import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session

from api.core.errors import NotFound
from api.domain.callbacks import (
    list_history,
    list_open,
    mark_done,
    open_change_token,
    reopen,
)
from api.domain.callbacks.board import HISTORY_DAYS
from api.jobs.retention import purge
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


def test_token_changes_when_the_deletion_job_empties_a_card(session, tenant_id):
    """Codex PR #371: the deletion job (T-11.2) clears number and summary of an
    open callback without changing the set of open ids. The tablet must still
    reload the card, or it keeps showing the deleted number."""
    cb_id = rueckruf(session, tenant_id)
    before = open_change_token(session, tenant_id)

    session.execute(
        text("UPDATE callbacks SET phone = '', summary = '' WHERE id = :id"),
        {"id": cb_id},
    )
    session.commit()

    after = open_change_token(session, tenant_id)
    assert after != before
    assert open_change_token(session, tenant_id) == after


# --- History and reopen (T-3.15, #516) ---

WINDOW = timedelta(days=HISTORY_DAYS)


def erledigt(session, tenant_id, done_at, **kw):
    return rueckruf(
        session, tenant_id, status="done", done_by="gui:tablet", done_at=done_at, **kw
    )


def reopened_entries(session):
    return session.scalars(
        select(AuditLog).where(AuditLog.action == "callback.reopened")
    ).all()


def test_history_holds_both_sides_of_the_window(session, tenant_id):
    inside = erledigt(session, tenant_id, NOW - WINDOW + timedelta(minutes=1))
    newest = erledigt(session, tenant_id, NOW - timedelta(minutes=5))
    erledigt(session, tenant_id, NOW - WINDOW - timedelta(minutes=1))

    rows = list_history(session, tenant_id, now=NOW)

    assert [r.callback_id for r in rows] == [newest, inside]
    assert rows[0].done_by == "gui:tablet" and rows[0].phone == "+4972215551234"


def test_history_leaves_out_open_deleted_and_other_tenants(session, tenant_id):
    rueckruf(session, tenant_id)
    erledigt(session, tenant_id, NOW, deleted_at=NOW)
    erledigt(session, tenant_id, NOW - timedelta(hours=1))

    assert len(list_history(session, tenant_id, now=NOW)) == 1
    assert list_history(session, uuid.uuid4(), now=NOW) == []


def test_reopen_brings_the_card_back_with_an_audit_entry(session, tenant_id):
    done_at = NOW - timedelta(hours=2)
    cb_id = erledigt(session, tenant_id, done_at, reason="complaint")
    before = open_change_token(session, tenant_id)

    cb = reopen(session, tenant_id, cb_id, now=NOW)

    assert cb.status == "open" and cb.done_at is None and cb.done_by is None
    assert [c.callback_id for c in list_open(session, tenant_id)] == [cb_id]
    assert list_history(session, tenant_id, now=NOW) == []
    assert open_change_token(session, tenant_id) != before
    [entry] = reopened_entries(session)
    assert entry.entity_id == cb_id and entry.actor == "gui:tablet"
    assert entry.payload["reason"] == "complaint"
    assert entry.payload["done_by"] == "gui:tablet"
    assert datetime.fromisoformat(entry.payload["done_at"]) == done_at


def test_reopen_twice_writes_once(session, tenant_id):
    cb_id = erledigt(session, tenant_id, NOW - timedelta(hours=1))

    reopen(session, tenant_id, cb_id, now=NOW)
    reopen(session, tenant_id, cb_id, now=NOW)

    assert len(reopened_entries(session)) == 1


def test_reopen_outside_the_window_changes_nothing(session, tenant_id):
    """A sheet left open past the window, or a forged request, cannot bring an
    old number back: the list stays as short as L8 wants it."""
    cb_id = erledigt(session, tenant_id, NOW - WINDOW - timedelta(minutes=1))

    cb = reopen(session, tenant_id, cb_id, now=NOW)

    assert cb.status == "done" and cb.done_at is not None
    assert reopened_entries(session) == []


def test_reopen_of_an_unknown_or_foreign_callback_is_not_found(session, tenant_id):
    cb_id = erledigt(session, tenant_id, NOW)

    with pytest.raises(NotFound):
        reopen(session, uuid.uuid4(), cb_id, now=NOW)
    with pytest.raises(NotFound):
        reopen(session, tenant_id, uuid.uuid4(), now=NOW)
    assert session.get(Callback, cb_id).status == "done"


def test_history_row_keeps_no_number_the_deletion_job_removed(session, tenant_id):
    """Open for 100 days, the job emptied it (T-11.2), then the team marked it
    done: the history shows it without a number and the job leaves it so."""
    cb_id = rueckruf(session, tenant_id, minutes=-100 * 24 * 60)
    purge(session, NOW)
    session.get(Callback, cb_id).status = "done"
    session.get(Callback, cb_id).done_at = NOW - timedelta(hours=1)
    session.commit()

    [row] = list_history(session, tenant_id, now=NOW)

    assert row.callback_id == cb_id and row.phone == "" and row.summary == ""


def test_reopened_old_card_falls_back_to_its_creation_date(session, tenant_id):
    """Reopening clears done_at, so the deletion period runs from the creation
    date again: a card older than the period loses its number on the next run,
    while a fresh one keeps it."""
    old = erledigt(session, tenant_id, NOW - timedelta(days=1), minutes=-100 * 24 * 60)
    fresh = erledigt(session, tenant_id, NOW - timedelta(days=1))
    purge(session, NOW)
    assert session.get(Callback, old).phone != ""

    reopen(session, tenant_id, old, now=NOW)
    reopen(session, tenant_id, fresh, now=NOW)
    purge(session, NOW)
    session.expire_all()

    assert session.get(Callback, old).phone == ""
    assert session.get(Callback, fresh).phone == "+4972215551234"
