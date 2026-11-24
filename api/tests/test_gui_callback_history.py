"""GUI: the history sheet of done callbacks and "Wieder öffnen" (T-3.15, #516)."""

import json
import re
import uuid
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from api.core.time import utcnow
from api.db import get_db
from api.domain.callbacks.board import CALL_HAS_AN_OPEN_CARD, HISTORY_DAYS
from api.main import app
from api.models import AuditLog, Call, Callback
from scripts.seed import seed

HX = {"HX-Request": "true", "X-CSRF-Token": "testtoken"}
PHONE = "+4972215551234"


@pytest.fixture
def engine(migrated_db_url):
    engine = create_engine(migrated_db_url)
    yield engine
    engine.dispose()


@pytest.fixture
def client(engine):
    def override_get_db():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    try:
        c = TestClient(app)
        c.cookies.set("maex_csrf", "testtoken")
        yield c
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.fixture
def tenant_id(engine) -> uuid.UUID:
    with Session(engine) as s:
        return uuid.UUID(
            seed(s, tenant_name="Testbetrieb", timezone="Europe/Berlin").tenant_id
        )


def done_callback(
    engine, tenant_id, *, phone=PHONE, done_ago=timedelta(minutes=5), **more
):
    return a_callback(
        engine,
        tenant_id,
        phone=phone,
        status="done",
        done_by="gui:team",
        done_at=utcnow() - done_ago,
        **more,
    )


def a_callback(engine, tenant_id, *, phone=PHONE, **fields):
    with Session(engine) as s:
        call = Call(
            tenant_id=tenant_id,
            external_session_id=uuid.uuid4().hex,
            started_at=utcnow(),
            delete_after=date(2099, 1, 1),
        )
        s.add(call)
        s.flush()
        cb = Callback(
            tenant_id=tenant_id,
            call_id=call.id,
            phone=phone,
            reason="not_understood",
            summary="Bitte zurueckrufen wegen Tisch.",
            **fields,
        )
        s.add(cb)
        s.commit()
        return cb.id


def test_the_column_offers_the_history(client, tenant_id):
    body = client.get("/gui/fragments/rueckrufe").text
    assert 'hx-get="/gui/rueckrufe/verlauf"' in body
    assert "Verlauf" in body


def test_the_sheet_lists_a_done_callback_with_both_buttons(client, engine, tenant_id):
    cb_id = done_callback(engine, tenant_id)
    body = client.get("/gui/rueckrufe/verlauf").text
    assert PHONE in body
    assert f'href="tel:{PHONE}"' in body
    assert f'hx-post="/gui/rueckrufe/{cb_id}/wieder-offen"' in body
    assert "Wieder &ouml;ffnen" in body
    assert "Erledigt " in body
    assert "Bitte zurueckrufen wegen Tisch." in body


def test_a_done_mark_of_an_earlier_day_names_the_date(client, engine, tenant_id):
    done_callback(engine, tenant_id, done_ago=timedelta(days=2))
    body = client.get("/gui/rueckrufe/verlauf").text
    # A date such as "04.11." precedes the clock time.
    assert re.search(r"Erledigt \d{2}\.\d{2}\. \d{2}:\d{2}", body)


# The clock time of the call on a card, with the date in front when it was not
# today: "Annahme 22:40" of last night must not read as this morning (#926).
DATED = re.compile(r'class="time">\d{2}\.\d{2}\. \d{2}:\d{2}<')
UNDATED = re.compile(r'class="time">\d{2}:\d{2}<')


def test_an_open_card_from_the_day_before_names_its_date(client, engine, tenant_id):
    a_callback(engine, tenant_id, created_at=utcnow() - timedelta(hours=25))
    body = client.get("/gui/fragments/rueckrufe").text
    assert DATED.search(body)
    assert not UNDATED.search(body)


def test_an_open_card_from_today_shows_the_clock_time_only(client, engine, tenant_id):
    a_callback(engine, tenant_id)
    body = client.get("/gui/fragments/rueckrufe").text
    assert UNDATED.search(body)
    assert not DATED.search(body)


def test_a_history_card_names_the_date_its_call_came_in(client, engine, tenant_id):
    """The history shares the card: a call of two days ago, done today."""
    # Done this instant: five minutes ago could be yesterday just after midnight.
    done_callback(
        engine,
        tenant_id,
        created_at=utcnow() - timedelta(days=2),
        done_ago=timedelta(0),
    )
    body = client.get("/gui/rueckrufe/verlauf").text
    assert DATED.search(body)
    assert re.search(r"Erledigt \d{2}:\d{2}", body)


def test_a_removed_number_shows_the_note_and_no_call_button(client, engine, tenant_id):
    cb_id = done_callback(engine, tenant_id, phone="")
    body = client.get("/gui/rueckrufe/verlauf").text
    assert "Nummer nach der L&ouml;schfrist entfernt" in body
    assert "tel:" not in body
    assert "Anrufen" not in body
    assert f"/gui/rueckrufe/{cb_id}/wieder-offen" in body


def test_an_empty_history_says_so_with_the_window(client, tenant_id):
    body = client.get("/gui/rueckrufe/verlauf").text
    assert (
        f"Keine erledigten R&uuml;ckrufe in den letzten {HISTORY_DAYS} Tagen." in body
    )


def test_reopening_returns_the_card_and_audits_once(client, engine, tenant_id):
    cb_id = done_callback(engine, tenant_id)
    response = client.post(f"/gui/rueckrufe/{cb_id}/wieder-offen", headers=HX)
    assert response.status_code == 200
    assert "Keine erledigten" in response.text
    assert PHONE in client.get("/gui/fragments/rueckrufe").text
    with Session(engine) as s:
        callback = s.get(Callback, cb_id)
        assert callback is not None
        assert callback.status == "open"
        count = s.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.action == "callback.reopened")
        )
    assert count == 1


def test_reopening_twice_audits_once(client, engine, tenant_id):
    cb_id = done_callback(engine, tenant_id)
    for _ in range(2):
        assert (
            client.post(f"/gui/rueckrufe/{cb_id}/wieder-offen", headers=HX).status_code
            == 200
        )
    with Session(engine) as s:
        count = s.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.action == "callback.reopened")
        )
    assert count == 1


def test_an_unknown_callback_is_no_error(client, tenant_id):
    response = client.post(f"/gui/rueckrufe/{uuid.uuid4()}/wieder-offen", headers=HX)
    assert response.status_code == 200
    assert "Keine erledigten" in response.text


def test_reopening_needs_the_operations_view(client, engine, tenant_id):
    cb_id = done_callback(engine, tenant_id)
    assert client.post(f"/gui/rueckrufe/{cb_id}/wieder-offen").status_code == 403


def test_reopening_next_to_an_open_card_of_its_call_says_why(client, engine, tenant_id):
    """One open card per call (#981): the done card stays in the history and
    the board says where the open one is."""
    cb_id = done_callback(engine, tenant_id)
    with Session(engine) as s:
        done = s.get(Callback, cb_id)
        s.add(
            Callback(
                tenant_id=tenant_id,
                call_id=done.call_id,
                phone=PHONE,
                reason="not_understood",
                summary="Die neuere Karte.",
            )
        )
        s.commit()

    response = client.post(f"/gui/rueckrufe/{cb_id}/wieder-offen", headers=HX)

    assert response.status_code == 200
    notice = json.loads(response.headers["HX-Trigger"])["callback-notice"]
    assert notice == CALL_HAS_AN_OPEN_CARD
    assert "schon eine Karte offen" in notice
    with Session(engine) as s:
        assert s.get(Callback, cb_id).status == "done"
