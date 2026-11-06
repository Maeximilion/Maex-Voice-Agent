"""gui/router: the sheet "Reservierung eintragen" (T-3.12, D30). The team enters a
table by hand: the form writes a draft, the summary shows the window's load, and
"Eintragen" is the confirming tap; a full window asks "Trotzdem eintragen"."""

import uuid
from datetime import date, datetime, time, timedelta
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.core.time import business_day
from api.models import (
    AuditLog,
    Call,
    DiningTable,
    FloorArea,
    Reservation,
    ReservationTable,
    TableCombination,
)
from api.tests import test_gui_router as gui

# The view's fixtures, and its clock fixed at noon of the business day.
client, engine, tenant_id, feste_uhr = (
    gui.client,
    gui.engine,
    gui.tenant_id,
    gui.feste_uhr,
)
TYPED, heute = gui.TYPED, gui.heute

BERLIN = ZoneInfo("Europe/Berlin")
# Fixed, not uuid4(): xdist workers must collect the same test ids.
UNKNOWN = uuid.UUID("00000000-0000-4000-8000-000000000001")


def ahead() -> date:
    """A day the seed opens (Monday is closed), after the real clock too: the
    domain reads its own clock, which `feste_uhr` does not stop."""
    day = business_day(tz_name="Europe/Berlin") + timedelta(days=2)
    return day + timedelta(days=1) if day.weekday() == 0 else day


@pytest.fixture
def tables(engine, tenant_id) -> dict[str, uuid.UUID]:
    """T1 and T2 combine, T3 stands alone."""
    with Session(engine) as s:
        area = FloorArea(
            tenant_id=tenant_id, name="Gastraum", width_cm=1000, depth_cm=700
        )
        s.add(area)
        s.flush()
        rows = {
            name: DiningTable(
                tenant_id=tenant_id,
                area_id=area.id,
                name=name,
                seats=seats,
                width_cm=120,
                depth_cm=80,
                x_cm=200 + 200 * i,
                y_cm=200,
            )
            for i, (name, seats) in enumerate((("T1", 4), ("T2", 4), ("T3", 6)))
        }
        s.add_all(rows.values())
        s.flush()
        low, high = sorted([rows["T1"].id, rows["T2"].id])
        s.add(TableCombination(tenant_id=tenant_id, table_a_id=low, table_b_id=high))
        s.commit()
        return {name: row.id for name, row in rows.items()}


def entry(**overrides) -> str:
    fields = {
        "schluessel": uuid.uuid4().hex,
        "datum": ahead().isoformat(),
        "uhrzeit": "19:00",
        "personen": "4",
        "name": "Freundin von Anna",
        "telefon": "",
        "hinweis": "",
        "tisch": "",
        **overrides,
    }
    return urlencode(fields)


def drafts(engine) -> list[Reservation]:
    with Session(engine) as s:
        return list(s.scalars(select(Reservation).order_by(Reservation.created_at)))


def full_evening(engine, tenant_id) -> None:
    """The seed's evening holds 40 guests: one confirmed agent table fills it."""
    with Session(engine) as s:
        call = Call(
            tenant_id=tenant_id,
            external_session_id=uuid.uuid4().hex,
            started_at=datetime.now(BERLIN),
            delete_after=datetime.now(BERLIN).date(),
        )
        s.add(call)
        s.flush()
        s.add(
            Reservation(
                tenant_id=tenant_id,
                call_id=call.id,
                status="confirmed",
                guest_name="Müller",
                phone="+4972215551234",
                party_size=40,
                reserved_for=datetime.combine(ahead(), time(18), tzinfo=BERLIN),
                idempotency_key=uuid.uuid4().hex,
            )
        )
        s.commit()


def test_the_column_heute_opens_the_sheet(client, tenant_id):
    assert 'hx-get="/gui/reservierung"' in client.get("/gui").text


def test_the_sheet_offers_today_the_tables_and_the_pairs(client, tenant_id, tables):
    text = client.get("/gui/reservierung").text

    assert "Reservierung eintragen" in text
    assert f'value="{business_day(tz_name="Europe/Berlin").isoformat()}"' in text
    assert "Ohne Tisch" in text
    assert "T3 (6 Plätze)" in text
    low, high = sorted([tables["T1"], tables["T2"]])
    assert f'value="{low}+{high}"' in text
    assert "T1 + T2 (8 Plätze)" in text
    assert 'name="schluessel"' in text


def test_the_team_enters_a_table_and_confirms_it(client, engine, tenant_id, tables):
    form = entry(telefon="07221 5551234", tisch=str(tables["T3"]), hinweis="Geburtstag")

    summary = client.post("/gui/reservierung", content=form, headers=TYPED)

    assert summary.status_code == 200
    assert "Freundin von Anna" in summary.text
    assert "4 Personen" in summary.text
    assert "Tisch T3" in summary.text
    assert "0 von 40 Plätzen belegt" in summary.text
    assert "Trotzdem eintragen" not in summary.text
    [draft] = drafts(engine)
    assert (draft.status, draft.source, draft.call_id) == ("draft", "tablet", None)
    assert draft.phone == "+4972215551234"
    assert f'hx-post="/gui/reservierung/{draft.id}/eintragen"' in summary.text

    done = client.post(
        f"/gui/reservierung/{draft.id}/eintragen", content="", headers=TYPED
    )

    assert done.status_code == 200
    assert "reservierung-eingetragen" in done.headers["HX-Trigger"]
    [booked] = drafts(engine)
    assert booked.status == "confirmed"


def test_a_second_tap_on_weiter_makes_no_second_draft(client, engine, tenant_id):
    form = entry()

    client.post("/gui/reservierung", content=form, headers=TYPED)
    again = client.post("/gui/reservierung", content=form, headers=TYPED)

    assert again.status_code == 200
    assert len(drafts(engine)) == 1


def test_a_full_window_is_entered_only_after_trotzdem(client, engine, tenant_id):
    full_evening(engine, tenant_id)

    summary = client.post("/gui/reservierung", content=entry(), headers=TYPED)

    assert "40 von 40 Plätzen belegt" in summary.text
    assert "Trotzdem eintragen" in summary.text
    assert "hx-confirm=" in summary.text
    draft = drafts(engine)[-1]
    path = f"/gui/reservierung/{draft.id}/eintragen"

    refused = client.post(path, content="", headers=TYPED)
    assert refused.status_code == 409
    assert "40 von 40 Plätzen belegt" in refused.text
    assert drafts(engine)[-1].status == "draft"

    taken = client.post(path, content="trotzdem=1", headers=TYPED)
    assert taken.status_code == 200
    assert drafts(engine)[-1].status == "confirmed"
    with Session(engine) as s:
        last = s.scalars(select(AuditLog).order_by(AuditLog.id.desc())).first()
    assert last.payload == {"source": "tablet", "override": True}


@pytest.mark.parametrize(
    "overrides, say",
    [
        ({"uhrzeit": ""}, "Bitte Datum, Uhrzeit, Personen und Namen prüfen."),
        ({"personen": "abc"}, "Bitte Datum, Uhrzeit, Personen und Namen prüfen."),
        ({"personen": "0"}, "Bitte Datum, Uhrzeit, Personen und Namen prüfen."),
        (
            {"personen": "99999999999"},
            "Bitte Datum, Uhrzeit, Personen und Namen prüfen.",
        ),
        ({"name": ""}, "Bitte Datum, Uhrzeit, Personen und Namen prüfen."),
        ({"name": "   "}, "Bitte einen Namen eintragen."),
        ({"telefon": "keine Nummer"}, None),
        ({"datum": "2020-01-01"}, "Dieser Zeitpunkt ist schon vorbei."),
        ({"tisch": "kein-tisch"}, "Bitte Datum, Uhrzeit, Personen und Namen prüfen."),
    ],
)
def test_what_the_form_refuses_writes_nothing_and_keeps_the_values(
    client, engine, tenant_id, overrides, say
):
    body = client.post("/gui/reservierung", content=entry(**overrides), headers=TYPED)

    assert body.status_code == 409
    if say:
        assert say in body.text
    if "name" not in overrides:
        assert 'value="Freundin von Anna"' in body.text
    assert drafts(engine) == []


def test_a_table_held_that_evening_is_refused_in_the_form(
    client, engine, tenant_id, tables
):
    first = client.post(
        "/gui/reservierung", content=entry(tisch=str(tables["T1"])), headers=TYPED
    )
    draft = drafts(engine)[0]
    client.post(f"/gui/reservierung/{draft.id}/eintragen", content="", headers=TYPED)
    assert first.status_code == 200

    again = client.post(
        "/gui/reservierung",
        content=entry(uhrzeit="21:00", tisch=str(tables["T1"]), name="Zweiter"),
        headers=TYPED,
    )

    assert again.status_code == 409
    assert "Tisch T1 ist um 21:00 Uhr schon vergeben." in again.text
    assert len(drafts(engine)) == 1


def test_a_table_taken_between_the_taps_blocks_the_entry(
    client, engine, tenant_id, tables
):
    """Two employees, two sheets: the second "Eintragen" is refused, no override."""
    for name in ("Erste", "Zweite"):
        client.post(
            "/gui/reservierung",
            content=entry(tisch=str(tables["T1"]), name=name),
            headers=TYPED,
        )
    first, second = drafts(engine)
    client.post(f"/gui/reservierung/{first.id}/eintragen", content="", headers=TYPED)

    body = client.post(
        f"/gui/reservierung/{second.id}/eintragen", content="trotzdem=1", headers=TYPED
    )

    assert body.status_code == 409
    assert "Tisch T1 ist um 19:00 Uhr schon vergeben." in body.text
    # No button at all: the table stays taken, whatever the team says.
    assert f"/gui/reservierung/{second.id}/eintragen" not in body.text
    assert drafts(engine)[1].status == "draft"


def test_an_unknown_reservation_answers_404(client, tenant_id):
    body = client.post(
        f"/gui/reservierung/{uuid.uuid4()}/eintragen", content="", headers=TYPED
    )

    assert body.status_code == 404


@pytest.mark.parametrize(
    "path", ["/gui/reservierung", f"/gui/reservierung/{UNKNOWN}/eintragen"]
)
def test_the_sheet_needs_the_csrf_header(client, tenant_id, path):
    body = client.post(
        path,
        content=entry(),
        headers={
            "HX-Request": "true",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )

    assert body.status_code == 403


def test_the_card_names_its_tables_and_hides_a_missing_number(
    client, engine, tenant_id, tables
):
    with Session(engine) as s:
        row = Reservation(
            tenant_id=tenant_id,
            source="tablet",
            status="confirmed",
            guest_name="Freundin von Anna",
            phone="",
            party_size=8,
            reserved_for=heute(19),
            idempotency_key=uuid.uuid4().hex,
        )
        s.add(row)
        s.flush()
        s.add_all(
            ReservationTable(tenant_id=tenant_id, reservation_id=row.id, table_id=t)
            for t in (tables["T2"], tables["T1"])
        )
        s.commit()

    text = client.get("/gui/fragments/heute").text

    assert "Tisch T1 + T2" in text
    assert '<div class="muted"></div>' not in text
