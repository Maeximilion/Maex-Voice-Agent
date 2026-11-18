"""The admin frame and its tab "Zeiten" (#571, plan number 7): tabs and the
four figures on every admin page, the weekly hours and the special days behind
the admin PIN. These tests act as a signed-in device with the admin PIN
(conftest); the refusal without it is in test_gui_login.py."""

import re
import uuid
from datetime import UTC, datetime, time, timedelta
from html import unescape
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
from api.gui import hours_view
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
    """What an untouched submit of the form in `html` sends, as a browser
    reads the markup: every named input and text area with its value, a
    checkbox only when ticked."""
    sent = {}
    for tag in re.findall(r"<input\b[^>]*>", html):
        name = re.search(r'name="([^"]+)"', tag)
        value = re.search(r'value="([^"]*)"', tag)
        if name is None or ('type="checkbox"' in tag and " checked" not in tag):
            continue
        sent[name.group(1)] = unescape(value.group(1)) if value else ""
    for name, text in re.findall(
        r'<textarea\b[^>]*name="([^"]+)"[^>]*>([^<]*)</textarea>', html
    ):
        sent[name] = unescape(text)
    return sent


def _edit(client, weekday=TUESDAY) -> dict[str, str]:
    form = client.get(f"{PAGE}/woche/{weekday}", headers=HX)
    assert form.status_code == 200
    assert form.headers["Cache-Control"] == "no-store"
    return _fields(form.text)


def _week_table(html: str) -> str:
    return html.split('<table class="tbl week">', 1)[1].split("</table>", 1)[0]


# --- the frame: tabs and the four figures -------------------------------------------


@pytest.mark.parametrize(
    ("path", "tab"),
    [
        (PAGE, "Zeiten"),
        ("/gui/lieferung", "Lieferung"),
        ("/gui/menue", "Menü"),
        ("/gui/anrufe", "Anrufe"),
        ("/gui/kennzahlen", "Kennzahlen"),
        ("/gui/daten", "Daten"),
        ("/gui/geraete", "Geräte"),
    ],
)
def test_every_admin_page_has_the_tabs_and_the_four_figures(client, path, tab):
    page = client.get(path)
    assert page.status_code == 200
    nav = page.text.split('<nav class="tabs"', 1)[1].split("</nav>", 1)[0]
    assert re.findall(r">([^<>]+)</a>", nav) == [
        "Zeiten",
        "Lieferung",
        "Menü",
        "Anrufe",
        "Kennzahlen",
        "Daten",
        "Geräte",
    ]
    assert re.search(rf'aria-current="page"[^>]*>{tab}</a>', nav)
    assert nav.count('aria-current="page"') == 1
    # In the frame, not somewhere on the page: Kennzahlen names them again
    # in its history table.
    frame = page.text.split('<header class="admin">', 1)[1].split("</header>", 1)[0]
    bar = frame.split('class="kz-bar"', 1)[1]
    for label in FIGURES:
        assert label in bar
    assert page.headers["Cache-Control"] == "no-store"


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
    # Monday's three cells, and no other.
    assert _week_table(page.text).count("geschlossen") == 3
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
    assert "badge ok" in answer.text and "badge danger" not in answer.text
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
        # A dot is no colon: read as an ISO time it would be 11:00 and a fraction.
        ({"pickup-von-0": "11.30"}, "Bitte Beginn und Ende als Uhrzeit angeben."),
        ({"pickup-bis-0": ""}, "Bitte Beginn und Ende als Uhrzeit angeben."),
        ({"pickup-bis-0": "11:30"}, "das Ende nicht gleich dem Beginn"),
        ({"pickup-bis-0": "18:00"}, "Zwei Zeiten überschneiden sich"),
        ({"pickup-bis-0": "17:00"}, "schließen direkt aneinander an"),
        # A night to 13:00 runs into Wednesday's lunch.
        (
            {"pickup-bis-1": "13:00", "pickup-nachts-1": "1"},
            "Die Zeit über Mitternacht am Dienstag reicht in die Zeiten am Mittwoch",
        ),
    ],
)
def test_times_that_cannot_hold_are_refused_with_the_reason(
    client, engine, change, message
):
    sent = {**_edit(client), **change}
    answer = client.post(f"{PAGE}/woche/{TUESDAY}", data=sent, headers=HX)
    assert answer.status_code == 200 and message in answer.text
    assert "badge danger" in answer.text and "badge ok" not in answer.text
    assert answer.headers["Cache-Control"] == "no-store"
    # The refused form keeps the token it was typed against.
    assert _fields(answer.text)["gesehen"] == sent["gesehen"]
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
    assert "badge danger" in answer.text and "badge ok" not in answer.text
    assert answer.headers["Cache-Control"] == "no-store"
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
    assert page.status_code == 200
    assert _week_table(page.text).count("geschlossen") == 21


# --- found by the deep review ----------------------------------------------------------


def test_a_refused_stale_form_cannot_save_over_another_device(client, engine):
    """A form refused for a typing error keeps its old token: corrected and
    sent again, it meets the conflict, not the other device's hours."""
    stale = _edit(client)
    other = {**stale, "pickup-bis-1": "21:00"}
    client.post(f"{PAGE}/woche/{TUESDAY}", data=other, headers=HX)

    typo = {**stale, "pickup-bis-1": "23:00", "pickup-von-0": ""}
    refused = client.post(f"{PAGE}/woche/{TUESDAY}", data=typo, headers=HX)
    assert "Bitte Beginn und Ende als Uhrzeit angeben." in refused.text
    again = {**_fields(refused.text), "pickup-von-0": "11:30"}
    assert again["gesehen"] == stale["gesehen"] and again["pickup-bis-1"] == "23:00"
    answer = client.post(f"{PAGE}/woche/{TUESDAY}", data=again, headers=HX)
    assert "haben sich gerade geändert" in answer.text
    assert _spans(engine, TUESDAY) == ["11:30-14:00", "17:00-21:00"]
    assert len(_audits(engine, "opening_hours.changed")) == 1


def test_the_week_again_after_abbrechen(client):
    answer = client.get(f"{PAGE}/woche", headers=HX)
    assert answer.status_code == 200
    assert answer.headers["Cache-Control"] == "no-store"
    assert _week_table(answer.text).count("geschlossen") == 3
    assert 'id="zeiten-gaeste" hx-swap-oob="true"' in answer.text


def test_a_service_the_form_does_not_name_keeps_its_hours(client, engine):
    sent = {
        name: value
        for name, value in _edit(client).items()
        if not name.startswith("delivery-")
    }
    sent["pickup-bis-1"] = "21:00"
    answer = client.post(f"{PAGE}/woche/{TUESDAY}", data=sent, headers=HX)
    assert "Gespeichert" in answer.text
    assert _spans(engine, TUESDAY, "delivery") == ["11:30-14:00", "17:00-22:00"]
    assert _spans(engine, TUESDAY) == ["11:30-14:00", "17:00-21:00"]


def test_a_fifth_span_is_refused_by_name(client, engine):
    sent = _edit(client)
    for row, (von, bis) in enumerate(
        [("06:00", "07:00"), ("08:00", "09:00"), ("10:00", "11:00")], start=2
    ):
        sent[f"pickup-von-{row}"], sent[f"pickup-bis-{row}"] = von, bis
    answer = client.post(f"{PAGE}/woche/{TUESDAY}", data=sent, headers=HX)
    assert "Höchstens 4 Zeiten je Tag und Bereich." in answer.text
    assert _spans(engine, TUESDAY) == ["11:30-14:00", "17:00-22:00"]


def test_a_date_saved_again_says_no_change_and_a_night_holds_with_its_box(
    client, engine
):
    later = (_today() + timedelta(days=9)).isoformat()
    night = {"datum": later, "art": "zeiten", "von": "18:00", "bis": "02:00"}
    refused = _day(client, **night)
    form = _fields(refused.text.split('class="day-form"', 1)[1])
    assert form == {"datum": later, "von": "18:00", "bis": "02:00"}
    saved = _day(client, **night, nachts="1")
    assert "Gespeichert" in saved.text and "badge ok" in saved.text
    assert "18:00 bis 02:00 Uhr" in saved.text
    again = _day(client, **night, nachts="1")
    assert "Keine Änderung." in again.text and "badge ok" in again.text
    assert len(_audits(engine, "special_day.set")) == 1


def test_a_table_without_a_number_gets_no_call_link(client, engine):
    """The team may enter a table without a number ("Trotzdem eintragen")."""
    _book(engine, _next(0), time(19), "Ohne Nummer", phone="")
    page = client.get(PAGE).text
    assert "Ohne Nummer" in page and "keine Nummer eingetragen" in page
    assert 'href="tel:"' not in page
    assert "Hat das Team einen Tisch bewusst so eingetragen" in page


@pytest.mark.parametrize(
    "text",
    ["10.30", "T1000", "1000Z", "24:00", "1:30", "10:3o", "10:30:00", " 10:30", ""],
)
def test_only_a_clock_time_of_digits_and_a_colon_is_a_time(text):
    with pytest.raises(ValueError):
        hours_view.hhmm(text)
    assert hours_view.hhmm("10:30") == time(10, 30)


def test_a_service_stored_with_more_spans_than_the_form_adds_comes_back_whole(
    client, engine
):
    """Six spans from provisioning: the form shows and sends all six, so the
    service reads as untouched and the other services can be saved."""
    with Session(engine) as session:
        tenant_id = _tenant_id(engine)
        session.query(OpeningHours).filter_by(
            tenant_id=tenant_id, weekday=TUESDAY, service="delivery"
        ).delete()
        session.add_all(
            OpeningHours(
                tenant_id=tenant_id,
                weekday=TUESDAY,
                service="delivery",
                opens_at=time(hour),
                closes_at=time(hour, 30),
            )
            for hour in range(8, 14)
        )
        session.commit()
    sent = _edit(client)
    assert sent["delivery-von-5"] == "13:00" and "delivery-von-6" not in sent
    untouched = client.post(f"{PAGE}/woche/{TUESDAY}", data=sent, headers=HX)
    assert "Keine Änderung." in untouched.text
    sent["dinein-bis-1"] = "21:00"
    saved = client.post(f"{PAGE}/woche/{TUESDAY}", data=sent, headers=HX)
    assert "Gespeichert" in saved.text
    assert _spans(engine, TUESDAY, "dinein") == ["11:30-14:00", "17:00-21:00"]
    assert len(_spans(engine, TUESDAY, "delivery")) == 6


# --- forms and accessibility (#819, #821, #829) -----------------------------------


def _tag(html: str, name: str) -> str:
    """The opening tag of the field `name`."""
    found = re.search(rf'<(?:input|textarea|select)\b[^>]*name="{name}"[^>]*>', html)
    assert found, name
    return found.group(0)


def _described(html: str, name: str) -> str:
    """The text of every element the field `name` names in aria-describedby."""
    ids = re.search(r'aria-describedby="([^"]+)"', _tag(html, name))
    assert ids, name
    texts = []
    for each in ids.group(1).split():
        found = re.search(rf'<p\b[^>]*id="{each}"[^>]*>(.*?)</p>', html, re.S)
        assert found, each
        texts.append(unescape(re.sub(r"<[^>]+>", "", found.group(1))))
    return " ".join(texts)


def test_a_form_body_beyond_the_limit_is_refused_before_it_is_read(client, engine):
    """`_form` reads at most FORM_MAX bytes, in every setup, not only behind
    the proxy (#819)."""
    sent = {**_edit(client), "pickup-bis-1": "21:00", "pad": "x" * 70_000}
    answer = client.post(f"{PAGE}/woche/{TUESDAY}", data=sent, headers=HX)
    assert answer.status_code == 413
    assert _spans(engine, TUESDAY) == ["11:30-14:00", "17:00-22:00"]
    # A form within the limit still saves.
    sent.pop("pad")
    assert (
        "Gespeichert"
        in client.post(f"{PAGE}/woche/{TUESDAY}", data=sent, headers=HX).text
    )


def test_every_refused_row_is_marked_and_named_by_area_and_row(client, engine):
    sent = {
        **_edit(client),
        "pickup-bis-1": "01:00",
        "delivery-von-1": "13:00",
        "delivery-bis-1": "20:00",
    }
    answer = client.post(f"{PAGE}/woche/{TUESDAY}", data=sent, headers=HX)
    html = answer.text
    for name in ("pickup-von-1", "pickup-bis-1", "delivery-von-1", "delivery-bis-1"):
        assert 'aria-invalid="true"' in _tag(html, name), name
    assert "Ende nach Mitternacht" in _described(html, "pickup-bis-1")
    assert "überschneiden sich" in _described(html, "delivery-von-1")
    for name in ("pickup-von-0", "dinein-von-0", "delivery-von-0"):
        assert "aria-invalid" not in _tag(html, name), name
    # The summary names area and row, links to the field and takes the focus.
    summary = html.split("data-fokus", 1)[1].split("</div>", 1)[0]
    assert "Abholung, Zeile 2" in summary and "Lieferung, Zeile 2" in summary
    assert 'href="#f-pickup-von-1"' in summary and 'id="f-pickup-von-1"' in html
    assert 'role="alert"' in html.split("data-fokus", 1)[0].rsplit("<", 1)[1]
    assert _spans(engine, TUESDAY) == ["11:30-14:00", "17:00-22:00"]


def test_rows_that_are_no_time_are_all_marked_at_once(client):
    sent = {**_edit(client), "pickup-von-0": "25:00", "dinein-bis-1": "9"}
    html = client.post(f"{PAGE}/woche/{TUESDAY}", data=sent, headers=HX).text
    assert 'aria-invalid="true"' in _tag(html, "pickup-von-0")
    assert 'aria-invalid="true"' in _tag(html, "dinein-bis-1")
    assert "als Uhrzeit" in _described(html, "dinein-von-1")
    assert "aria-invalid" not in _tag(html, "delivery-von-0")


def test_an_end_at_midnight_is_named_on_the_form(client, engine):
    sent = {**_edit(client), "pickup-bis-1": "00:00"}
    html = client.post(f"{PAGE}/woche/{TUESDAY}", data=sent, headers=HX).text
    assert "00:00" in _described(html, "pickup-bis-1")
    assert _spans(engine, TUESDAY) == ["11:30-14:00", "17:00-22:00"]


def test_a_save_says_so_where_the_eye_and_the_focus_go(client):
    """The result of a save is moved into view and takes the focus (#829-2):
    the message carries `data-fokus` and can be focused."""
    sent = {**_edit(client), "pickup-bis-1": "21:00"}
    html = client.post(f"{PAGE}/woche/{TUESDAY}", data=sent, headers=HX).text
    message = re.search(r"<p\b[^>]*data-fokus[^>]*>([^<]*)</p>", html)
    assert message and message.group(1) == "Gespeichert. Gilt ab sofort."
    assert 'tabindex="-1"' in message.group(0) and 'role="status"' in message.group(0)
    # An opened form takes the focus on its heading (#821).
    form = client.get(f"{PAGE}/woche/{TUESDAY}", headers=HX).text
    assert re.search(r'<h2\b[^>]*tabindex="-1"[^>]*data-fokus', form)


def test_the_forms_are_greyed_while_their_answer_is_on_its_way(client):
    """No second tap while a save travels (#829-5)."""
    form = client.get(f"{PAGE}/woche/{TUESDAY}", headers=HX).text
    page = client.get(PAGE).text
    day = page.split('<form class="day-form"', 1)[1].split(">", 1)[0]
    for html in (form, day):
        assert 'hx-disabled-elt="find button"' in html


def test_the_column_of_the_change_buttons_has_a_header(client):
    table = _week_table(client.get(PAGE).text)
    head = table.split("</thead>", 1)[0]
    assert "<th></th>" not in head and ">Ändern</th>" in head


@pytest.mark.parametrize(
    ("data", "fields"),
    [
        ({"art": "zeiten", "von": "09:00"}, ("von", "bis")),
        ({"art": "zeiten", "von": "18:00", "bis": "02:00"}, ("bis",)),
        ({"art": "geschlossen", "datum": "2020-01-01"}, ("datum",)),
    ],
)
def test_a_refused_date_marks_its_field(client, data, fields):
    later = (_today() + timedelta(days=9)).isoformat()
    html = _day(client, **{"datum": later, **data}).text
    form = html.split('class="day-form"', 1)[1]
    for name in ("datum", "von", "bis"):
        marked = 'aria-invalid="true"' in _tag(form, name)
        assert marked == (name in fields), name
    assert "data-fokus" in html


def test_closing_a_day_asks_first_like_the_way_back(client):
    """#829-4: "Geschlossen" stops every service for a day; it is its own request
    because htmx reads hx-confirm from the element that sends."""
    page = client.get(PAGE).text
    button = re.search(r"<button[^>]*>Geschlossen</button>", page, re.S)

    assert button
    assert 'type="button"' in button.group(0) and "hx-confirm=" in button.group(0)
    assert 'hx-vals=\'{"art": "geschlossen"}\'' in button.group(0)


@pytest.mark.parametrize("path", ["/gui/zeiten", "/gui/geraete"])
def test_the_frame_names_its_way_back_and_its_page_like_the_tab(client, path):
    """#829-14, #829-22: "Zurück" led to the board, not back; Geräte had a title
    of its own; the tab had no icon."""
    page = client.get(path).text

    assert 'href="/gui">Zur Betriebsansicht</a>' in page
    assert ">Zurück</a>" not in page
    assert "Angemeldete Geräte" not in page.split("<main>", 1)[0]
    assert '<link rel="icon"' in page


def test_the_tab_row_stands_beside_the_header_so_it_can_stick(client):
    page = client.get(PAGE).text
    after_header = page.split("</header>", 1)[1]

    assert after_header.lstrip().startswith('<nav class="tabs"')
