"""The call list (T-8.7, docs/06 §4): the domain rows and the page "Anrufe"."""

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from api.core.time import business_day
from api.db import get_db
from api.domain.calls.log import list_calls
from api.main import app
from api.models import Call
from api.tests.test_gui_figures import _tenant

BERLIN = ZoneInfo("Europe/Berlin")


@pytest.fixture
def now(monkeypatch):
    """12:00 local of the running business day, fixed for setup and request."""
    noon = datetime.combine(
        business_day(tz_name="Europe/Berlin"), time(12, 0), tzinfo=BERLIN
    ).astimezone(UTC)
    monkeypatch.setattr("api.core.time.utcnow", lambda: noon)
    return noon


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
def client(engine):
    def override_get_db():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_db, None)


def _call(db, tenant, at, **more) -> Call:
    values = {
        "ended_at": at + timedelta(seconds=134),
        "duration_seconds": 134,
        "outcome": "completed",
        "intent": "pickup",
        **more,
    }
    call = Call(
        tenant_id=tenant.id,
        external_session_id=f"s-{at.isoformat()}",
        started_at=at,
        delete_after=at.date(),
        **values,
    )
    db.add(call)
    db.commit()
    return call


def test_the_calls_of_a_business_day_newest_first(db, now):
    tenant = _tenant(db, "Eins")
    _call(db, tenant, now - timedelta(hours=2), caller_id="+4917655512345")
    _call(db, tenant, now - timedelta(hours=1), cost_cents=29, tool_calls=[{}, {}])
    _call(db, tenant, now - timedelta(days=1))

    rows = list_calls(db, tenant, business_day(now, tenant.timezone))

    assert [row.started_at for row in rows] == [
        now - timedelta(hours=1),
        now - timedelta(hours=2),
    ]
    assert (rows[0].cost_cents, rows[0].tool_calls) == (29, 2)
    assert rows[1].caller_id == "+4917655512345"


def test_the_list_filters_by_outcome_and_intent(db, now):
    tenant = _tenant(db, "Eins")
    today = business_day(now, tenant.timezone)
    _call(db, tenant, now - timedelta(hours=3), outcome="abandoned", intent=None)
    _call(db, tenant, now - timedelta(hours=2), outcome="callback", intent="complaint")
    _call(db, tenant, now - timedelta(hours=1))

    assert len(list_calls(db, tenant, today, outcome="callback")) == 1
    assert len(list_calls(db, tenant, today, intent="pickup")) == 1
    assert list_calls(db, tenant, today, outcome="callback", intent="pickup") == []


def test_another_restaurants_calls_are_never_listed(db, now):
    one = _tenant(db, "Eins")
    two = _tenant(db, "Zwei")
    _call(db, two, now - timedelta(hours=1))

    assert list_calls(db, one, business_day(now, one.timezone)) == []


def test_the_page_lists_a_call_as_the_mockup_does(db, client, now):
    tenant = _tenant(db, "Eins")
    at = now - timedelta(hours=1)
    _call(db, tenant, at, caller_id="+4917655512345", cost_cents=29, tool_calls=[{}])

    response = client.get("/gui/anrufe")

    assert response.status_code == 200
    html = response.text
    assert "1 Anruf heute" in html
    # The number is cut as in the mockup: enough to tell callers apart.
    assert "+4917655\N{HORIZONTAL ELLIPSIS}" in html and "+4917655512345" not in html
    for cell in ("Abholung", "abgeschlossen", "2:14", "0,29 €"):
        assert f"<td>{cell}</td>" in html.split("<tbody>")[1]
    assert "call-note" not in html


def test_a_call_without_caller_audio_shows_its_number_marked_with_the_note(
    db, client, now
):
    """#651 (Maxi, 11.11.2026): the number stands out and the note says why."""
    tenant = _tenant(db, "Eins")
    _call(
        db,
        tenant,
        now - timedelta(hours=1),
        caller_id="+4917655512345",
        outcome="abandoned",
        intent=None,
        line_note="no_caller_audio",
    )

    html = client.get("/gui/anrufe").text

    assert '<td class="call-note">+4917655\N{HORIZONTAL ELLIPSIS}</td>' in html
    assert "Anrufer nicht hörbar (kein Ton angekommen)" in html
    assert "abgebrochen" in html


def test_a_withheld_number_and_a_call_still_running(db, client, now):
    tenant = _tenant(db, "Eins")
    running = {"outcome": None, "ended_at": None, "duration_seconds": None}
    _call(db, tenant, now - timedelta(minutes=1), **running)

    html = client.get("/gui/anrufe").text

    assert "unterdrückt" in html and "läuft" in html


def test_the_page_filters_by_day_outcome_and_intent(db, client, now):
    tenant = _tenant(db, "Eins")
    yesterday = business_day(now, tenant.timezone) - timedelta(days=1)
    _call(db, tenant, now - timedelta(days=1), outcome="callback", intent="complaint")
    _call(db, tenant, now - timedelta(hours=1))

    html = client.get(f"/gui/anrufe?tag={yesterday.isoformat()}").text
    assert (
        "Rückruf angelegt" in html and "abgeschlossen" not in html.split("<tbody>")[1]
    )

    html = client.get("/gui/anrufe?ergebnis=callback").text
    assert "Keine Anrufe" in html
    # A value nobody offered is no filter and no error.
    assert (
        client.get("/gui/anrufe?tag=gestern&ergebnis=x&anliegen=y").status_code == 200
    )


def test_the_page_without_tenant_is_503_and_the_operations_view_links_to_it(
    db, client, now
):
    assert client.get("/gui/anrufe").status_code == 503
    _tenant(db, "Eins")
    assert 'href="/gui/anrufe"' in client.get("/gui").text
