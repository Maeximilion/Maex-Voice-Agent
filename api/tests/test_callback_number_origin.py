"""A callback number the call did not come from is marked on the tablet (#252)."""

import uuid
from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from api.db import get_db
from api.domain.callbacks import list_open
from api.domain.callbacks.board import NUMBER_FROM_CALL, NUMBER_OTHER, NUMBER_WITHHELD
from api.main import app
from api.models import Call, Callback
from scripts.seed import seed

NOW = datetime(2026, 10, 12, 16, 0, tzinfo=UTC)
PHONE = "+4917612345678"
NOTE_OTHER = "Andere Nummer als der Anruf"
NOTE_WITHHELD = "Anruf ohne Rufnummer"


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
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.fixture
def tenant_id(engine) -> uuid.UUID:
    with Session(engine) as s:
        return uuid.UUID(
            seed(s, tenant_name="Testbetrieb", timezone="Europe/Berlin").tenant_id
        )


def callback(
    engine,
    tenant_id,
    *,
    caller_id: str | None,
    phone=PHONE,
    summary="Bitte zurueckrufen",
) -> None:
    with Session(engine) as s:
        call = Call(
            tenant_id=tenant_id,
            external_session_id=uuid.uuid4().hex,
            caller_id=caller_id,
            started_at=NOW,
            delete_after=date(2028, 10, 12),
        )
        s.add(call)
        s.flush()
        s.add(
            Callback(
                tenant_id=tenant_id,
                call_id=call.id,
                phone=phone,
                reason="human_requested",
                summary=summary,
            )
        )
        s.commit()


@pytest.mark.parametrize(
    ("caller_id", "origin"),
    [
        (PHONE, NUMBER_FROM_CALL),
        ("+4972215551234", NUMBER_OTHER),
        (None, NUMBER_WITHHELD),
        ("", NUMBER_WITHHELD),
        (" ", NUMBER_WITHHELD),
        # A caller ID the platform sent in a form that could not be normalized
        # is stored raw (calls/start.py) and backs no number.
        ("anonymous", NUMBER_OTHER),
    ],
)
def test_number_origin(engine, tenant_id, caller_id, origin):
    callback(engine, tenant_id, caller_id=caller_id)

    with Session(engine) as s:
        [cb] = list_open(s, tenant_id)

    assert cb.caller_id == caller_id and cb.number_origin == origin


def test_card_is_unmarked_for_the_callers_own_number(client, engine, tenant_id):
    callback(engine, tenant_id, caller_id=PHONE)

    body = client.get("/gui/fragments/rueckrufe").text

    assert f'href="tel:{PHONE}"' in body
    assert NOTE_OTHER not in body and NOTE_WITHHELD not in body
    assert "Anruf kam von" not in body


def test_card_marks_another_number_and_names_the_callers(client, engine, tenant_id):
    callback(engine, tenant_id, caller_id="+4972215551234")

    body = client.get("/gui/fragments/rueckrufe").text

    assert NOTE_OTHER in body and "Anruf kam von +4972215551234" in body
    # The button still calls the number the guest named.
    assert f'href="tel:{PHONE}"' in body


@pytest.mark.parametrize("caller_id", [None, " "])
def test_card_marks_a_withheld_caller(client, engine, tenant_id, caller_id):
    callback(engine, tenant_id, caller_id=caller_id)

    body = client.get("/gui/fragments/rueckrufe").text

    assert NOTE_WITHHELD in body and "Anruf kam von" not in body


def test_raw_caller_id_is_escaped_on_the_card(client, engine, tenant_id):
    callback(engine, tenant_id, caller_id="<script>alert(1)</script>")

    body = client.get("/gui/fragments/rueckrufe").text

    assert NOTE_OTHER in body and "&lt;script&gt;" in body
    assert "<script>alert(1)" not in body


def test_card_after_the_deletion_period_offers_no_call(client, engine, tenant_id):
    """T-11.2: 90 days after its creation an open callback has lost number and
    summary (api/jobs/retention.py). The card says so, offers no empty call
    link and no empty quote, and can still be closed."""
    callback(engine, tenant_id, caller_id=None, phone="", summary="")

    body = client.get("/gui/fragments/rueckrufe").text

    assert "Nummer nach der L&ouml;schfrist entfernt" in body
    assert 'href="tel:' not in body
    assert NOTE_WITHHELD not in body
    assert "&bdquo;" not in body
    assert "Erledigt" in body and "Will Mitarbeiter sprechen" in body
