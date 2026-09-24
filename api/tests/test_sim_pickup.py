"""Abholung im Text-Telefon: Transkript -> Skript-Modell -> Agent -> DB (sim/scripted_order.py).

Geprueft wird der Datenbankzustand, nicht der Text des Agenten (docs/08 §3) - bis
auf die Saetze, die der Gast wirklich hoeren muss (Rueckfrage, Abholcode).
"""

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import Session

from api.domain.menu.importer import apply, parse
from api.models import Callback, MenuItem, Order, OrderItem
from api.tests.test_domain_menu_search import KARTE
from scripts.seed import seed
from sim.replay import replay
from sim.session import resolve_tenant

BERLIN = ZoneInfo("Europe/Berlin")
NOW = datetime(2026, 9, 15, 18, 0, tzinfo=BERLIN)  # Dienstag, Abholung offen
CASE = (
    Path(__file__).resolve().parents[2]
    / "evals"
    / "cases"
    / "abholung_0001_zwei_positionen_mit_option.json"
)


@pytest.fixture
def session(migrated_db_url):
    engine = create_engine(migrated_db_url)
    with Session(engine) as s:
        yield s
    engine.dispose()


@pytest.fixture
def tenant(session):
    seed(session, tenant_name="Testbetrieb", timezone="Europe/Berlin")
    tenant = resolve_tenant(session, "Testbetrieb")
    plan = parse(KARTE)
    assert plan.ok, plan.errors
    apply(session, tenant.id, plan, now=NOW)
    return tenant


def case(*lines: str) -> dict:
    return {
        "id": "test",
        "transcript": [{"role": "customer", "text": line} for line in lines],
    }


def said(turns) -> str:
    return " ".join(" ".join(t.say) for t in turns)


def orders(session) -> list[Order]:
    session.expire_all()
    return list(session.scalars(select(Order)))


def positions(session, order: Order) -> list[tuple[str, int, list]]:
    rows = session.execute(
        select(MenuItem.number, OrderItem.quantity, OrderItem.options)
        .join(MenuItem, MenuItem.id == OrderItem.menu_item_id)
        .where(OrderItem.order_id == order.id)
        .order_by(OrderItem.created_at)
    ).all()
    return [(n, q, [o["option"] for o in opts]) for n, q, opts in rows]


def test_fall_aus_evals_landet_bestaetigt_mit_abholcode(session, tenant):
    fall = json.loads(CASE.read_text(encoding="utf-8"))
    call, turns = replay(session, fall, tenant, now=NOW)

    [order] = orders(session)
    assert order.status == "confirmed"
    assert order.pickup_code == "A1"
    assert order.customer_name == "Mueller"
    assert positions(session, order) == [("23", 2, []), ("47", 1, ["Huhn"])]
    text = said(turns)
    assert "KI-Assistent" in text
    assert "Welche Auswahl bei Fleisch möchten Sie zu Ente knusprig" in text
    assert "Ihr Abholcode ist A1." in text
    # Seed laeuft im Modus shadow: nichts geht ohne Freigabe an die Kueche.
    assert "Das Team bestätigt die Bestellung gleich noch." in text
    assert call.finish().outcome == "completed"


def test_mehrdeutiges_gericht_wird_angeboten_nicht_gewaehlt(session, tenant):
    """ "Suppe" haengt an 12 und 13: das Skript bietet beide an und nimmt erst
    die Nummer, die der Gast nennt (CLAUDE.md §2 Regel 2)."""
    _, turns = replay(
        session,
        case(
            "Ich moechte etwas zum Abholen bestellen.",
            "Eine Suppe.",
            "Die 13 bitte.",
            "Das wars.",
            "Auf den Namen Mueller.",
            "0721 5551234",
            "Ja.",
        ),
        tenant,
        now=NOW,
    )

    assert "Meinen Sie Nummer 12 Wan-Tan-Suppe oder Nummer 13 Pho Bo?" in said(turns)
    [order] = orders(session)
    assert order.status == "confirmed"
    assert positions(session, order) == [("13", 1, [])]


def test_nein_beim_vorlesen_bestaetigt_nichts_und_faengt_neu_an(session, tenant):
    _, turns = replay(
        session,
        case(
            "Ich moechte etwas zum Abholen bestellen.",
            "Die 23.",
            "Nein, das wars.",
            "Auf den Namen Mueller.",
            "0721 5551234",
            "Nein, das stimmt nicht.",
            "Zweimal die 24.",
            "Das wars.",
            "Ja, passt.",
        ),
        tenant,
        now=NOW,
    )

    assert "Dann noch einmal von vorn" in said(turns)
    drafts = {o.status: o for o in orders(session)}
    assert set(drafts) == {"draft", "confirmed"}
    # Bestaetigt ist nur die Korrektur, der erste Entwurf bleibt liegen.
    assert positions(session, drafts["confirmed"]) == [("24", 2, [])]
    assert positions(session, drafts["draft"]) == [("23", 1, [])]


def test_unbekanntes_gericht_faellt_nicht_still_weg(session, tenant):
    """Ein Teil ohne Treffer wird ausgesprochen, der gefundene bleibt im Warenkorb."""
    _, turns = replay(
        session,
        case(
            "Ich moechte etwas zum Abholen bestellen.",
            "Die 23 und die 99.",
        ),
        tenant,
        now=NOW,
    )

    text = said(turns)
    assert "Die Nummer 99 habe ich nicht auf der Karte" in text
    assert "Darf es noch etwas sein?" in text
    assert orders(session) == []


def test_ausverkauftes_gericht_kommt_nicht_in_den_warenkorb(session, tenant):
    session.execute(
        update(MenuItem)
        .where(MenuItem.tenant_id == tenant.id, MenuItem.number == "23")
        .values(sold_out_until=datetime(2026, 9, 15, 23, 0, tzinfo=BERLIN))
    )
    session.commit()
    _, turns = replay(
        session,
        case("Ich moechte etwas zum Abholen bestellen.", "Die 23."),
        tenant,
        now=NOW,
    )

    text = said(turns)
    assert "ist heute leider aus" in text
    assert "Was möchten Sie bestellen?" in text


def test_lieferung_bleibt_ein_rueckruf(session, tenant):
    replay(
        session,
        case("Koennen Sie mir das liefern?", "0721 5551234"),
        tenant,
        now=NOW,
    )
    session.expire_all()
    assert [c.reason for c in session.scalars(select(Callback))] == ["out_of_scope"]
    assert orders(session) == []


# --- Rufnummernerkennung (Maxi, PR #127) --------------------------------------------


def test_rufnummer_kommt_aus_der_erkennung(session, tenant):
    """Der Gast ruft an, seine Nummer ist bekannt: der Agent fragt nicht danach."""
    fall = {
        **case(
            "Ich moechte etwas zum Abholen bestellen.",
            "Die 23.",
            "Das wars.",
            "Auf den Namen Mueller.",
            "Ja.",
        ),
        "caller_id": "+497215551234",
    }
    _, turns = replay(session, fall, tenant, now=NOW)

    assert "Telefonnummer" not in said(turns)
    [order] = orders(session)
    assert order.status == "confirmed"
    assert order.phone == "+497215551234"


def test_andere_nummer_vom_gast_gilt(session, tenant):
    """Nennt der Gast von sich aus eine andere Nummer, gilt diese."""
    fall = {
        **case(
            "Ich moechte etwas zum Abholen bestellen.",
            "Die 23.",
            "Das wars.",
            "Auf den Namen Mueller, erreichbar unter 0721 9998877.",
            "Ja.",
        ),
        "caller_id": "+497215551234",
    }
    replay(session, fall, tenant, now=NOW)

    [order] = orders(session)
    assert order.phone == "+497219998877"


def test_unterdrueckte_nummer_wird_erfragt(session, tenant):
    _, turns = replay(
        session,
        case(
            "Ich moechte etwas zum Abholen bestellen.",
            "Die 23.",
            "Das wars.",
            "Auf den Namen Mueller.",
        ),
        tenant,
        now=NOW,
    )
    assert "Telefonnummer" in said(turns)


def test_fall_mit_rufnummer_aus_der_erkennung(session, tenant):
    fall = json.loads(
        (CASE.parent / "abholung_0002_rufnummer_aus_erkennung.json").read_text(
            encoding="utf-8"
        )
    )
    _, turns = replay(session, fall, tenant, now=NOW)

    assert "Telefonnummer" not in said(turns)
    [order] = orders(session)
    assert order.status == "confirmed"
    assert (order.customer_name, order.phone) == ("Schmidt", "+497215551234")


# --- Mehreres hintereinander (Maxi, PR #127) -----------------------------------------


@pytest.mark.parametrize(
    "gesagt",
    ["Die 23, die 24 und die 13.", "Frühlingsrollen, Sommerrollen und Pho Bo."],
)
def test_mehreres_hintereinander_wird_wiederholt_und_aufgenommen(
    session, tenant, gesagt
):
    """Nummern oder Namen hintereinander: der Agent wiederholt sofort alles
    Verstandene und nimmt alles auf, ohne dass der Gast etwas wiederholen muss."""
    _, turns = replay(
        session,
        case(
            "Ich moechte etwas zum Abholen bestellen.",
            gesagt,
            "Nein, das wars.",
            "Auf den Namen Mueller.",
            "0721 5551234",
            "Ja.",
        ),
        tenant,
        now=NOW,
    )

    text = said(turns)
    if gesagt.startswith("Die"):
        assert "Nummer 23, Nummer 24 und Nummer 13" in text
    else:
        # Der Name, wie er auf der Karte steht, nicht wie der Gast ihn sagte.
        assert (
            "Nummer 23 Frühlingsrollen (4 Stück), Nummer 24 Sommerrollen mit "
            "Garnelen und Nummer 13 Pho Bo"
        ) in text
    [order] = orders(session)
    assert order.status == "confirmed"
    assert [p[0] for p in positions(session, order)] == ["23", "24", "13"]


def test_zwei_rueckfragen_nacheinander_keine_faellt_weg(session, tenant):
    """Zwei unklare Teile in einem Satz: erst die eine Rueckfrage, dann die
    andere. Keiner der beiden faellt still weg."""
    _, turns = replay(
        session,
        case(
            "Ich moechte etwas zum Abholen bestellen.",
            "Eine Suppe und eine Ente.",
            "Die 13.",
            "Die 48.",
            "Nein, das wars.",
            "Auf den Namen Mueller.",
            "0721 5551234",
            "Ja.",
        ),
        tenant,
        now=NOW,
    )

    text = said(turns)
    assert "Nummer 12 Wan-Tan-Suppe oder Nummer 13 Pho Bo" in text
    assert "Nummer 47 Ente knusprig oder Nummer 48 Ente süß-sauer" in text
    [order] = orders(session)
    assert order.status == "confirmed"
    assert [p[0] for p in positions(session, order)] == ["13", "48"]


def test_eindeutiges_nach_einer_rueckfrage_faellt_nicht_weg(session, tenant):
    """Codex PR #130, P1: "eine Suppe und die 23" - die Suppe braucht eine
    Rueckfrage, die 23 ist eindeutig. Sie kommt trotzdem in den Warenkorb."""
    _, turns = replay(
        session,
        case(
            "Ich moechte etwas zum Abholen bestellen.",
            "Eine Suppe und die 23.",
            "Die 13.",
            "Nein, das wars.",
            "Auf den Namen Mueller.",
            "0721 5551234",
            "Ja.",
        ),
        tenant,
        now=NOW,
    )

    assert "Nummer 12 Wan-Tan-Suppe oder Nummer 13 Pho Bo" in said(turns)
    [order] = orders(session)
    assert order.status == "confirmed"
    assert sorted(p[0] for p in positions(session, order)) == ["13", "23"]


def test_gericht_im_ersten_satz_geht_nicht_verloren(session, tenant):
    """Codex PR #130, P2: "Ich moechte die 23 zum Abholen" nennt schon das
    Gericht. Nach der Statusabfrage wird es gesucht, statt erneut zu fragen,
    was der Gast bestellen moechte. Der KI-Hinweis kommt trotzdem zuerst."""
    _, turns = replay(
        session,
        case(
            "Ich moechte die 23 zum Abholen.",
            "Nein, das wars.",
            "Auf den Namen Mueller.",
            "0721 5551234",
            "Ja.",
        ),
        tenant,
        now=NOW,
    )

    first = " ".join(turns[0].say)
    assert first.startswith("Guten Tag, hier ist der KI-Assistent")
    assert "Nummer 23" in first
    assert "Was möchten Sie bestellen?" not in first
    [order] = orders(session)
    assert order.status == "confirmed"
    assert [p[0] for p in positions(session, order)] == ["23"]


def test_unbekannte_nummer_im_ersten_satz_wird_gesagt(session, tenant):
    _, turns = replay(
        session,
        case("Ich moechte die 99 zum Abholen."),
        tenant,
        now=NOW,
    )
    first = " ".join(turns[0].say)
    assert first.startswith("Guten Tag, hier ist der KI-Assistent")
    assert "Die Nummer 99 habe ich nicht auf der Karte" in first
