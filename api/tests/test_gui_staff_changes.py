"""The staff changes on the tablet (#570 part b, D34 point 4): the sheets for the
team number and a date's hours, the check cards and the owner's answer. These
tests act as a signed-in device with the admin PIN (conftest); the refusal
without it is in test_gui_login.py."""

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from api.config import settings
from api.core.time import business_day
from api.db import get_db
from api.domain.status import holidays
from api.domain.status.staff_changes import DayHours, change_team_phone, set_day_hours
from api.main import app
from api.models import Reservation, ServiceConfig, SpecialDay, StaffChange
from scripts.seed import seed

BERLIN = ZoneInfo("Europe/Berlin")
HX = {"HX-Request": "true", "X-CSRF-Token": "testtoken"}
NUMBER = "+497215551234"


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    noon = datetime.combine(
        business_day(tz_name="Europe/Berlin"), time(12), tzinfo=BERLIN
    ).astimezone(UTC)
    monkeypatch.setattr("api.core.time.utcnow", lambda: noon)


@pytest.fixture(autouse=True)
def dialable(monkeypatch):
    monkeypatch.setattr(settings, "transfer_targets", NUMBER)


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


def _today():
    return business_day(tz_name="Europe/Berlin")


def _cards(engine) -> list[StaffChange]:
    with Session(engine) as session:
        return list(session.scalars(select(StaffChange)))


def _seen(card: StaffChange) -> dict[str, str]:
    """What a card's buttons send: the version the owner saw (#596)."""
    return {"gesehen": card.updated_at.isoformat()}


def _config(engine) -> ServiceConfig:
    with Session(engine) as session:
        return session.scalars(select(ServiceConfig)).one()


def test_back_to_the_standard_number_says_the_change_is_undone(client, engine):
    """#861: the card closes as undone, so the sheet no longer says the number
    holds to closing or that the owner sees it on the board."""
    standard = _config(engine).team_phone
    client.post("/gui/teamnummer", data={"nummer": NUMBER}, headers=HX)
    back = client.post("/gui/teamnummer", data={"nummer": standard}, headers=HX)
    assert back.status_code == 200
    assert "Gilt bis Betriebsschluss" not in back.text
    assert "Zurückgenommen, es gilt wieder die Nummer von vorher." in back.text
    assert [c.status for c in _cards(engine)] == ["undone"]
    cards = client.get("/gui/fragments/pruefen", headers=HX).text
    assert "Teamnummer heute" not in cards


def test_the_team_number_for_today_and_its_card(client, engine):
    standard = _config(engine).team_phone
    sheet = client.get("/gui/teamnummer", headers=HX)
    assert sheet.status_code == 200 and standard in sheet.text
    # The listed numbers are the choices; the one that holds is not offered.
    assert f'"nummer": "{NUMBER}"' in sheet.text
    assert f'"nummer": "{standard}"' not in sheet.text
    answer = client.post("/gui/teamnummer", data={"nummer": NUMBER}, headers=HX)
    assert answer.status_code == 200 and "Gilt bis Betriebsschluss" in answer.text
    assert NUMBER in answer.text
    assert _config(engine).team_phone_today == NUMBER

    cards = client.get("/gui/fragments/pruefen", headers=HX).text
    assert "Teamnummer heute" in cards and "Als Standard übernehmen" in cards

    board = client.get("/gui/").text
    assert 'id="pruefen-liste"' in board and "Teamnummer heute" in board
    assert 'hx-get="/gui/einstellungen"' in board
    sheet = client.get("/gui/einstellungen", headers=HX).text
    assert 'hx-get="/gui/teamnummer"' in sheet and 'hx-get="/gui/tageszeiten"' in sheet


@pytest.mark.parametrize(
    ("number", "says"), [("12", "keine Telefonnummer"), ("+4930123456", "Betreiber")]
)
def test_a_number_that_cannot_ring_is_refused(client, engine, number, says):
    answer = client.post("/gui/teamnummer", data={"nummer": number}, headers=HX)
    assert answer.status_code == 409 and says in answer.text
    assert _cards(engine) == []


def test_without_listed_numbers_the_sheet_says_who_adds_them(client, monkeypatch):
    monkeypatch.setattr(settings, "transfer_targets", "")
    sheet = client.get("/gui/teamnummer", headers=HX).text
    assert "gibt der Betreiber frei" in sheet and '"nummer"' not in sheet


def test_today_closed_holds_and_a_later_longer_day_waits(client, engine):
    today = _today()
    closed = client.post(
        "/gui/tageszeiten",
        data={"datum": today.isoformat(), "art": "geschlossen"},
        headers=HX,
    )
    assert closed.status_code == 200 and "Gilt sofort" in closed.text
    later = today + timedelta(days=7)
    longer = client.post(
        "/gui/tageszeiten",
        data={
            "datum": later.isoformat(),
            "art": "zeiten",
            "von": "09:00",
            "bis": "23:30",
        },
        headers=HX,
    )
    assert longer.status_code == 200 and "Wartet auf den Inhaber" in longer.text
    cards = client.get("/gui/fragments/pruefen", headers=HX).text
    assert "Beantragt" in cards and "09:00 bis 23:30 Uhr" in cards
    assert "geschlossen" in cards


@pytest.mark.parametrize(
    "form",
    [
        {"datum": "", "art": "geschlossen"},
        {"datum": "2026-11-14", "art": "irgendwas"},
        {"datum": "2026-11-14", "art": "zeiten", "von": "", "bis": "22:00"},
    ],
)
def test_a_form_without_day_kind_or_times_is_refused(client, engine, form):
    answer = client.post("/gui/tageszeiten", data=form, headers=HX)
    assert answer.status_code == 422
    assert _cards(engine) == []


def test_the_sheet_shows_what_a_date_holds(client):
    later = _today() + timedelta(days=7)
    sheet = client.get(f"/gui/tageszeiten?datum={later.isoformat()}", headers=HX)
    assert sheet.status_code == 200 and f'value="{later.isoformat()}"' in sheet.text
    # A past or broken date shows today.
    past = client.get("/gui/tageszeiten?datum=2020-01-01", headers=HX).text
    assert f'value="{_today().isoformat()}"' in past


def test_the_owner_keeps_and_undoes_on_the_card(client, engine):
    client.post("/gui/teamnummer", data={"nummer": NUMBER}, headers=HX)
    (card,) = _cards(engine)
    kept = client.post(
        f"/gui/pruefen/{card.id}/uebernehmen", data=_seen(card), headers=HX
    )
    assert kept.status_code == 200 and "Teamnummer heute" not in kept.text
    assert _config(engine).team_phone == NUMBER

    day = _today() + timedelta(days=7)
    client.post(
        "/gui/tageszeiten",
        data={"datum": day.isoformat(), "art": "geschlossen"},
        headers=HX,
    )
    card = next(c for c in _cards(engine) if c.kind == "day_hours")
    undone = client.post(
        f"/gui/pruefen/{card.id}/zuruecknehmen", data=_seen(card), headers=HX
    )
    assert undone.status_code == 200
    assert {c.status for c in _cards(engine)} == {"kept", "undone"}
    assert client.post(f"/gui/pruefen/{card.id}/weg", headers=HX).status_code == 404


def test_a_card_changed_after_it_was_shown_is_not_kept(client, engine):
    """The buttons carry the version the owner saw (#596 P1); a newer one is
    refused with the fresh list and a note, and a button without it too."""
    client.post("/gui/teamnummer", data={"nummer": NUMBER}, headers=HX)
    (card,) = _cards(engine)
    cards = client.get("/gui/fragments/pruefen", headers=HX).text
    assert f'"gesehen": "{card.updated_at.isoformat()}"' in cards
    stale = (card.updated_at - timedelta(seconds=1)).isoformat()
    refused = client.post(
        f"/gui/pruefen/{card.id}/uebernehmen", data={"gesehen": stale}, headers=HX
    )
    assert refused.status_code == 200 and "gerade geändert" in refused.text
    assert "Teamnummer heute" in refused.text
    bare = client.post(f"/gui/pruefen/{card.id}/uebernehmen", headers=HX)
    assert "gerade geändert" in bare.text
    assert _config(engine).team_phone != NUMBER
    assert _cards(engine)[0].status == "open"


def test_a_span_over_midnight_needs_the_box(client, engine):
    form = {
        "datum": _today().isoformat(),
        "art": "zeiten",
        "von": "22:00",
        "bis": "10:00",
    }
    refused = client.post("/gui/tageszeiten", data=form, headers=HX)
    assert refused.status_code == 409 and "Mitternacht" in refused.text
    assert 'value="22:00"' in refused.text and 'value="10:00"' in refused.text
    assert _cards(engine) == []
    held = client.post("/gui/tageszeiten", data={**form, "nachts": "1"}, headers=HX)
    assert held.status_code == 200 and "Gilt sofort" in held.text


@pytest.mark.parametrize("typed", ["10:00:30", "10:00+02:00", "1000Z", "10:0Z"])
def test_times_with_seconds_or_an_offset_are_refused(client, engine, typed):
    form = {
        "datum": _today().isoformat(),
        "art": "zeiten",
        "von": typed,
        "bis": "22:00",
    }
    assert client.post("/gui/tageszeiten", data=form, headers=HX).status_code == 422
    assert _cards(engine) == []


def test_a_held_closure_lists_the_guests_to_call(client, engine):
    later = _today() + timedelta(days=7)
    with Session(engine) as session:
        session.add(
            Reservation(
                tenant_id=session.scalar(select(ServiceConfig.tenant_id)),
                source="tablet",
                status="confirmed",
                guest_name="Familie Bauer",
                phone="+491701234567",
                party_size=4,
                reserved_for=datetime.combine(later, time(19), tzinfo=BERLIN),
                idempotency_key="gui-closure-list",
            )
        )
        session.commit()
    before = client.get("/gui/fragments/pruefen", headers=HX).text
    assert "selbst anrufen" not in before
    client.post(
        "/gui/tageszeiten",
        data={"datum": later.isoformat(), "art": "geschlossen"},
        headers=HX,
    )
    cards = client.get("/gui/fragments/pruefen", headers=HX).text
    assert "Diese Gäste bitte selbst anrufen" in cards
    assert "19:00 Uhr, Familie Bauer, 4 Personen" in cards
    assert 'href="tel:+491701234567"' in cards


def _table(engine, day, name, phone="+491701234567", at=time(19)):
    with Session(engine) as session:
        session.add(
            Reservation(
                tenant_id=session.scalar(select(ServiceConfig.tenant_id)),
                source="tablet",
                status="confirmed",
                guest_name=name,
                phone=phone,
                party_size=2,
                reserved_for=datetime.combine(day, at, tzinfo=BERLIN),
                idempotency_key=f"gui-{name}",
            )
        )
        session.commit()


def test_the_list_stays_while_replacement_hours_wait(client, engine):
    later = _today() + timedelta(days=7)
    _table(engine, later, "Familie Bauer")
    form = {"datum": later.isoformat()}
    client.post("/gui/tageszeiten", data=form | {"art": "geschlossen"}, headers=HX)
    longer = form | {"art": "zeiten", "von": "10:00", "bis": "23:59"}
    client.post("/gui/tageszeiten", data=longer, headers=HX)
    cards = client.get("/gui/fragments/pruefen", headers=HX).text
    assert "Familie Bauer" in cards


def test_a_costly_number_is_shown_but_not_linked(client, engine):
    later = _today() + timedelta(days=7)
    _table(engine, later, "Premium", phone="+4990012345678")
    client.post(
        "/gui/tageszeiten",
        data={"datum": later.isoformat(), "art": "geschlossen"},
        headers=HX,
    )
    cards = client.get("/gui/fragments/pruefen", headers=HX).text
    assert "+4990012345678" in cards and "Teure Sondernummer" in cards
    assert "tel:+4990012345678" not in cards


def test_weekly_hours_back_on_a_longer_date_list_the_late_table(client, engine):
    later = _today() + timedelta(days=7)
    with Session(engine) as session:
        session.add(
            SpecialDay(
                tenant_id=session.scalar(select(ServiceConfig.tenant_id)),
                date=later,
                opens_at=time(10),
                closes_at=time(23),
                status="set",
            )
        )
        session.commit()
    _table(engine, later, "Late", at=time(22, 30))
    client.post(
        "/gui/tageszeiten",
        data={"datum": later.isoformat(), "art": "normal"},
        headers=HX,
    )
    assert "Late" in client.get("/gui/fragments/pruefen", headers=HX).text


@pytest.mark.parametrize(
    ("actor", "says", "not_says"),
    [
        ("operator:maxi", "Vom Betreiber geändert", "Vom Team geändert"),
        ("gui:tablet", "Vom Team geändert", "Vom Betreiber geändert"),
    ],
)
def test_the_card_names_who_set_the_number(client, engine, actor, says, not_says):
    """#811 question 1: the operator's number for today is not the team's."""
    with Session(engine) as session:
        tenant_id = session.scalars(select(ServiceConfig)).one().tenant_id
        change_team_phone(session, tenant_id, NUMBER, actor=actor)

    cards = client.get("/gui/fragments/pruefen", headers=HX).text

    assert says in cards and not_says not in cards


def _answer(client, engine, choice):
    (card,) = [c for c in _cards(engine) if c.status == "open"]
    answered = client.post(
        f"/gui/pruefen/{card.id}/{choice}", data=_seen(card), headers=HX
    ).text
    return answered, client.get("/gui/fragments/pruefen", headers=HX).text


def test_a_keep_refused_for_the_date_beside_says_why(client, engine):
    """#862: the staff's night waits; the owner opens the next date earlier
    meanwhile. Keeping it is refused, and the board says why."""
    later = _today() + timedelta(days=7)
    client.post(
        "/gui/tageszeiten",
        data={
            "datum": later.isoformat(),
            "art": "zeiten",
            "von": "18:00",
            "bis": "11:00",
            "nachts": "1",
        },
        headers=HX,
    )
    with Session(engine) as session:
        set_day_hours(
            session,
            session.scalar(select(ServiceConfig.tenant_id)),
            later + timedelta(days=1),
            DayHours(opens=time(10), closes=time(14)),
        )
    answered, _board = _answer(client, engine, "uebernehmen")
    assert "reicht in die Zeiten am" in answered
    assert [c.status for c in _cards(engine)] == ["open"]


def test_a_kept_closure_still_names_its_guests(client, engine):
    """The card goes with the owner's answer; the tables its hours leave out
    stay listed until they are cancelled or moved (#764)."""
    later = _today() + timedelta(days=7)
    _table(engine, later, "Familie Bauer")
    client.post(
        "/gui/tageszeiten",
        data={"datum": later.isoformat(), "art": "geschlossen"},
        headers=HX,
    )
    answered, board = _answer(client, engine, "uebernehmen")
    for text in (answered, board):
        assert "Familie Bauer" in text and "außerhalb der Öffnungszeiten" in text


def test_an_undone_longer_day_names_the_late_table(client, engine):
    """Today's longer hours hold at once and the agent books into them; the
    owner's undo leaves that table outside the hours (#764). 22:30 is past
    every weekday's closing time in the seed."""
    today = _today()
    client.post(
        "/gui/tageszeiten",
        data={
            "datum": today.isoformat(),
            "art": "zeiten",
            "von": "10:00",
            "bis": "23:59",
        },
        headers=HX,
    )
    _table(engine, today, "Spaet", at=time(22, 30))
    answered, board = _answer(client, engine, "zuruecknehmen")
    assert "Spaet" in answered and "Spaet" in board


def test_a_guest_on_a_card_is_listed_once(client, engine):
    later = _today() + timedelta(days=7)
    _table(engine, later, "Familie Bauer")
    client.post(
        "/gui/tageszeiten",
        data={"datum": later.isoformat(), "art": "geschlossen"},
        headers=HX,
    )
    cards = client.get("/gui/fragments/pruefen", headers=HX).text
    assert cards.count("Familie Bauer") == 1


def test_a_table_outside_the_hours_without_a_card_is_on_the_board(client, engine):
    """A holiday answered "Geschlossen" or a date set behind the owner's PIN
    opens no card: the board still names its guests (#764)."""
    later = _today() + timedelta(days=7)
    _table(engine, later, "Familie Bauer")
    with Session(engine) as session:
        session.add(
            SpecialDay(
                tenant_id=session.scalar(select(ServiceConfig.tenant_id)),
                date=later,
                closed=True,
                status="set",
            )
        )
        session.commit()
    assert _cards(engine) == []
    board = client.get("/gui").text
    assert "Familie Bauer" in board and "außerhalb der Öffnungszeiten" in board


def test_a_holiday_closed_under_a_waiting_card_names_its_guests(client, engine):
    """The answer becomes the waiting card's origin (#802), so the card lists
    nobody; the board still names the table the closed date leaves out (#764)."""
    later = _today() + timedelta(days=7)
    client.post(
        "/gui/tageszeiten",
        data={
            "datum": later.isoformat(),
            "art": "zeiten",
            "von": "10:00",
            "bis": "23:59",
        },
        headers=HX,
    )
    (card,) = _cards(engine)
    assert card.held is False
    _table(engine, later, "Familie Bauer")
    with Session(engine) as session:
        suggestion = SpecialDay(
            tenant_id=session.scalar(select(ServiceConfig.tenant_id)),
            date=later,
            note="Feiertag",
            like_weekday=6,
            status="suggested",
        )
        session.add(suggestion)
        session.commit()
        holidays.answer(session, suggestion.tenant_id, suggestion.id, holidays.CLOSED)
    board = client.get("/gui/fragments/pruefen", headers=HX).text
    assert "Familie Bauer" in board and "außerhalb der Öffnungszeiten" in board
