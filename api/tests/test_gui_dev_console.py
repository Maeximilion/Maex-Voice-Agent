"""gui: simulator console in the browser, ENV=dev only (T-2.5, docs/11 §gui)."""

import re
import threading
from zoneinfo import ZoneInfoNotFoundError

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select, update
from sqlalchemy.orm import Session

from api.config import settings
from api.db import get_db
from api.gui import dev, mount_gui
from api.models import Call, Tenant
from api.tests.test_domain_draft_order import _tenant
from sim.session import Turn

HX = {"HX-Request": "true"}
BASE = "/gui/dev/console"


@pytest.fixture
def engine(migrated_db_url):
    engine = create_engine(migrated_db_url)
    yield engine
    engine.dispose()


@pytest.fixture
def db(engine):
    with Session(engine) as s:
        yield s


@pytest.fixture
def client(engine, monkeypatch):
    # The console keeps its own session per open call; point it at the test database.
    monkeypatch.setattr(dev, "SessionLocal", lambda: Session(engine))

    def override_get_db():
        with Session(engine) as session:
            yield session

    # CI runs with ENV=test, where the global app has no console: build one in dev.
    monkeypatch.setattr(settings, "env", "dev")
    app = FastAPI()
    mount_gui(app)
    app.dependency_overrides[get_db] = override_get_db
    try:
        yield TestClient(app)
    finally:
        for entry in dev.OPEN_CALLS.values():
            entry.session.close()
        dev.OPEN_CALLS.clear()


@pytest.fixture
def tenant_id(db):
    return _tenant(db, "Testbetrieb")


def _call_id(html: str) -> str:
    match = re.search(r'data-call="([0-9a-f-]{36})"', html)
    assert match, html
    return match.group(1)


def test_page_has_a_start_button(client):
    page = client.get(BASE)

    assert page.status_code == 200
    assert f'hx-post="{BASE}/call"' in page.text


def test_conversation_is_shown_and_ends_in_the_call_log(client, db, tenant_id):
    started = client.post(f"{BASE}/call", headers=HX)
    assert started.status_code == 200
    call_id = _call_id(started.text)

    said = client.post(
        f"{BASE}/call/{call_id}/say", headers=HX, data={"text": "Guten Tag"}
    )
    assert said.status_code == 200
    assert "Guten Tag" in said.text
    assert "Agent:" in said.text

    ended = client.post(f"{BASE}/call/{call_id}/end", headers=HX)
    assert ended.status_code == 200
    assert "Anruf beendet" in ended.text
    row = db.scalar(select(Call).where(Call.id == call_id))
    db.refresh(row)
    assert row.ended_at is not None
    assert call_id not in dev.OPEN_CALLS


def test_tool_calls_are_listed_with_duration(client, tenant_id):
    call_id = _call_id(client.post(f"{BASE}/call", headers=HX).text)

    said = client.post(
        f"{BASE}/call/{call_id}/say",
        headers=HX,
        data={"text": "Ich möchte einen Tisch für vier Personen morgen um 19 Uhr"},
    )

    assert "tool " in said.text
    assert " ms" in said.text


def test_empty_sentence_changes_nothing(client, tenant_id):
    call_id = _call_id(client.post(f"{BASE}/call", headers=HX).text)

    said = client.post(f"{BASE}/call/{call_id}/say", headers=HX, data={"text": "  "})

    assert said.status_code == 200
    assert "Agent:" not in said.text


def test_unknown_call_is_a_swappable_message(client, tenant_id):
    said = client.post(
        f"{BASE}/call/00000000-0000-0000-0000-000000000000/say",
        headers=HX,
        data={"text": "Hallo"},
    )

    # 200, not 404: htmx does not swap a 4xx and the page would show nothing.
    assert said.status_code == 200
    assert "nicht mehr offen" in said.text
    assert f'hx-post="{BASE}/call"' in said.text


def test_missing_tenant_is_said_in_words(client):
    started = client.post(f"{BASE}/call", headers=HX)

    assert started.status_code == 200
    assert "make seed" in started.text


def test_closing_turn_finishes_the_call(client, db, tenant_id, monkeypatch):
    call_id = _call_id(client.post(f"{BASE}/call", headers=HX).text)
    monkeypatch.setattr(
        dev.SimCall, "say", lambda self, text: Turn(customer=text, ended=True)
    )

    said = client.post(
        f"{BASE}/call/{call_id}/say", headers=HX, data={"text": "Ja, danke"}
    )

    assert "Anruf beendet" in said.text
    assert 'name="text"' not in said.text
    assert call_id not in dev.OPEN_CALLS
    row = db.scalar(select(Call).where(Call.id == call_id))
    db.refresh(row)
    assert row.ended_at is not None


def test_new_call_hangs_up_a_call_the_page_lost(client, db, tenant_id):
    first = _call_id(client.post(f"{BASE}/call", headers=HX).text)
    second = _call_id(client.post(f"{BASE}/call", headers=HX).text)

    assert list(dev.OPEN_CALLS) == [second]
    row = db.scalar(select(Call).where(Call.id == first))
    db.refresh(row)
    assert row.ended_at is not None


def test_hang_up_waits_for_a_running_turn(client, db, tenant_id, monkeypatch):
    call_id = _call_id(client.post(f"{BASE}/call", headers=HX).text)
    running, release = threading.Event(), threading.Event()

    def slow_say(self, text):
        running.set()
        assert release.wait(5)
        return Turn(customer=text, say=["Antwort"])

    monkeypatch.setattr(dev.SimCall, "say", slow_say)
    results = {}

    def say():
        results["say"] = client.post(
            f"{BASE}/call/{call_id}/say", headers=HX, data={"text": "Hallo"}
        )

    def hang_up():
        results["end"] = client.post(f"{BASE}/call/{call_id}/end", headers=HX)

    t1 = threading.Thread(target=say)
    t1.start()
    assert running.wait(5)
    t2 = threading.Thread(target=hang_up)
    t2.start()
    t2.join(0.5)
    assert (
        t2.is_alive()
    )  # the hang-up waits, it does not close the session under the turn
    release.set()
    t1.join(5)
    t2.join(5)

    assert "Antwort" in results["say"].text
    assert "Anruf beendet" in results["end"].text
    assert call_id not in dev.OPEN_CALLS


def test_failed_hang_up_keeps_the_call_for_a_retry(client, db, tenant_id, monkeypatch):
    call_id = _call_id(client.post(f"{BASE}/call", headers=HX).text)
    real_finish = dev.SimCall.finish
    calls = []

    def flaky_finish(self):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("database gone")
        return real_finish(self)

    monkeypatch.setattr(dev.SimCall, "finish", flaky_finish)

    with pytest.raises(RuntimeError):
        client.post(f"{BASE}/call/{call_id}/end", headers=HX)
    assert call_id in dev.OPEN_CALLS

    retry = client.post(f"{BASE}/call/{call_id}/end", headers=HX)

    assert "Anruf beendet" in retry.text
    assert call_id not in dev.OPEN_CALLS


def test_two_starts_at_once_leave_one_call(client, db, tenant_id, monkeypatch):
    inside, release = threading.Event(), threading.Event()
    real = dev.resolve_tenant

    def slow_resolve(session, name):
        inside.set()
        assert release.wait(5)
        return real(session, name)

    monkeypatch.setattr(dev, "resolve_tenant", slow_resolve)
    t1 = threading.Thread(target=lambda: client.post(f"{BASE}/call", headers=HX))
    t2 = threading.Thread(target=lambda: client.post(f"{BASE}/call", headers=HX))
    t1.start()
    assert inside.wait(5)
    t2.start()
    t2.join(0.5)
    assert t2.is_alive()  # the second start waits for the first
    release.set()
    t1.join(5)
    t2.join(5)

    assert len(dev.OPEN_CALLS) == 1


def test_failed_turn_ends_the_call_and_says_so(client, db, tenant_id, monkeypatch):
    call_id = _call_id(client.post(f"{BASE}/call", headers=HX).text)

    def broken(self, text):
        raise RuntimeError("database gone")

    monkeypatch.setattr(dev.SimCall, "say", broken)

    said = client.post(f"{BASE}/call/{call_id}/say", headers=HX, data={"text": "Hallo"})

    assert said.status_code == 200
    assert "fehlgeschlagen" in said.text
    assert call_id not in dev.OPEN_CALLS
    row = db.scalar(select(Call).where(Call.id == call_id))
    db.refresh(row)
    assert row.ended_at is not None


def test_failed_setup_leaves_no_open_call_row(client, db, tenant_id):
    db.execute(update(Tenant).values(timezone="Europe/Berln"))
    db.commit()

    with pytest.raises(ZoneInfoNotFoundError):
        client.post(f"{BASE}/call", headers=HX)

    assert db.scalar(select(func.count()).select_from(Call)) == 0
    assert not dev.OPEN_CALLS


def test_start_drops_a_stale_call_that_cannot_be_finished(
    client, tenant_id, monkeypatch
):
    stale = _call_id(client.post(f"{BASE}/call", headers=HX).text)

    def broken(self):
        raise RuntimeError("database gone")

    monkeypatch.setattr(dev.SimCall, "finish", broken)

    started = client.post(f"{BASE}/call", headers=HX)

    assert started.status_code == 200
    assert stale not in dev.OPEN_CALLS
    assert len(dev.OPEN_CALLS) == 1


def test_page_shows_a_failed_tap(client):
    page = client.get(BASE)

    assert "htmx:responseError" in page.text
    assert 'id="stoerung"' in page.text


def test_writes_need_the_htmx_header(client, tenant_id):
    assert client.post(f"{BASE}/call").status_code == 403


def test_console_is_absent_outside_dev(monkeypatch):
    monkeypatch.setattr(settings, "env", "prod")
    prod = FastAPI()

    mount_gui(prod)

    assert TestClient(prod).get(BASE).status_code == 404
