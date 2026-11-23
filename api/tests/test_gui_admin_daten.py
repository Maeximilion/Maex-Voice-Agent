"""The tab "Daten" of the admin area (#571, plan number 10, T-11.8): the
deletion periods and the requests of guests (legal check U2) behind the admin
PIN. These tests act as a signed-in device with the admin PIN (conftest); the
refusal without it is in test_gui_login.py. Erasing is not on the page: it
stays the server command with its backup and its ledger."""

import logging
import re
import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from api.config import settings
from api.core.time import add_months, to_local
from api.db import get_db
from api.domain.customers.requests import Details, erase, get_request, verify
from api.main import app
from api.models import AuditLog, Customer, DataSubjectRequest, Order, Tenant
from api.tests.test_domain_customers_requests import (
    OTHER_PHONE,
    PAST,
    PHONE,
    _call,
    _order,
)
from api.tests.test_domain_draft_order import _tenant
from api.tests.test_gui_admin_zeiten import _described, _fields, _tag
from scripts.seed import seed

HX = {"HX-Request": "true", "X-CSRF-Token": "testtoken"}
PAGE = "/gui/daten"
NEW = f"{PAGE}/anfrage"
NOTE = "Frau Müller, Bestellung vom 12.10., rief von 07221 555123 an"
# Details that name nobody: the answers below must not show a name from them.
NEUTRAL = "Angaben per Brief, liegen im Ordner"
DAY = "2026-10-12"
ZONE = "Europe/Berlin"


@pytest.fixture
def engine(migrated_db_url):
    engine = create_engine(migrated_db_url)
    with Session(engine) as s:
        # Seeded, with the test menu: an order needs a dish.
        _tenant(s, "Testbetrieb")
        s.commit()
    yield engine
    engine.dispose()


@pytest.fixture
def client(engine):
    def override_get_db():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app)
    client.cookies.set("maex_csrf", "testtoken")
    try:
        yield client
    finally:
        app.dependency_overrides.pop(get_db, None)


def _tenant_id(engine) -> uuid.UUID:
    with Session(engine) as session:
        return session.scalars(
            select(Tenant.id).where(Tenant.name == "Testbetrieb")
        ).one()


def _requests(engine) -> list[DataSubjectRequest]:
    with Session(engine) as session:
        return list(
            session.scalars(
                select(DataSubjectRequest).order_by(DataSubjectRequest.reference)
            )
        )


def _audits(engine, action=None) -> list[AuditLog]:
    with Session(engine) as session:
        query = select(AuditLog).where(AuditLog.entity == "data_subject_request")
        if action:
            query = query.where(AuditLog.action == action)
        return list(session.scalars(query.order_by(AuditLog.id)))


def _token(client) -> str:
    return _fields(client.get(f"{PAGE}/anfragen", headers=HX).text)["gesehen"]


def _log(client, **changes):
    form = {
        "gesehen": _token(client),
        "art": "erasure",
        "weg": "mail",
        "eingang": "",
        "notiz": NOTE,
        **changes,
    }
    return client.post(NEW, data=form, headers=HX)


def _logged(client, engine, **changes) -> str:
    answer = _log(client, **changes)
    assert answer.status_code == 200 and "Eingetragen" in answer.text, answer.text
    return _requests(engine)[-1].reference


def _a_guest(engine, name="Müller", phone=PHONE) -> uuid.UUID:
    """An order under the name from the guest's own line on 12.10.2026."""
    with Session(engine) as session:
        order = _order(session, _tenant_id(engine), name=name, phone=phone)
        session.commit()
        return order.id


def _check(client, reference, **details):
    form = {"name": "Müller", "tag-1": DAY, **details}
    return client.post(f"{NEW}/{reference}/pruefen", data=form, headers=HX)


# --- the page ---------------------------------------------------------------------


def test_the_page_shows_the_frame_the_periods_and_no_request(client, engine):
    page = client.get(PAGE)
    assert page.status_code == 200
    assert page.headers["Cache-Control"] == "no-store"
    assert '<a href="/gui/daten" aria-current="page">Daten</a>' in page.text
    for tab in ("/gui/zeiten", "/gui/lieferung", "/gui/anrufe", "/gui/geraete"):
        assert f'href="{tab}"' in page.text
    assert "Keine offene Anfrage" in page.text
    assert "Löschfristen" in page.text and "Inhaber-Bereich schließen" in page.text
    assert f"{settings.retention_caller_id_days} Tage nach dem Anruf" in page.text


def test_a_period_is_the_setting_not_a_number_of_the_page(client, engine, monkeypatch):
    monkeypatch.setattr(settings, "retention_caller_id_days", 41)
    monkeypatch.setattr(settings, "retention_request_log_years", 5)
    page = client.get(PAGE).text
    assert "41 Tage nach dem Anruf" in page
    assert "bis zum Ende des 5. Kalenderjahres" in page


# --- a new request ----------------------------------------------------------------


def test_a_request_is_logged_with_its_deadline_and_the_owner_in_the_audit_log(
    client, engine
):
    reference = _logged(client, engine)
    (row,) = _requests(engine)
    today = to_local(datetime.now(UTC), ZONE).date()
    assert re.fullmatch(rf"dsr_{today.year}_001", reference)
    assert (row.kind, row.channel, row.verification_note) == ("erasure", "mail", NOTE)
    assert row.deadline == add_months(today, 1)
    (audit,) = _audits(engine, "data_subject.request_logged")
    assert audit.actor == "gui:owner" and audit.entity_id == row.id
    assert "Müller" not in str(audit.payload)

    listed = client.get(PAGE).text
    assert reference in listed and "Löschung" in listed
    assert f"{row.deadline:%d.%m.%Y}" in listed
    # The list names no guest: the note is shown with its request only.
    assert "Müller" not in listed and "555123" not in listed


def test_a_second_tap_on_eintragen_writes_no_second_line(client, engine):
    form = {
        "gesehen": _token(client),
        "art": "access",
        "weg": "phone",
        "eingang": "",
        "notiz": NOTE,
    }
    first = client.post(NEW, data=form, headers=HX)
    second = client.post(NEW, data=form, headers=HX)
    assert "Eingetragen" in first.text
    assert "Die Liste hat sich gerade geändert" in second.text
    assert len(_requests(engine)) == 1
    # The fresh form carries the fresh token and no longer the typed note.
    assert _fields(second.text)["gesehen"] != form["gesehen"]
    assert "Müller" not in second.text


def test_a_day_of_receipt_that_has_passed_sets_the_deadline_from_it(client, engine):
    day = to_local(datetime.now(UTC), ZONE).date() - timedelta(days=10)
    _logged(client, engine, eingang=day.isoformat())
    (row,) = _requests(engine)
    assert to_local(row.received_at, ZONE).date() == day
    assert row.deadline == add_months(day, 1)


@pytest.mark.parametrize(
    ("changes", "said"),
    [
        ({"eingang": "2999-01-01"}, "nicht in der Zukunft"),
        ({"eingang": "12.10.2026"}, "als Datum"),
        ({"notiz": "x" * 2001}, "zu lang"),
        ({"notiz": "x" + chr(0)}, "nicht speichern"),
        ({"art": "everything"}, "nicht geklappt"),
        ({"weg": "fax"}, "nicht geklappt"),
    ],
)
def test_a_refused_request_writes_nothing_and_says_why(client, engine, changes, said):
    answer = _log(client, **changes)
    assert answer.status_code == 200 and said in answer.text, answer.text
    assert _requests(engine) == [] and _audits(engine) == []
    # What was typed is shown again, against the token it was typed with.
    if "notiz" not in changes:
        assert NOTE in answer.text
    assert _fields(answer.text)["gesehen"] == _token(client)


def test_a_note_with_signs_of_html_goes_through_unchanged(client, engine):
    note = 'Herr <b>O\'Neil</b> & "Söhne"'
    reference = _logged(client, engine, notiz=note)
    assert _requests(engine)[0].verification_note == note
    shown = client.get(f"{NEW}/{reference}", headers=HX).text
    assert "<b>O" not in shown and "&lt;b&gt;O" in shown


# --- one request ------------------------------------------------------------------


def test_a_request_opens_with_its_note_and_what_can_be_done(client, engine):
    reference = _logged(client, engine)
    shown = client.get(f"{NEW}/{reference}", headers=HX)
    assert shown.status_code == 200
    assert shown.headers["Cache-Control"] == "no-store"
    for text in (NOTE, "Löschung", "E-Mail oder Brief", "Person prüfen"):
        assert text in shown.text, text
    assert "Frist verlängern" in shown.text
    assert "Als beantwortet eintragen" in shown.text
    # The erasure itself is the server's: the page says so and offers no button.
    assert "Auf dem Server noch nicht gelaufen" in shown.text
    assert "/loeschen" not in shown.text and "erase" not in shown.text


def test_an_access_request_names_no_run_on_the_server(client, engine):
    reference = _logged(client, engine, art="access")
    shown = client.get(f"{NEW}/{reference}", headers=HX).text
    assert "Auskunft" in shown and "Auf dem Server" not in shown


@pytest.mark.parametrize("reference", ["dsr_2026_999", "x", "dsr_2026_001%27"])
def test_no_request_of_that_reference(client, engine, reference):
    assert client.get(f"{NEW}/{reference}", headers=HX).status_code == 404
    for step in ("frist", "antwort", "pruefen"):
        answer = client.post(f"{NEW}/{reference}/{step}", data={}, headers=HX)
        assert answer.status_code == 404, step


def test_another_restaurants_request_is_no_request_here(client, engine):
    reference = _logged(client, engine)
    with Session(engine) as session:
        other = uuid.UUID(seed(session, tenant_name="Nachbar", timezone=ZONE).tenant_id)
        row = session.scalars(select(DataSubjectRequest)).one()
        row.tenant_id = other
        session.commit()
    assert client.get(f"{NEW}/{reference}", headers=HX).status_code == 404
    for step, data in (
        ("frist", {}),
        ("antwort", {"ergebnis": "refused"}),
        ("pruefen", {"name": "Müller", "tag-1": DAY}),
    ):
        answer = client.post(f"{NEW}/{reference}/{step}", data=data, headers=HX)
        assert answer.status_code == 404, step
    (row,) = _requests(engine)
    assert row.answered_at is None and len(_audits(engine)) == 1
    assert "Keine offene Anfrage" in client.get(PAGE).text


# --- the deadline -----------------------------------------------------------------


def test_the_deadline_moves_once(client, engine):
    reference = _logged(client, engine)
    received = to_local(_requests(engine)[0].received_at, ZONE).date()
    moved = client.post(f"{NEW}/{reference}/frist", headers=HX)
    assert "Frist verlängert" in moved.text
    assert _requests(engine)[0].deadline == add_months(received, 3)
    assert "Frist verlängern (" not in moved.text
    again = client.post(f"{NEW}/{reference}/frist", headers=HX)
    assert "nicht verlängern" in again.text
    (audit,) = _audits(engine, "data_subject.deadline_extended")
    assert audit.actor == "gui:owner"


def test_after_the_first_month_the_page_moves_no_deadline(client, engine):
    """An overdue request must not read as in time after one tap."""
    day = to_local(datetime.now(UTC), ZONE).date() - timedelta(days=40)
    reference = _logged(client, engine, eingang=day.isoformat())
    shown = client.get(f"{NEW}/{reference}", headers=HX).text
    assert "überfällig" in shown and "Frist verlängern (" not in shown
    answer = client.post(f"{NEW}/{reference}/frist", headers=HX)
    assert "nicht verlängern" in answer.text
    assert _requests(engine)[0].deadline == add_months(day, 1)
    assert _audits(engine, "data_subject.deadline_extended") == []


# --- the answer -------------------------------------------------------------------


def test_an_access_request_is_answered_and_leaves_the_open_list(client, engine):
    reference = _logged(client, engine, art="access")
    answer = client.post(
        f"{NEW}/{reference}/antwort", data={"ergebnis": "done"}, headers=HX
    )
    assert "Als beantwortet eingetragen" in answer.text
    assert (
        "Keine offene Anfrage" in answer.text and "Zuletzt beantwortet" in answer.text
    )
    (row,) = _requests(engine)
    today = to_local(datetime.now(UTC), ZONE).date()
    assert row.outcome == "done"
    assert row.delete_after == date(
        today.year + settings.retention_request_log_years, 12, 31
    )
    (audit,) = _audits(engine, "data_subject.request_answered")
    assert audit.actor == "gui:owner" and audit.payload == {"outcome": "done"}

    # Answered once: a second answer changes nothing, and nothing is checked
    # for a request that is closed.
    again = client.post(
        f"{NEW}/{reference}/antwort", data={"ergebnis": "refused"}, headers=HX
    )
    assert "schon beantwortet" in again.text
    assert _requests(engine)[0].outcome == "done"
    shown = client.get(f"{NEW}/{reference}", headers=HX).text
    assert "beantwortet: erledigt" in shown
    assert "Person prüfen" not in shown and "Als beantwortet eintragen" not in shown


@pytest.mark.parametrize("kind", ["erasure", "objection"])
def test_an_erasure_is_not_erledigt_before_it_ran_on_the_server(client, engine, kind):
    """The log must not say erased while every row is still there."""
    reference = _logged(client, engine, art=kind)
    answer = client.post(
        f"{NEW}/{reference}/antwort", data={"ergebnis": "done"}, headers=HX
    )
    assert "noch nichts gelaufen" in answer.text
    assert _requests(engine)[0].answered_at is None
    assert _audits(engine, "data_subject.request_answered") == []
    # Another outcome needs no run.
    other = client.post(
        f"{NEW}/{reference}/antwort", data={"ergebnis": "not_verified"}, headers=HX
    )
    assert "Als beantwortet eingetragen" in other.text
    assert _requests(engine)[0].outcome == "not_verified"


def test_an_erasure_that_ran_on_the_server_is_answered_as_erledigt(client, engine):
    reference = _logged(client, engine)
    _a_guest(engine)
    tenant_id = _tenant_id(engine)
    with Session(engine) as session:
        request = get_request(session, tenant_id, reference)
        person = verify(
            session,
            tenant_id,
            Details(name="Müller", days=(date(2026, 10, 12),)),
            ZONE,
            datetime.now(UTC),
        )
        erase(session, request, person)
        session.commit()
    shown = client.get(f"{NEW}/{reference}", headers=HX).text
    assert "Auf dem Server gelaufen" in shown
    answer = client.post(
        f"{NEW}/{reference}/antwort", data={"ergebnis": "done"}, headers=HX
    )
    assert "Als beantwortet eingetragen" in answer.text


def test_an_outcome_nobody_offered_is_refused(client, engine):
    reference = _logged(client, engine, art="access")
    for outcome in ("", "erased", "done; drop table"):
        answer = client.post(
            f"{NEW}/{reference}/antwort", data={"ergebnis": outcome}, headers=HX
        )
        assert answer.status_code == 200 and "nicht geklappt" in answer.text
    assert _requests(engine)[0].answered_at is None


# --- the check of the person --------------------------------------------------------


def test_a_name_with_the_day_shows_the_persons_rows_and_audits_the_look(client, engine):
    reference = _logged(client, engine)
    order_id = _a_guest(engine)
    answer = _check(client, reference)
    assert answer.status_code == 200
    assert answer.headers["Cache-Control"] == "no-store"
    for text in ("Bestätigt", "Bestellungen (1)", "Müller", PHONE, "Bitte klingeln"):
        assert text in answer.text, text
    assert "12,00 €" in answer.text and "übergeben" in answer.text
    assert "Gerichte:</span> <bdi>1 x 23 " in answer.text
    # The copy goes only where the rows prove it reaches the guest (#888 S4).
    assert "Rückruf an die geprüfte Nummer" in answer.text
    # What was typed stays in the form for the next check.
    assert _fields(answer.text)["name"] == "Müller"
    (look,) = _audits(engine, "data_subject.rows_read")
    assert look.actor == "gui:owner" and look.payload == {}
    assert look.entity_id == _requests(engine)[0].id
    # Nothing was changed by the look.
    with Session(engine) as session:
        name = session.scalar(select(Order.customer_name).where(Order.id == order_id))
        assert name == "Müller"


@pytest.mark.parametrize(
    ("details", "said"),
    [
        ({"name": "", "tag-1": "", "nummern": PHONE}, "Eine Nummer oder ein Name"),
        ({"name": "Müller", "tag-1": ""}, "Eine Nummer oder ein Name"),
        ({"name": "Müller", "tag-1": "", "strasse": "Hauptstraße"}, "allein"),
        ({"name": "Schmidt"}, "Nicht bestätigt"),
        ({"tag-1": "2026-10-13"}, "Nicht bestätigt"),
        ({"tag-1": "12.10.2026"}, "als Datum"),
        ({"nummern": "abc"}, "nicht lesbar"),
        ({"name": "M" * 121}, "zu lang"),
        (
            {"tag-1": "", "strasse": "B", "hausnummer": "1", "plz": "1" + chr(0)},
            "nicht vorkommen",
        ),
        ({"name": "Müller" + chr(0)}, "nicht vorkommen"),
        ({"nummern": ",".join(["+497221555123"] * 6)}, "zu viele"),
        ({"claim-nonsense": "1"}, "angekreuzte Zeile"),
        ({f"claim-{uuid.UUID(int=7)}": "1"}, "angekreuzte Zeile"),
    ],
)
def test_details_that_verify_nobody_show_nothing_of_a_guest(
    client, engine, details, said
):
    reference = _logged(client, engine, notiz=NEUTRAL)
    _a_guest(engine)
    answer = _check(client, reference, **details)
    assert answer.status_code == 200 and said in answer.text, answer.text
    assert "Bestätigt" not in answer.text and "Bitte klingeln" not in answer.text
    assert PHONE not in answer.text.replace(details.get("nummern", "\x00"), "")


def test_a_number_alone_opens_nothing_also_when_it_is_the_guests(client, engine):
    """Legal check U2: a number is no person."""
    reference = _logged(client, engine, notiz=NEUTRAL)
    _a_guest(engine)
    answer = _check(client, reference, **{"name": "", "tag-1": "", "nummern": PHONE})
    assert "Bestätigt" not in answer.text and "Müller" not in answer.text


def test_a_namesake_on_another_number_is_not_in_the_rows(client, engine):
    reference = _logged(client, engine, notiz=NEUTRAL)
    _a_guest(engine)
    with Session(engine) as session:
        _order(
            session,
            _tenant_id(engine),
            phone=OTHER_PHONE,
            ready_at=PAST + timedelta(days=3),
        )
        session.commit()
    answer = _check(client, reference)
    assert "Bestellungen (1)" in answer.text
    assert OTHER_PHONE not in answer.text


def test_the_name_on_two_numbers_on_one_day_asks_for_the_number(client, engine):
    reference = _logged(client, engine, notiz=NEUTRAL)
    _a_guest(engine)
    _a_guest(engine, phone=OTHER_PHONE)
    asked = _check(client, reference)
    assert "mehreren Rufnummern" in asked.text and "Bestätigt" not in asked.text
    named = _check(client, reference, nummern="07221 555123")
    assert "Bestellungen (1)" in named.text and OTHER_PHONE not in named.text


def test_a_call_with_only_the_number_waits_for_the_guests_word(client, engine):
    reference = _logged(client, engine, notiz=NEUTRAL)
    _a_guest(engine)
    with Session(engine) as session:
        call = _call(session, _tenant_id(engine), started_at=PAST + timedelta(days=1))
        session.commit()
        call_id = call.id
    offered = _check(client, reference)
    assert "Beim Gast nachfragen" in offered.text
    assert f'name="claim-{call_id}"' in offered.text
    assert "Anrufe (1)" in offered.text
    # Ticked and sent again with the same details, the call is the person's.
    form = {**_fields(offered.text), f"claim-{call_id}": "1"}
    claimed = client.post(f"{NEW}/{reference}/pruefen", data=form, headers=HX)
    assert "Anrufe (2)" in claimed.text
    # Its box stays, ticked: the next check sends it again, and the owner sees
    # which rows to name to the operator.
    assert f'name="claim-{call_id}" value="1" checked' in claimed.text
    assert "vom Gast bestätigt" in claimed.text
    again = client.post(
        f"{NEW}/{reference}/pruefen", data=_fields(claimed.text), headers=HX
    )
    assert "Anrufe (2)" in again.text


def test_an_open_table_is_named_as_what_stops_an_erasure(client, engine):
    from api.tests.test_domain_customers_requests import _reservation

    reference = _logged(client, engine, notiz=NEUTRAL)
    _a_guest(engine)
    with Session(engine) as session:
        _reservation(
            session,
            _tenant_id(engine),
            reserved_for=datetime.now(UTC) + timedelta(days=5),
        )
        session.commit()
    answer = _check(client, reference)
    assert "Das hält eine Löschung noch auf" in answer.text
    assert "Reservierung vom" in answer.text


def test_the_address_verifies_the_person_too(client, engine):
    from api.tests.test_domain_customers_requests import _address, _customer

    reference = _logged(client, engine, notiz=NEUTRAL)
    with Session(engine) as session:
        tenant_id = _tenant_id(engine)
        _address(session, tenant_id, _customer(session, tenant_id))
        session.commit()
    answer = _check(
        client,
        reference,
        **{
            "tag-1": "",
            "strasse": "hauptstrasse",
            "hausnummer": "12 A",
            "plz": "76530",
        },
    )
    assert "Kundendaten (1)" in answer.text and "Adressen (1)" in answer.text
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(Customer)) == 1


def test_nothing_is_checked_for_a_request_already_answered(client, engine):
    reference = _logged(client, engine, art="access")
    _a_guest(engine)
    client.post(f"{NEW}/{reference}/antwort", data={"ergebnis": "done"}, headers=HX)
    answer = _check(client, reference)
    assert "schon beantwortet" in answer.text
    assert "Bitte klingeln" not in answer.text and PHONE not in answer.text
    assert _audits(engine, "data_subject.rows_read") == []


def test_no_typed_detail_and_no_row_goes_to_a_log(client, engine, caplog):
    reference = _logged(client, engine)
    _a_guest(engine)
    with caplog.at_level(logging.DEBUG):
        _check(client, reference, nummern=PHONE)
        _check(client, reference, name="Schmidt")
        _log(client, notiz="Herr Schmidt, Hauptstraße 12a")
    logged = " ".join(record.getMessage() for record in caplog.records)
    for secret in ("Müller", "Schmidt", PHONE, "555123", "Hauptstraße", "klingeln"):
        assert secret not in logged, secret


def test_a_typed_name_with_signs_of_html_is_shown_as_text(client, engine):
    reference = _logged(client, engine, notiz=NEUTRAL)
    answer = _check(client, reference, name='"><script>alert(1)</script>')
    assert "<script>alert" not in answer.text
    assert "&lt;script&gt;" in answer.text


# --- the page's token ---------------------------------------------------------------


@pytest.mark.parametrize(
    "headers",
    [{}, {"HX-Request": "true"}, {"HX-Request": "true", "X-CSRF-Token": "wrong"}],
)
def test_a_write_without_the_pages_token_is_refused(client, engine, headers):
    reference = _logged(client, engine)
    _a_guest(engine)
    before = len(_audits(engine))
    form = {"gesehen": _token(client), "art": "access", "weg": "mail", "notiz": ""}
    assert client.post(NEW, data=form, headers=headers).status_code == 403
    for step, data in (
        ("frist", {}),
        ("antwort", {"ergebnis": "refused"}),
        ("pruefen", {"name": "Müller", "tag-1": DAY}),
    ):
        answer = client.post(f"{NEW}/{reference}/{step}", data=data, headers=headers)
        assert answer.status_code == 403, step
        assert "Bitte klingeln" not in answer.text
    (row,) = _requests(engine)
    assert row.answered_at is None and len(_audits(engine)) == before


# --- forms and accessibility (#829) -----------------------------------------------


@pytest.mark.parametrize("note", ["", "  \n "])
def test_a_request_without_the_guests_details_is_not_logged(client, engine, note):
    """#829-11 as Maxi decided: no details, no request and no deadline."""
    answer = _log(client, notiz=note)
    html = answer.text
    assert answer.status_code == 200 and "Eingetragen" not in html
    assert 'aria-invalid="true"' in _tag(html, "notiz")
    assert "Angaben des Gastes" in _described(html, "notiz")
    assert "data-fokus" in html
    assert _requests(engine) == [] and _audits(engine) == []


def test_the_details_field_is_marked_as_needed_and_tied_to_its_help(client):
    html = client.get(PAGE).text
    tag = _tag(html, "notiz")
    assert 'aria-required="true"' in tag
    assert "Der Name mit dem Tag" in _described(html, "notiz")
    form = html.split('hx-post="/gui/daten/anfrage"', 1)[1].split("</form>", 1)[0]
    assert "(Pflichtfeld)" in form
    assert (
        'hx-disabled-elt="find button"'
        in html.split('hx-post="/gui/daten/anfrage"', 1)[1]
    )


def test_every_fault_of_a_new_request_comes_at_once(client, engine):
    """#862: faults came in three rounds (unreadable date; then empty note or
    future date; then a note too long). One answer names them all."""
    answer = _log(client, eingang="12.10.2026", notiz="x" * 2001)
    html = answer.text
    assert "als Datum" in html and "zu lang" in html, html
    answer = _log(client, eingang="12.10.2026", notiz="")
    assert "als Datum" in answer.text and "Angaben des Gastes" in answer.text
    answer = _log(client, eingang="2999-01-01", notiz="x" + chr(0))
    assert "nicht in der Zukunft" in answer.text and "nicht speichern" in answer.text
    assert _requests(engine) == [] and _audits(engine) == []


def test_a_note_refused_as_too_long_is_shown_in_full(client, engine):
    """#862: it came back cut to 2000 characters, and saving that stored the cut
    note without a word."""
    note = "Herr Schmidt " + "x" * 2100
    answer = _log(client, notiz=note)
    assert "zu lang" in answer.text
    assert note in answer.text.replace("&#34;", '"')
    assert _requests(engine) == []
