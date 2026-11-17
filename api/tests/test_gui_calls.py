"""The call list (T-8.7, docs/06 §4): the domain rows and the page "Anrufe"."""

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from api.core.time import business_day
from api.db import get_db
from api.domain.calls.log import call_for_team, list_calls
from api.main import app
from api.models import AuditLog, Call
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
        assert f">{cell}</td>" in html.split("<tbody>")[1]
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

    assert (
        '<td data-label="Nummer" class="call-note">+4917655\N{HORIZONTAL ELLIPSIS}</td>'
        in html
    )
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


# --- one row in detail (#571, plan number 10) ---------------------------------------

HX = {"HX-Request": "true", "X-CSRF-Token": "testtoken"}
DETAIL = "/gui/anrufe/anruf"
TOOLS = [
    {"name": "get_item_details", "duration_ms": 41.6, "ok": True},
    {"name": "draft_order", "duration_ms": 120, "ok": False, "error_code": "ambiguous"},
]


def _looks(db) -> list[AuditLog]:
    db.expire_all()
    return list(db.query(AuditLog).filter(AuditLog.action == "gui.call_read"))


def _detail(client, call_id, headers=HX):
    client.cookies.set("csrf_token", "testtoken")
    return client.post(DETAIL, data={"anruf": str(call_id)}, headers=headers)


def test_a_look_at_one_call_is_in_the_audit_log_before_the_row_is_read(db, now):
    tenant = _tenant(db, "Eins")
    call = _call(
        db,
        tenant,
        now - timedelta(hours=1),
        caller_id="+4917655512345",
        tool_calls=TOOLS,
    )

    found = call_for_team(db, tenant, call.id, now, actor="gui:tablet")

    assert found is not None and found.tool_calls_total == 2
    assert [tool.name for tool in found.tool_calls] == [
        "get_item_details",
        "draft_order",
    ]
    (look,) = _looks(db)
    assert (look.actor, look.entity) == ("gui:tablet", "call")
    # Which call, nothing of the guest.
    assert look.payload == {"call": str(call.id)}


def test_a_call_the_list_does_not_show_has_no_detail(db, now):
    one = _tenant(db, "Eins")
    two = _tenant(db, "Zwei")
    theirs = _call(db, two, now - timedelta(hours=1))
    test_call = _call(db, one, now - timedelta(hours=1), is_test=True)
    old = _call(db, one, now - timedelta(days=14))
    last_listed = _call(db, one, now - timedelta(days=13))

    for call in (theirs, test_call, old):
        assert call_for_team(db, one, call.id, now, actor="gui:tablet") is None
    assert call_for_team(db, one, last_listed.id, now, actor="gui:tablet") is not None
    # A look that found nothing is a look all the same.
    assert len(_looks(db)) == 4


def test_the_row_carries_its_id_in_the_button_and_in_no_address(db, client, now):
    tenant = _tenant(db, "Eins")
    call = _call(db, tenant, now - timedelta(hours=1))

    html = client.get("/gui/anrufe").text

    assert 'hx-post="/gui/anrufe/anruf"' in html
    assert 'hx-vals=\'{"anruf": "' + str(call.id) + "\"}'" in html
    assert f"/{call.id}" not in html and f"={call.id}" not in html
    assert 'id="anruf-detail"' in html


def test_the_detail_shows_the_call_and_its_tool_calls(db, client, now):
    tenant = _tenant(db, "Eins")
    call = _call(
        db,
        tenant,
        now - timedelta(hours=1),
        caller_id="+4917655512345",
        cost_cents=29,
        tool_calls=TOOLS,
        outcome="transferred",
        transfer_reason="human_requested",
        line_note="no_caller_audio",
    )

    answer = _detail(client, call.id)

    assert answer.status_code == 200
    assert answer.headers["Cache-Control"] == "no-store"
    for text in (
        "11:00:00",
        "11:02:14",
        "2:14",
        "an Team übergeben",
        "Will Mitarbeiter sprechen",
        "Anrufer nicht hörbar",
        "0,29 €",
        "1. get_item_details",
        "42 ms",
        "2. draft_order",
        "Fehler: ambiguous",
        "Ein Transkript wird nicht gespeichert",
    ):
        assert text in answer.text, text
    # The detail adds no number to what the list shows.
    assert "+4917655" not in answer.text
    assert len(_looks(db)) == 1


def test_whatever_stands_in_the_tool_calls_does_not_break_the_detail(db, client, now):
    tenant = _tenant(db, "Eins")
    junk = [None, "text", {"name": "<b>x</b>", "duration_ms": True}, {"ok": "yes"}, 7]
    call = _call(db, tenant, now - timedelta(hours=1), tool_calls=junk)

    answer = _detail(client, call.id)

    assert answer.status_code == 200
    assert "&lt;b&gt;x&lt;/b&gt;" in answer.text and "<b>x</b>" not in answer.text
    assert "5. ?" in answer.text


@pytest.mark.parametrize("sent", ["", "x", "00000000-0000-4000-8000-000000000001"])
def test_an_id_that_is_no_call_of_the_list_gets_one_answer(db, client, now, sent):
    _tenant(db, "Eins")

    answer = _detail(client, sent)

    assert answer.status_code == 200
    assert "Dieser Anruf ist nicht mehr in der Liste." in answer.text
    assert answer.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize(
    "headers",
    [{}, {"HX-Request": "true"}, {"HX-Request": "true", "X-CSRF-Token": "wrong"}],
)
def test_the_detail_without_the_pages_token_is_refused_and_not_audited(
    db, client, now, headers
):
    tenant = _tenant(db, "Eins")
    call = _call(db, tenant, now - timedelta(hours=1), tool_calls=TOOLS)

    answer = _detail(client, call.id, headers)

    assert answer.status_code == 403 and "draft_order" not in answer.text
    assert _looks(db) == []
