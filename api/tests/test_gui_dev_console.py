"""gui: simulator console in the browser, ENV=dev only (T-2.5, docs/11 §gui)."""

import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from api.config import settings
from api.db import get_db
from api.gui import dev, mount_gui
from api.main import app
from api.models import Call
from api.tests.test_domain_draft_order import _tenant

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

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_db, None)
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


def test_unknown_call_is_a_message_not_a_crash(client, tenant_id):
    said = client.post(
        f"{BASE}/call/00000000-0000-0000-0000-000000000000/say",
        headers=HX,
        data={"text": "Hallo"},
    )

    assert said.status_code == 404
    assert "Anruf" in said.text


def test_missing_tenant_is_said_in_words(client):
    started = client.post(f"{BASE}/call", headers=HX)

    assert started.status_code == 503
    assert "make seed" in started.text


def test_writes_need_the_htmx_header(client, tenant_id):
    assert client.post(f"{BASE}/call").status_code == 403


def test_console_is_absent_outside_dev(monkeypatch):
    monkeypatch.setattr(settings, "env", "prod")
    prod = FastAPI()

    mount_gui(prod)

    assert TestClient(prod).get(BASE).status_code == 404
