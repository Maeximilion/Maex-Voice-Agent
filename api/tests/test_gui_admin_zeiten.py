"""The admin frame and its tab "Zeiten" (#571, plan number 7): tabs and the
four figures on every admin page, the weekly hours and the special days behind
the admin PIN. These tests act as a signed-in device with the admin PIN
(conftest); the refusal without it is in test_gui_login.py."""

import re
import uuid
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from api.core.time import business_day
from api.db import get_db
from api.domain.reservations.spoken import WEEKDAYS
from api.domain.status import staff_changes as staff
from api.domain.status import weekly
from api.main import app
from api.models import (
    AuditLog,
    OpeningHours,
    OutboxEvent,
    Reservation,
    SpecialDay,
    StaffChange,
    Tenant,
)
from scripts.seed import seed

BERLIN = ZoneInfo("Europe/Berlin")
HX = {"HX-Request": "true", "X-CSRF-Token": "testtoken"}
PAGE = "/gui/zeiten"
FIGURES = (
    "Genauigkeit",
    "Kosten je Bestellung",
    "An das Team übergeben",
    "Verpasste Anrufe",
)
TUESDAY = 1


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    noon = datetime.combine(
        business_day(tz_name="Europe/Berlin"), time(12), tzinfo=BERLIN
    ).astimezone(UTC)
    monkeypatch.setattr("api.core.time.utcnow", lambda: noon)


@pytest.fixture
def engine(migrated_db_url):
    engine = create_engine(migrated_db_url)
    with Session(engine) as s:
        seed(s, tenant_name="Testbetrieb", timezone="Europe/Berlin")
    yield engine
    engine.dispose()


@pytest.fixture
def client(engine):
    def override_get_db():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app)
    client.cookies.set("csrf_token", "testtoken")
    try:
        yield client
    finally:
        app.dependency_overrides.pop(get_db, None)


def _shown(spans: str) -> str:
    """Spans as the page writes them, with an en dash."""
    return spans.replace("-", "\N{EN DASH}")


def _today():
    return business_day(tz_name="Europe/Berlin")


def _tenant_id(engine) -> uuid.UUID:
    with Session(engine) as session:
        return session.scalars(select(Tenant.id)).one()


def _spans(engine, weekday, service="pickup") -> list[str]:
    with Session(engine) as session:
        return [
            span.text()
            for span in weekly.week(session, _tenant_id(engine))[weekday][service]
        ]


def _audits(engine, action) -> list[AuditLog]:
    with Session(engine) as session:
        return list(session.scalars(select(AuditLog).where(AuditLog.action == action)))


def _count(engine, model) -> int:
    with Session(engine) as session:
        return session.scalar(select(func.count()).select_from(model)) or 0


def _fields(html: str) -> dict[str, str]:
    """What an untouched submit of the form in `html` sends: every named
    input with its value, a checkbox only when ticked."""
    sent = {}
    for tag in re.findall(r"<input\b[^>]*>", html):
        name = re.search(r'name="([^"]+)"', tag)
        value = re.search(r'value="([^"]*)"', tag)
        if name is None or ('type="checkbox"' in tag and " checked" not in tag):
            continue
        sent[name.group(1)] = value.group(1) if value else ""
    return sent


def _edit(client, weekday=TUESDAY) -> dict[str, str]:
    form = client.get(f"{PAGE}/woche/{weekday}", headers=HX)
    assert form.status_code == 200
    return _fields(form.text)


# --- the frame: tabs and the four figures -------------------------------------------


@pytest.mark.parametrize(
    ("path", "tab"),
    [
        (PAGE, "Zeiten"),
        ("/gui/anrufe", "Anrufe"),
        ("/gui/kennzahlen", "Kennzahlen"),
        ("/gui/geraete", "Geräte"),
    ],
)
def test_every_admin_page_has_the_tabs_and_the_four_figures(client, path, tab):
    page = client.get(path)
    assert page.status_code == 200
    nav = page.text.split('<nav class="tabs"', 1)[1].split("</nav>", 1)[0]
    assert re.findall(r">([^<>]+)</a>", nav) == [
        "Zeiten",
        "Anrufe",
        "Kennzahlen",
        "Geräte",
    ]
    assert re.search(rf'aria-current="page"[^>]*>{tab}</a>', nav)
    assert nav.count('aria-current="page"') == 1
    bar = page.text.split('class="kz-bar"', 1)[1].split("</div>\n</", 1)[0]
    for label in FIGURES:
        assert label in bar


@pytest.mark.parametrize("path", [PAGE, "/gui/anrufe", "/gui/geraete"])
def test_figures_that_cannot_be_read_do_not_take_an_admin_page_down(
    client, monkeypatch, path
):
    """The lost tablet is signed out on "Geräte": that page must not hang on
    the calls of the week."""

    def broken(session, tenant):
        raise ZeroDivisionError("figures")

    monkeypatch.setattr("api.gui.figures_view.bar", broken)
    page = client.get(path)
    assert page.status_code == 200
    assert '<nav class="tabs"' in page.text and 'class="kz-bar"' not in page.text


def test_the_settings_sheet_leads_to_the_weekly_hours(client):
    sheet = client.get("/gui/einstellungen", headers=HX)
    assert 'href="/gui/zeiten"' in sheet.text
    assert "Öffnungszeiten der Woche (Inhaber-PIN)" in sheet.text


# --- the weekly hours -------------------------------------------------------------


def test_the_page_shows_the_week_from_the_database(client, engine):
    page = client.get(PAGE)
    assert page.status_code == 200
    assert page.headers["Cache-Control"] == "no-store"
    for word in ("Öffnungszeiten", "Im Haus", "Abholung", "Lieferung", "Sondertage"):
        assert word in page.text
    for weekday in WEEKDAYS:
        assert weekday in page.text
    # Seed: Monday closed, every other day 11:30-14:00 and 17:00-22:00.
    assert _shown("11:30-14:00, 17:00-22:00") in page.text
    assert "geschlossen" in page.text
    # A change in the database is what the next load shows: nothing is kept
    # in the page or the process.
    with Session(engine) as session:
        session.add(
            OpeningHours(
                tenant_id=_tenant_id(engine),
                weekday=0,
                service="dinein",
                opens_at=time(7, 15),
                closes_at=time(9, 45),
            )
        )
        session.commit()
    assert _shown("07:15-09:45") in client.get(PAGE).text


def test_the_form_of_a_weekday_shows_what_holds(client):
    sent = _edit(client)
    assert sent["pickup-von-0"] == "11:30" and sent["pickup-bis-0"] == "14:00"
    assert sent["delivery-von-1"] == "17:00" and sent["delivery-bis-1"] == "22:00"
    # One empty row per service to add a span.
    assert sent["dinein-von-2"] == "" and sent["dinein-bis-2"] == ""
    assert len(sent["gesehen"]) == 16
    assert not any("nachts" in name for name in sent)


def test_an_untouched_form_changes_nothing(client, engine):
    """No field is preselected with a value its untouched submit would write."""
    answer = client.post(f"{PAGE}/woche/{TUESDAY}", data=_edit(client), headers=HX)
    assert answer.status_code == 200 and "Keine Änderung." in answer.text
    assert _spans(engine, TUESDAY) == ["11:30-14:00", "17:00-22:00"]
    assert _audits(engine, "opening_hours.changed") == []


def test_the_owner_changes_a_weekday(client, engine):
    sent = _edit(client)
    sent.update({"pickup-bis-1": "21:30", "pickup-von-2": "08:00"})
    sent["pickup-bis-2"] = "10:00"
    answer = client.post(f"{PAGE}/woche/{TUESDAY}", data=sent, headers=HX)
    assert answer.status_code == 200
    assert "Gespeichert. Gilt ab sofort." in answer.text
    assert _shown("08:00-10:00, 11:30-14:00, 17:00-21:30") in answer.text
    assert answer.headers["Cache-Control"] == "no-store"
    assert _spans(engine, TUESDAY) == ["08:00-10:00", "11:30-14:00", "17:00-21:30"]
    assert _spans(engine, TUESDAY, "dinein") == ["11:30-14:00", "17:00-22:00"]
    (audit,) = _audits(engine, "opening_hours.changed")
    assert audit.actor == "gui:owner"


def test_emptied_rows_close_the_service_on_that_weekday(client, engine):
    sent = _edit(client)
    for name in list(sent):
        if name.startswith("delivery-"):
            sent[name] = ""
    answer = client.post(f"{PAGE}/woche/{TUESDAY}", data=sent, headers=HX)
    assert "Gespeichert" in answer.text
    assert _spans(engine, TUESDAY, "delivery") == []
    assert _spans(engine, TUESDAY) == ["11:30-14:00", "17:00-22:00"]


def test_a_night_needs_its_box_and_shows_it_ticked_afterwards(client, engine):
    sent = _edit(client)
    sent.update({"pickup-von-1": "18:00", "pickup-bis-1": "01:00"})
    refused = client.post(f"{PAGE}/woche/{TUESDAY}", data=sent, headers=HX)
    assert "Ende nach Mitternacht" in refused.text and "badge danger" in refused.text
    # The form comes back with what was typed, and nothing is written.
    assert 'value="18:00"' in refused.text and 'value="01:00"' in refused.text
    assert _spans(engine, TUESDAY) == ["11:30-14:00", "17:00-22:00"]

    sent["pickup-nachts-1"] = "1"
    saved = client.post(f"{PAGE}/woche/{TUESDAY}", data=sent, headers=HX)
    assert "Gespeichert" in saved.text
    assert _spans(engine, TUESDAY) == ["11:30-14:00", "18:00-01:00"]
    again = _edit(client)
    assert again["pickup-nachts-1"] == "1"
    untouched = client.post(f"{PAGE}/woche/{TUESDAY}", data=again, headers=HX)
    assert "Keine Änderung." in untouched.text


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"pickup-von-0": "25:00"}, "Bitte Beginn und Ende als Uhrzeit angeben."),
        ({"pickup-von-0": "11:30:30"}, "Bitte Beginn und Ende als Uhrzeit angeben."),
        ({"pickup-bis-0": ""}, "Bitte Beginn und Ende als Uhrzeit angeben."),
        ({"pickup-bis-0": "11:30"}, "das Ende nicht gleich dem Beginn"),
        ({"pickup-bis-0": "18:00"}, "Zwei Zeiten überschneiden sich."),
    ],
)
def test_times_that_cannot_hold_are_refused_with_the_reason(
    client, engine, change, message
):
    sent = {**_edit(client), **change}
    answer = client.post(f"{PAGE}/woche/{TUESDAY}", data=sent, headers=HX)
    assert answer.status_code == 200 and message in answer.text
    assert 'name="gesehen"' in answer.text
    assert _spans(engine, TUESDAY) == ["11:30-14:00", "17:00-22:00"]
    assert _audits(engine, "opening_hours.changed") == []


def test_a_form_another_device_overtook_writes_nothing(client, engine):
    stale = _edit(client)
    first = {**stale, "pickup-bis-1": "21:00"}
    client.post(f"{PAGE}/woche/{TUESDAY}", data=first, headers=HX)
    second = {**stale, "pickup-bis-1": "23:00"}
    answer = client.post(f"{PAGE}/woche/{TUESDAY}", data=second, headers=HX)
    assert "haben sich gerade geändert" in answer.text
    assert _spans(engine, TUESDAY) == ["11:30-14:00", "17:00-21:00"]
    # The form that comes back shows the fresh hours and their token.
    assert _fields(answer.text)["pickup-bis-1"] == "21:00"
    assert _fields(answer.text)["gesehen"] != stale["gesehen"]


def test_a_form_without_its_token_writes_nothing(client, engine):
    sent = {**_edit(client), "pickup-bis-1": "21:00"}
    del sent["gesehen"]
    answer = client.post(f"{PAGE}/woche/{TUESDAY}", data=sent, headers=HX)
    assert "haben sich gerade geändert" in answer.text
    assert _spans(engine, TUESDAY) == ["11:30-14:00", "17:00-22:00"]


@pytest.mark.parametrize("weekday", ["7", "-1", "x"])
def test_a_weekday_that_does_not_exist_is_not_found(client, weekday):
    assert client.get(f"{PAGE}/woche/{weekday}", headers=HX).status_code in (404, 422)
    answer = client.post(f"{PAGE}/woche/{weekday}", data={}, headers=HX)
    assert answer.status_code in (404, 422)


@pytest.mark.parametrize(
    ("path", "data"),
    [(f"{PAGE}/woche/1", {"gesehen": "x"}), (f"{PAGE}/tag", {"art": "geschlossen"})],
)
def test_a_write_needs_htmx_and_the_csrf_token(client, engine, path, data):
    plain = client.post(path, data=data, headers={"X-CSRF-Token": "testtoken"})
    forged = client.post(path, data=data, headers={**HX, "X-CSRF-Token": "other"})
    missing = client.post(path, data=data, headers={"HX-Request": "true"})
    assert [plain.status_code, forged.status_code, missing.status_code] == [403] * 3
    assert _audits(engine, "opening_hours.changed") == []
    assert _count(engine, SpecialDay) == 0


# --- special days -------------------------------------------------------------------


def _day(client, **data):
    return client.post(f"{PAGE}/tag", data=data, headers=HX)


def test_the_form_for_a_date_preselects_nothing(client, engine):
    page = client.get(PAGE)
    form = page.text.split('hx-post="/gui/zeiten/tag"', 1)[1].split("</form>", 1)[0]
    assert _fields(form) == {"datum": "", "von": "", "bis": ""}
    for art in ("zeiten", "geschlossen"):
        answer = _day(client, **_fields(form), art=art)
        assert "Bitte einen Tag wählen." in answer.text
    assert _count(engine, SpecialDay) == 0


def test_the_owner_closes_a_later_date_and_opens_it_again(client, engine):
    later = _today() + timedelta(days=9)
    answer = _day(client, datum=later.isoformat(), art="geschlossen")
    assert answer.status_code == 200 and "Gespeichert. Gilt ab sofort." in answer.text
    assert f"{later:%d.%m.%Y}" in answer.text and "geschlossen" in answer.text
    assert answer.headers["Cache-Control"] == "no-store"
    with Session(engine) as session:
        row = session.scalars(select(SpecialDay)).one()
        assert (row.date, row.closed, row.status) == (later, True, "set")
    (audit,) = _audits(engine, "special_day.set")
    assert audit.actor == "gui:owner"
    # The owner's own change: no check card, no push.
    assert _count(engine, StaffChange) == 0 and _count(engine, OutboxEvent) == 0

    back = _day(client, datum=later.isoformat(), art="normal")
    assert "Gespeichert" in back.text and f"{later:%d.%m.%Y}" not in back.text
    assert _count(engine, SpecialDay) == 0


def test_longer_hours_for_a_later_date_hold_at_once(client, engine):
    later = _today() + timedelta(days=9)
    answer = _day(
        client, datum=later.isoformat(), art="zeiten", von="09:00", bis="23:30"
    )
    assert "09:00 bis 23:30 Uhr" in answer.text
    form = answer.text.split('class="day-form"', 1)[1]
    assert _fields(form) == {"datum": "", "von": "", "bis": ""}
    with Session(engine) as session:
        row = session.scalars(select(SpecialDay)).one()
        assert (row.opens_at, row.closes_at) == (time(9), time(23, 30))


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"art": "zeiten", "von": "09:00"}, "Bitte Beginn und Ende als Uhrzeit"),
        ({"art": "zeiten", "von": "9", "bis": "12:00"}, "Bitte Beginn und Ende als"),
        ({"art": "zeiten", "von": "18:00", "bis": "02:00"}, "Ende nach Mitternacht"),
        ({"art": "wie"}, "Bitte einen Tag wählen."),
        ({"art": "geschlossen", "datum": "morgen"}, "Bitte einen Tag wählen."),
        ({"art": "geschlossen", "datum": "2020-01-01"}, "Ein vergangener Tag"),
    ],
)
def test_a_date_that_cannot_be_set_is_refused_with_the_reason(
    client, engine, data, message
):
    later = (_today() + timedelta(days=9)).isoformat()
    answer = _day(client, **{"datum": later, **data})
    assert answer.status_code == 200 and message in answer.text
    assert _count(engine, SpecialDay) == 0
    assert _audits(engine, "special_day.set") == []
    # What was typed is shown again; a saved form starts empty (other tests).
    form = answer.text.split('class="day-form"', 1)[1]
    assert _fields(form)["von"] == data.get("von", "")


def test_a_date_with_an_open_card_is_left_to_the_card(client, engine):
    later = _today() + timedelta(days=9)
    asked = staff.DayHours(opens=time(9), closes=time(23))
    with Session(engine) as session:
        card = staff.change_day_hours(session, _tenant_id(engine), later, asked)
        assert card is not None and not card.held
    answer = _day(client, datum=later.isoformat(), art="geschlossen")
    assert "wartet eine Änderung des Teams" in answer.text
    assert _count(engine, SpecialDay) == 0


# --- confirmed tables outside the hours ---------------------------------------------


def _book(engine, day, at, name, phone="+491701234567"):
    with Session(engine) as session:
        session.add(
            Reservation(
                tenant_id=_tenant_id(engine),
                source="tablet",
                status="confirmed",
                guest_name=name,
                phone=phone,
                party_size=4,
                reserved_for=datetime.combine(day, at, tzinfo=BERLIN),
                idempotency_key=str(uuid.uuid4()),
            )
        )
        session.commit()


def _next(weekday: int):
    """The next date of that weekday after today, so its noon is still ahead."""
    day = _today() + timedelta(days=1)
    while day.weekday() != weekday:
        day += timedelta(days=1)
    return day


def test_tables_a_shorter_week_leaves_out_are_listed_to_call(client, engine):
    tuesday = _next(TUESDAY)
    _book(engine, tuesday, time(21), "Familie <b>Spät</b>")
    _book(engine, tuesday, time(19), "Bleibt Drin")
    assert "bitte selbst anrufen" not in client.get(PAGE).text

    sent = {**_edit(client), "dinein-bis-1": "20:00"}
    answer = client.post(f"{PAGE}/woche/{TUESDAY}", data=sent, headers=HX)
    for text in (answer.text, client.get(PAGE).text):
        assert "Diese Gäste bitte selbst anrufen" in text
        # The restaurant's clock, and the name as text, never as markup.
        assert f"{tuesday:%d.%m.} 21:00 Uhr" in text
        assert "Familie &lt;b&gt;Spät&lt;/b&gt;" in text and "<b>Spät</b>" not in text
        assert 'href="tel:+491701234567"' in text and "4 Personen" in text
        assert "Bleibt Drin" not in text


def test_a_costly_number_is_shown_without_a_call_link(client, engine):
    monday = _next(0)
    _book(engine, monday, time(12), "Teuer", phone="+4990012345678")
    page = client.get(PAGE).text
    assert "Teure Sondernummer" in page and 'href="tel:+4990012345678"' not in page


def test_closing_a_date_lists_its_tables(client, engine):
    tuesday = _next(TUESDAY)
    _book(engine, tuesday, time(19), "Am Sondertag")
    answer = _day(client, datum=tuesday.isoformat(), art="geschlossen")
    assert "Diese Gäste bitte selbst anrufen" in answer.text
    assert "Am Sondertag" in answer.text


def test_a_restaurant_without_hours_reads_closed_everywhere(client, engine):
    with Session(engine) as session:
        session.query(OpeningHours).delete()
        session.commit()
    page = client.get(PAGE)
    assert page.status_code == 200 and page.text.count("geschlossen") >= 21
