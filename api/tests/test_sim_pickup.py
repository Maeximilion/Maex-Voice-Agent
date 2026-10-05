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

from api.domain.menu.importer import MENU_FILE, OPTIONS_FILE, apply, parse
from api.models import Callback, MenuItem, Order, OrderItem
from api.tests.test_domain_menu_search import KARTE
from scripts.seed import seed
from sim.replay import replay
from sim.scripted_order import (
    MenuNumbers,
    PickupScript,
    _orders_something,
    _quantity,
)
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


def test_jeder_unklare_teil_wird_nachgefragt(session, tenant):
    """Codex PR #130, P1: zwei Teile ohne Treffer in einem Satz. Nach dem ersten
    kommt der zweite dran, statt still zu verschwinden."""
    _, turns = replay(
        session,
        case(
            "Ich moechte etwas zum Abholen bestellen.",
            "Die 98 und die 99.",
            "Die 23.",
            "Die 24.",
            "Nein, das wars.",
            "Auf den Namen Mueller.",
            "0721 5551234",
            "Ja.",
        ),
        tenant,
        now=NOW,
    )

    text = said(turns)
    assert "Die Nummer 98 habe ich nicht auf der Karte" in text
    assert "Die Nummer 99 habe ich nicht auf der Karte" in text
    [order] = orders(session)
    assert order.status == "confirmed"
    assert [p[0] for p in positions(session, order)] == ["23", "24"]


def test_antwort_auf_rueckfrage_als_neue_suche_beendet_die_rueckfrage(session, tenant):
    """Codex PR #130, P1: "die dreizehn" erkennt die Auswahl nicht direkt; die
    neue Suche findet die 13. Danach ist die Rueckfrage erledigt, "das wars"
    ist kein Gericht mehr."""
    replay(
        session,
        case(
            "Ich moechte etwas zum Abholen bestellen.",
            "Eine Suppe.",
            "Die dreizehn.",
            "Nein, das wars.",
            "Auf den Namen Mueller.",
            "0721 5551234",
            "Ja.",
        ),
        tenant,
        now=NOW,
    )

    [order] = orders(session)
    assert order.status == "confirmed"
    assert [p[0] for p in positions(session, order)] == ["13"]


# --- Befunde Codex PR #130 nach der abgearbeiteten Runde ---------------------------


@pytest.mark.parametrize(
    ("gesagt", "menge"),
    [
        # Der Satz ist die Kartennummer selbst: keine Menge (Codex PR #130, P1).
        ("die 7", 1),
        ("die sieben", 1),
        ("Nummer 07", 1),
        ("die 23 a", 1),
        ("die dreiundzwanzig a", 1),
        # Menge mit Marker oder vor der Nummer.
        ("zweimal die 7", 2),
        ("zwei Nummer 23", 2),
        # Zahl neben einem Namen ist eine Menge (Regel wie sole_item_number),
        # auch wenn sie zufaellig einer Kartennummer gleicht (Review PR #133).
        ("zwei Frühlingsrollen", 2),
        ("sieben Frühlingsrollen", 7),
        ("Frühlingsrollen", 1),
    ],
)
def test_menge_nach_der_regel_der_domain(gesagt, menge):
    assert _quantity(gesagt) == menge


def test_menge_bleibt_wenn_die_rueckfrage_neu_gesucht_wird(session, tenant):
    """Codex PR #130, P1: "zwei Suppen", dann "die dreizehn" - die neue Suche
    findet die 13, die Menge aus der ersten Nennung bleibt."""
    replay(
        session,
        case(
            "Ich moechte etwas zum Abholen bestellen.",
            "Zwei Suppen.",
            "Die dreizehn.",
            "Nein, das wars.",
            "Auf den Namen Mueller.",
            "0721 5551234",
            "Ja.",
        ),
        tenant,
        now=NOW,
    )
    [order] = orders(session)
    assert positions(session, order) == [("13", 2, [])]


def test_abholung_erst_im_zweiten_satz(session, tenant):
    """Codex PR #130, P2: erst ein Gruss, dann "Ich moechte die 23 zum Abholen".
    "zum Abholen" darf die 23 nicht verdecken."""
    _, turns = replay(
        session,
        case(
            "Guten Tag.",
            "Ich moechte die 23 zum Abholen.",
            "Nein, das wars.",
            "Auf den Namen Mueller.",
            "0721 5551234",
            "Ja.",
        ),
        tenant,
        now=NOW,
    )
    assert "Nummer 23" in " ".join(turns[1].say)
    [order] = orders(session)
    assert [p[0] for p in positions(session, order)] == ["23"]


def test_jedes_ausverkaufte_gericht_wird_gesagt(session, tenant):
    """Codex PR #130, P2: zwei ausverkaufte Gerichte in einem Satz - beide werden
    genannt, keins faellt still weg."""
    for nummer in ("23", "24"):
        session.execute(
            update(MenuItem)
            .where(MenuItem.tenant_id == tenant.id, MenuItem.number == nummer)
            .values(sold_out_until=datetime(2026, 9, 15, 23, 0, tzinfo=BERLIN))
        )
    session.commit()
    _, turns = replay(
        session,
        case(
            "Ich moechte etwas zum Abholen bestellen.",
            "Die 23 und die 24.",
            "Die 13.",
        ),
        tenant,
        now=NOW,
    )
    text = said(turns)
    assert "Frühlingsrollen (4 Stück) ist heute leider aus" in text
    assert "Sommerrollen mit Garnelen ist heute leider aus" in text


# --- Review PR #133 ----------------------------------------------------------------


def _suppe_und(session, tenant, *antworten):
    return replay(
        session,
        case(
            "Ich moechte etwas zum Abholen bestellen.",
            "Zwei Suppen.",
            *antworten,
            "Nein, das wars.",
            "Auf den Namen Mueller.",
            "0721 5551234",
            "Ja.",
        ),
        tenant,
        now=NOW,
    )


def test_gesprochene_nummer_waehlt_den_vorschlag_ohne_neue_suche(session, tenant):
    """ "die dreizehn" ist einer der Vorschlaege - gewaehlt wird direkt, ohne
    zweite Suche, mit der Menge aus "Zwei Suppen"."""
    _, turns = _suppe_und(session, tenant, "Die dreizehn.")
    assert "search_menu" not in str(turns[2].tools)
    [order] = orders(session)
    assert positions(session, order) == [("13", 2, [])]


def test_menge_in_der_antwort_gilt(session, tenant):
    """ "Einmal die dreizehn" korrigiert die Menge: eins, nicht zwei."""
    _suppe_und(session, tenant, "Einmal die dreizehn.")
    [order] = orders(session)
    assert positions(session, order) == [("13", 1, [])]


def test_anderes_gericht_als_antwort_erbt_keine_menge(session, tenant):
    """ "Dann die 23" ist keiner der Vorschlaege: neues Gericht, eigene Menge.
    Die zwei aus "Zwei Suppen" darauf zu legen waere geraten."""
    _suppe_und(session, tenant, "Dann die 23.")
    [order] = orders(session)
    assert positions(session, order) == [("23", 1, [])]


def test_menge_bleibt_nicht_haengen_nach_fehlgeschlagener_suche(session, tenant):
    _suppe_und(session, tenant, "Die neunundneunzig.", "Dann die 23.")
    [order] = orders(session)
    assert positions(session, order) == [("23", 1, [])]


def test_ausverkauft_wird_ohne_zweite_suche_gesagt(session, tenant):
    for nummer in ("23", "24"):
        session.execute(
            update(MenuItem)
            .where(MenuItem.tenant_id == tenant.id, MenuItem.number == nummer)
            .values(sold_out_until=datetime(2026, 9, 15, 23, 0, tzinfo=BERLIN))
        )
    session.commit()
    _, turns = replay(
        session,
        case("Ich moechte etwas zum Abholen bestellen.", "Die 23 und die 24."),
        tenant,
        now=NOW,
    )
    assert str(turns[1].tools).count("search_menu") == 1
    text = " ".join(turns[1].say)
    assert "Frühlingsrollen (4 Stück) ist heute leider aus" in text
    assert "Sommerrollen mit Garnelen ist heute leider aus" in text


def test_abholung_spaeter_ohne_gericht_behaelt_den_namen(session, tenant):
    """Abholung erst nach dem Gruss genannt, ohne Gericht, aber mit Namen: der
    Name geht nicht verloren, und es gibt kein "nicht gefunden"."""
    _, turns = replay(
        session,
        case(
            "Guten Tag.",
            "Ich moechte etwas zum Abholen bestellen, auf den Namen Mueller.",
            "Die 23.",
            "Nein, das wars.",
            "0721 5551234",
            "Ja.",
        ),
        tenant,
        now=NOW,
    )
    text = said(turns)
    assert "nicht gefunden" not in text
    assert "Auf welchen Namen" not in text
    [order] = orders(session)
    assert order.customer_name == "Mueller"


def test_abholung_spaeter_mit_fuellwort_sagt_nicht_nicht_gefunden(session, tenant):
    _, turns = replay(
        session,
        case("Guten Tag.", "Ich moechte gern was zum Abholen."),
        tenant,
        now=NOW,
    )
    second = " ".join(turns[1].say)
    assert "nicht gefunden" not in second
    assert "Was möchten Sie bestellen?" in second


@pytest.mark.parametrize(
    ("antwort", "nummer"),
    [
        ("zwei Pho Bo", "13"),
        ("die dreizehn", "13"),
        ("einmal die dreizehn", "13"),
        ("die 2", "2"),
        ("Nummer zwei", "2"),
    ],
)
def test_menge_neben_dem_namen_waehlt_nicht_die_nummer(antwort, nummer):
    """Codex PR #133, P2: "zwei Pho Bo" nennt Menge und Namen - nicht Karte 2.
    Wie in der Suche (sole_item_number) ist eine Zahl neben einem Namen eine
    Menge; nur ein Satz, der die Nummer selbst ist, waehlt per Nummer."""
    script = PickupScript()
    script._suggestions = [
        {"number": "2", "name": "Frühlingsrolle"},
        {"number": "13", "name": "Pho Bo"},
    ]
    assert script._pick_suggestion(antwort)["number"] == nummer


# --- Zweites Review PR #133 und Codex auf 79da492 ----------------------------------

FRUEHLING = {"number": "23", "name": "Frühlingsrollen (4 Stück)"}
ACHT = {"number": "31", "name": "Acht Schätze"}


@pytest.mark.parametrize(
    ("gesagt", "treffer", "menge"),
    [
        # Nummer mit Artikel und dann der Name: das Gericht, keine Menge.
        ("die 23 Frühlingsrollen", FRUEHLING, 1),
        ("die 23, Frühlingsrollen", FRUEHLING, 1),
        ("zweimal die 23 Frühlingsrollen", FRUEHLING, 2),
        ("zwei Frühlingsrollen", FRUEHLING, 2),
        # Der Name beginnt selbst mit dem Zahlwort.
        ("Acht Schätze", ACHT, 1),
        ("zweimal Acht Schätze", ACHT, 2),
        ("zwei Acht Schätze", ACHT, 2),
    ],
)
def test_menge_mit_dem_gefundenen_gericht(gesagt, treffer, menge):
    assert _quantity(gesagt, treffer) == menge


def test_menge_in_der_antwort_ohne_marker_gilt(session, tenant):
    """Codex PR #133, P2: "Drei Suppen", dann "zwei Nummer dreizehn" - zwei."""
    replay(
        session,
        case(
            "Ich moechte etwas zum Abholen bestellen.",
            "Drei Suppen.",
            "Zwei Nummer dreizehn.",
            "Nein, das wars.",
            "Auf den Namen Mueller.",
            "0721 5551234",
            "Ja.",
        ),
        tenant,
        now=NOW,
    )
    [order] = orders(session)
    assert positions(session, order) == [("13", 2, [])]


def test_name_mit_menge_als_antwort(session, tenant):
    replay(
        session,
        case(
            "Ich moechte etwas zum Abholen bestellen.",
            "Eine Suppe.",
            "Zwei Pho Bo.",
            "Nein, das wars.",
            "Auf den Namen Mueller.",
            "0721 5551234",
            "Ja.",
        ),
        tenant,
        now=NOW,
    )
    [order] = orders(session)
    assert positions(session, order) == [("13", 2, [])]


def test_unklare_antwort_laesst_die_rueckfrage_offen(session, tenant):
    """Findet die Antwort nichts ("Wie bitte?"), bleibt die Frage nach der Suppe
    offen, samt der Menge - statt dass die Suppe still verschwindet."""
    _, turns = replay(
        session,
        case(
            "Ich moechte etwas zum Abholen bestellen.",
            "Zwei Suppen.",
            "Wie bitte?",
            "Die dreizehn.",
            "Nein, das wars.",
            "Auf den Namen Mueller.",
            "0721 5551234",
            "Ja.",
        ),
        tenant,
        now=NOW,
    )
    assert "Nummer 12 Wan-Tan-Suppe oder Nummer 13 Pho Bo" in " ".join(turns[2].say)
    [order] = orders(session)
    assert positions(session, order) == [("13", 2, [])]


@pytest.mark.parametrize(
    "zeilen",
    [
        ("Guten Tag.", "Schnitzel zum Abholen."),
        ("Ich moechte Schnitzel zum Abholen.",),
    ],
)
def test_unbekanntes_gericht_zur_abholung_wird_gesagt(session, tenant, zeilen):
    """Codex PR #133, P2: nennt der Satz ein Gericht, das es nicht gibt, hoert
    der Gast das - nicht nur "Was moechten Sie bestellen?"."""
    _, turns = replay(session, case(*zeilen), tenant, now=NOW)
    assert "nicht gefunden" in " ".join(turns[-1].say)


def test_auf_im_gerichtnamen_bleibt_im_gericht():
    """Codex PR #133, P2: "Ente auf Reis" ist ein Gericht, kein Name "Reis"."""
    from sim.scripted_llm import _opening_dish

    assert _opening_dish("Ente auf Reis zum Abholen.") == "Ente auf Reis"
    assert _opening_dish("die 23 zum Abholen, auf den Namen Mueller") == "die 23"


def test_zahlwort_im_namen_auch_ohne_umlaut():
    """Codex PR #133, P2: "fuenf schaetze" trifft "Fünf Schätze" - die Fünf gehoert
    zum Namen, keine Menge. Verglichen wird in der gefalteten Form."""
    assert _quantity("fuenf schaetze", {"number": "33", "name": "Fünf Schätze"}) == 1


def test_ausverkaufter_vorschlag_wird_nicht_aufgenommen(session, tenant):
    """Codex PR #133: die Wahl aus den Vorschlaegen ("die dreizehn") nimmt ein
    ausverkauftes Gericht nicht auf, sondern sagt es und bietet den Rest an.
    Sonst lehnte draft_order den Warenkorb ab, und die Bestellung kaeme nicht
    mehr zum Abschluss."""
    session.execute(
        update(MenuItem)
        .where(MenuItem.tenant_id == tenant.id, MenuItem.number == "13")
        .values(sold_out_until=datetime(2026, 9, 15, 23, 0, tzinfo=BERLIN))
    )
    session.commit()
    _, turns = replay(
        session,
        case(
            "Ich moechte etwas zum Abholen bestellen.",
            "Eine Suppe.",
            "Die dreizehn.",
            "Die zwoelf.",
            "Nein, das wars.",
            "Auf den Namen Mueller.",
            "0721 5551234",
            "Ja.",
        ),
        tenant,
        now=NOW,
    )
    third = " ".join(turns[2].say)
    assert "Pho Bo ist heute leider aus" in third
    assert "Nummer 12 Wan-Tan-Suppe" in third
    [order] = orders(session)
    assert order.status == "confirmed"
    assert positions(session, order) == [("12", 1, [])]


def test_keiner_der_vorschlaege_fuehrt_weiter(session, tenant):
    """Codex PR #133: "keine davon" verwirft die Vorschlaege, statt als Gericht
    gesucht zu werden und dieselbe Frage endlos zurueckzubringen."""
    _, turns = replay(
        session,
        case(
            "Ich moechte etwas zum Abholen bestellen.",
            "Eine Suppe.",
            "Keine davon.",
            "Die 23.",
            "Nein, das wars.",
            "Auf den Namen Mueller.",
            "0721 5551234",
            "Ja.",
        ),
        tenant,
        now=NOW,
    )
    assert "Meinen Sie" not in " ".join(turns[2].say)
    [order] = orders(session)
    assert positions(session, order) == [("23", 1, [])]


def test_das_wars_beendet_auch_waehrend_einer_rueckfrage(session, tenant):
    """ "Nein, das wars" auf eine Rueckfrage: die Frage faellt weg, die
    Bestellung geht mit dem Rest weiter zum Namen."""
    _, turns = replay(
        session,
        case(
            "Ich moechte etwas zum Abholen bestellen.",
            "Die 23.",
            "Eine Suppe.",
            "Nein, das wars.",
            "Auf den Namen Mueller.",
            "0721 5551234",
            "Ja.",
        ),
        tenant,
        now=NOW,
    )
    assert "Auf welchen Namen" in " ".join(turns[3].say)
    [order] = orders(session)
    assert order.status == "confirmed"
    assert positions(session, order) == [("23", 1, [])]


# --- Wuensche (T-4.10) -------------------------------------------------------------


def _notes(session, order) -> list[str | None]:
    return list(
        session.scalars(
            select(OrderItem.note)
            .where(OrderItem.order_id == order.id)
            .order_by(OrderItem.created_at)
        )
    )


def _bestellung(session, tenant, *zeilen):
    return replay(
        session,
        case(
            "Ich moechte etwas zum Abholen bestellen.",
            *zeilen,
            "Nein, das wars.",
            "Auf den Namen Mueller.",
            "0721 5551234",
            "Ja.",
        ),
        tenant,
        now=NOW,
    )


def test_hinweis_landet_in_der_bestellung(session, tenant):
    _, turns = _bestellung(session, tenant, "Die 23 ohne Karotten.")
    assert "Nummer 23, ohne Karotten" in " ".join(turns[1].say)
    [order] = orders(session)
    assert order.status == "confirmed"
    assert _notes(session, order) == ["ohne Karotten"]


def test_option_als_wunsch_erspart_die_rueckfrage(session, tenant):
    """ "mit Huhn" ist die Option der Pflichtgruppe Fleisch: keine Frage mehr
    nach Ente oder Huhn, die Wahl steht in der Bestellung."""
    _, turns = _bestellung(session, tenant, "Die knusprige Ente mit Huhn.")
    assert "Welche Auswahl bei Fleisch" not in said(turns)
    [order] = orders(session)
    assert positions(session, order) == [("47", 1, ["Huhn"])]


def test_wunsch_nach_der_wahl_aus_vorschlaegen(session, tenant):
    """ "Ente ohne Zwiebeln" ist mehrdeutig; nach der Wahl gilt der Hinweis."""
    _bestellung(session, tenant, "Ente ohne Zwiebeln.", "Die 48.")
    [order] = orders(session)
    assert positions(session, order)[0][0] == "48"
    assert _notes(session, order) == ["ohne Zwiebeln"]


def test_fall_mit_wuenschen_aus_evals(session, tenant):
    fall = json.loads(
        (CASE.parent / "abholung_0003_wuensche.json").read_text(encoding="utf-8")
    )
    _, turns = replay(session, fall, tenant, now=NOW)

    assert "Welche Auswahl bei Fleisch" not in said(turns)
    [order] = orders(session)
    assert order.status == "confirmed"
    assert positions(session, order) == [("23", 1, []), ("47", 1, ["Huhn"])]
    assert _notes(session, order) == ["ohne Karotten", None]


def test_allergie_steht_im_festen_wortlaut_in_der_bestellung(session, tenant):
    """E14: Hinweis an die Kueche im festen Wortlaut, beim Vorlesen wiederholt."""
    _, turns = _bestellung(session, tenant, "Pho Bo, ich vertrage keine Erdnüsse.")
    [order] = orders(session)
    assert order.status == "confirmed"
    assert _notes(session, order) == ["WICHTIG: Keine Erdnüsse. Grund: Allergie"]
    assert "WICHTIG: Keine Erdnüsse. Grund: Allergie" in said(turns)


def test_wunsch_nach_der_wahl_wird_gesagt(
    session,
    tenant,
):
    """Codex PR #139, P2: nach der Wahl aus Vorschlaegen wird der Wunsch
    eingeordnet und gesagt - Unbekanntes abgelehnt, eine Option wiederholt."""
    _, turns = _bestellung(session, tenant, "Ente mit Pommes.", "Die 48.")
    assert "Den Wunsch „mit Pommes“ kann ich leider nicht anbieten" in said(turns)


def test_option_nach_der_wahl_wird_wiederholt(session, tenant):
    _, turns = _bestellung(session, tenant, "Ente mit Huhn.", "Die 47.")
    assert "Nummer 47 Ente knusprig mit Huhn" in " ".join(turns[2].say)
    [order] = orders(session)
    assert positions(session, order) == [("47", 1, ["Huhn"])]


def test_unbekannter_wunsch_in_einer_aufzaehlung_wird_gesagt(session, tenant):
    _, turns = _bestellung(session, tenant, "Die 23 mit Pommes und Pho Bo.")
    assert "Den Wunsch „mit Pommes“ kann ich leider nicht anbieten" in said(turns)


def test_menge_am_ende_mit_wunsch(session, tenant):
    """(4) "Die 23 ohne Zwiebeln, zweimal." - zwei Portionen, Hinweis ohne "zweimal"."""
    _bestellung(session, tenant, "Die 23 ohne Zwiebeln, zweimal.")
    [order] = orders(session)
    assert positions(session, order) == [("23", 2, [])]
    assert _notes(session, order) == ["ohne Zwiebeln"]


def test_weglassen_und_option_zusammen_in_der_bestellung(session, tenant):
    """(1) Hinweis und Option aus einem Wunsch landen beide in der Bestellung."""
    _bestellung(session, tenant, "Die knusprige Ente ohne Zwiebeln, dafür mit Huhn.")
    [order] = orders(session)
    assert positions(session, order) == [("47", 1, ["Huhn"])]
    assert _notes(session, order) == ["ohne Zwiebeln"]


def test_eigene_allergie_ist_kein_rueckruf(session, tenant):
    """Codex PR #139: "ich habe eine Erdnussallergie" waehrend der Bestellung ist
    ein Hinweis zur Position (E14), kein Anliegen fuer einen Rueckruf."""
    _bestellung(session, tenant, "Pho Bo, ich habe eine Erdnussallergie.")
    session.expire_all()
    assert list(session.scalars(select(Callback))) == []
    [order] = orders(session)
    assert _notes(session, order) == ["WICHTIG: Keine Erdnuss. Grund: Allergie"]


def test_allergie_ohne_zutat_wird_nachgefragt_und_notiert(session, tenant):
    """Codex PR #139, P1: "ich habe eine Allergie" - die Frage "Wogegen?" bleibt
    offen, bis die Antwort kommt; die Zutat geht im festen Wortlaut an die
    Kueche, statt als Gericht gesucht zu werden."""
    _, turns = _bestellung(
        session, tenant, "Pho Bo, ich habe eine Allergie.", "Erdnüsse."
    )
    assert "Wogegen" in " ".join(turns[1].say)
    assert "Darf es noch etwas sein?" not in " ".join(turns[1].say)
    [order] = orders(session)
    assert _notes(session, order) == ["WICHTIG: Keine Erdnüsse. Grund: Allergie"]


@pytest.mark.parametrize("antwort", ["Weiß ich nicht.", "Nein."])
def test_allergie_ohne_antwort_bleibt_offen(session, tenant, antwort):
    """Codex PR #139, P1: auf "Wogegen?" keine Zutat - die Frage bleibt offen,
    kein Hinweis "Keine Weiß ich nicht" an die Kueche."""
    _, turns = _bestellung(
        session, tenant, "Pho Bo, ich habe eine Allergie.", antwort, "Erdnüsse."
    )
    assert "Wogegen" in " ".join(turns[2].say)
    [order] = orders(session)
    assert _notes(session, order) == ["WICHTIG: Keine Erdnüsse. Grund: Allergie"]


def test_zwei_allergien_ohne_zutat_werden_nacheinander_gefragt(session, tenant):
    """Codex PR #139, P1: zwei Gerichte mit Allergie ohne Zutat in einem Satz -
    jede Frage bleibt offen, jede Antwort gehoert zu ihrem Gericht."""
    _, turns = _bestellung(
        session,
        tenant,
        "Pho Bo mit Allergie und Frühlingsrollen mit Allergie.",
        "Erdnüsse.",
        "Sesam.",
    )
    erste, zweite = " ".join(turns[1].say), " ".join(turns[2].say)
    assert erste.count("Wogegen") == 1
    assert "bei Pho Bo" in erste
    assert "bei Frühlingsrollen (4 Stück)" in zweite
    [order] = orders(session)
    assert _notes(session, order) == [
        "WICHTIG: Keine Erdnüsse. Grund: Allergie",
        "WICHTIG: Keine Sesam. Grund: Allergie",
    ]


def test_allergie_und_auswahl_nacheinander(session, tenant):
    """Codex PR #139, P1: ein mehrdeutiges Gericht und eine Allergie ohne Zutat
    in einem Satz - erst die Allergie, dann die Auswahl, nie beide Fragen
    zugleich. Sonst wuerde "Nummer 12" als Zutat notiert."""
    _, turns = _bestellung(
        session,
        tenant,
        "Eine Suppe und eine Pho Bo mit Allergie.",
        "Erdnüsse.",
        "Nummer 12.",
    )
    erste, zweite = " ".join(turns[1].say), " ".join(turns[2].say)
    assert "Wogegen" in erste
    assert "Meinen Sie" not in erste
    assert "Meinen Sie" in zweite
    [order] = orders(session)
    assert sorted(p[0] for p in positions(session, order)) == ["12", "13"]
    assert "WICHTIG: Keine Erdnüsse. Grund: Allergie" in _notes(session, order)


@pytest.mark.parametrize(
    "antwort",
    [
        "Gegen Erdnüsse.",
        "Ich bin gegen Erdnüsse allergisch.",
        "Ich habe eine Erdnüsseallergie.",
        "Erdnüsse.",
    ],
)
def test_ganze_antwort_auf_wogegen(session, tenant, antwort):
    """Codex PR #139, P1: eine ganze Antwort ("gegen Erdnüsse", "ich bin gegen
    Erdnüsse allergisch") gibt dieselbe Zutat wie das blosse Wort."""
    _bestellung(session, tenant, "Pho Bo, ich habe eine Allergie.", antwort)
    [order] = orders(session)
    assert _notes(session, order) == ["WICHTIG: Keine Erdnüsse. Grund: Allergie"]


@pytest.mark.parametrize("antwort", ["Keine Erdnüsse.", "Kein Erdnüsse bitte."])
def test_keine_als_antwort_auf_wogegen(session, tenant, antwort):
    """Codex PR #139, P1: "Keine Erdnüsse" als Antwort ergibt keinen Hinweis
    "Keine Keine Erdnüsse"."""
    _bestellung(session, tenant, "Pho Bo, ich habe eine Allergie.", antwort)
    [order] = orders(session)
    assert _notes(session, order) == ["WICHTIG: Keine Erdnüsse. Grund: Allergie"]


def test_allergiefrei_geht_an_das_team(session, tenant):
    """Codex PR #139, P2: "Frühlingsrollen allergiefrei?" waehrend der Bestellung
    ist die Frage nach Allergenen (Rueckruf), nicht "Wogegen?"."""
    _, turns = replay(
        session,
        case(
            "Ich moechte etwas zum Abholen bestellen.",
            "Sind die Frühlingsrollen allergiefrei?",
        ),
        tenant,
        now=NOW,
    )
    assert "Wogegen" not in said(turns)
    # Der Rueckrufpfad fragt nach der Nummer (Rufnummer unterdrueckt).
    assert "Telefonnummer" in said(turns)


def test_mehrdeutige_position_mit_wunsch_fragt_einmal(session, tenant):
    """Codex PR #139, P2: "eine Ente mit Nudeln und eine Pho Bo" - die Auswahl
    zur Ente steht im Teil und wird einmal gefragt, nicht doppelt."""
    _, turns = _bestellung(session, tenant, "Eine Ente mit Nudeln und eine Pho Bo.")
    assert " ".join(turns[1].say).count("Meinen Sie") == 1


def test_allergie_bleibt_wenn_die_wahl_neu_gesucht_wird(session, tenant):
    """Review PR #139: "Pho" statt der angebotenen "Pho Bo" wird neu gesucht - die
    Allergie aus der Frage bleibt an diesem Gericht."""
    _bestellung(session, tenant, "Eine Suppe mit Erdnussallergie.", "Pho.")
    [order] = orders(session)
    assert _notes(session, order) == ["WICHTIG: Keine Erdnuss. Grund: Allergie"]


def test_mehrdeutig_mit_allergie_erst_die_wahl(session, tenant):
    """Review PR #139: mehrdeutiges Gericht mit Allergie ohne Zutat - erst die Wahl,
    dann "Wogegen?", nie beides zugleich."""
    _, turns = _bestellung(
        session,
        tenant,
        "Eine Suppe mit Allergie und die 23.",
        "Nummer 12.",
        "Erdnüsse.",
    )
    erste = " ".join(turns[1].say)
    assert "Wogegen" not in erste
    assert erste.count("Meinen Sie") == 1
    assert "Wogegen" in " ".join(turns[2].say)
    [order] = orders(session)
    assert "WICHTIG: Keine Erdnüsse. Grund: Allergie" in _notes(session, order)


def test_mehrdeutig_mit_allergie_und_zutat_fragt_einmal(session, tenant):
    _, turns = _bestellung(
        session, tenant, "Eine Suppe mit Erdnussallergie und die 23.", "Nummer 12."
    )
    assert " ".join(turns[1].say).count("Meinen Sie") == 1


@pytest.mark.parametrize("antwort", ["Eine Cola bitte.", "Nummer 12."])
def test_bestellung_ist_keine_antwort_auf_wogegen(session, tenant, antwort):
    """Review PR #139: ein Gericht statt der Zutat wird nicht zur Allergie."""
    _, turns = _bestellung(
        session, tenant, "Pho Bo, ich habe eine Allergie.", antwort, "Erdnüsse."
    )
    assert "Wogegen" in " ".join(turns[2].say)
    [order] = orders(session)
    assert _notes(session, order) == ["WICHTIG: Keine Erdnüsse. Grund: Allergie"]


# --- Befunde aus der Eval-Suite v1 (T-5.2) -----------------------------------------


@pytest.mark.parametrize(
    ("gericht", "rueckfrage", "nummer"),
    [
        ("Einmal Fruehlingsrollen.", "Ja, genau.", "23"),
        ("Einmal Ente.", "Die süß-saure.", "48"),
    ],
)
def test_ja_auf_die_einzige_rueckfrage_nimmt_das_gericht(
    session, tenant, gericht, rueckfrage, nummer
):
    """T-5.2: "Meinen Sie Nummer 23?" - ein Ja darauf nimmt genau diesen einen
    Vorschlag. Vorher suchte das Skript "Ja, genau" als Gericht und fragte
    endlos dasselbe."""
    replay(
        session,
        case(
            "Guten Tag, ich moechte etwas zum Abholen bestellen.",
            gericht,
            rueckfrage,
            "Ja.",
            "Nein, das wars.",
            "Auf den Namen Mueller.",
            "0721 5551234",
            "Ja, passt so.",
        ),
        tenant,
        now=NOW,
    )
    [order] = orders(session)
    assert order.status == "confirmed"
    assert [p[0] for p in positions(session, order)] == [nummer]


def test_nein_auf_die_einzige_rueckfrage_nimmt_nichts(session, tenant):
    _, turns = replay(
        session,
        case(
            "Guten Tag, ich moechte etwas zum Abholen bestellen.",
            "Einmal Fruehlingsrollen.",
            "Nein, nicht die.",
        ),
        tenant,
        now=NOW,
    )
    assert orders(session) == []
    assert "Meinen Sie Nummer 23" not in " ".join(turns[-1].say)


@pytest.mark.parametrize(
    "satz",
    [
        "Hallo, ich moechte die 13 zum Abholen bestellen.",
        "Guten Abend, die 13 zum Mitnehmen, bitte.",
    ],
)
def test_nummer_im_ersten_satz_ist_keine_rufnummer(satz):
    """T-5.2: Nach dem Streichen von "zum Abholen bestellen" blieben Leerzeichen
    hinter der 13 stehen, und die Rufnummern-Regel nahm "13    " als Nummer weg.
    Eine Rufnummer hat mindestens sieben Ziffern, wie in `_phone`."""
    from sim.scripted_llm import _opening_dish

    assert "13" in (_opening_dish(satz) or "")


@pytest.mark.parametrize(
    ("antwort", "ja"),
    [
        ("Ja, genau.", True),
        ("Richtig.", True),
        ("Das stimmt nicht.", False),
        ("Nicht richtig.", False),
        ("Ja, aber lieber was anderes.", False),
        ("Ich haette gern die Suppe.", False),
        ("Nein.", False),
    ],
)
def test_ja_zum_einzigen_vorschlag(antwort, ja):
    from sim.scripted_order import _agrees

    assert _agrees(antwort) is ja


def test_andere_nummer_nach_der_rueckfrage_gilt(session, tenant):
    """Review PR #145: "Ich haette gern die 24" auf "Meinen Sie Nummer 23?" ist
    eine neue Wahl, kein Ja zur 23."""
    replay(
        session,
        case(
            "Guten Tag, ich moechte etwas zum Abholen bestellen.",
            "Einmal Fruehlingsrollen.",
            "Ich haette gern die 24.",
            "Nein, das wars.",
            "Auf den Namen Mueller.",
            "0721 5551234",
            "Ja, passt so.",
        ),
        tenant,
        now=NOW,
    )
    [order] = orders(session)
    assert [p[0] for p in positions(session, order)] == ["24"]


def _add_dishes(session, tenant, rows: str) -> None:
    plan = parse({**KARTE, MENU_FILE: KARTE[MENU_FILE] + rows})
    assert plan.ok, plan.errors
    apply(session, tenant.id, plan, now=NOW)


@pytest.fixture
def sushi(session, tenant):
    """A menu like the register (T-4.12): S and SM in the group Sushi, 25 and
    25g, next to the numbers without a prefix."""
    _add_dishes(
        session,
        tenant,
        "S1;Lachs Nigiri;Sushi;4,50;;ja\n"
        "S12;Thunfisch Maki;Sushi;5,90;;ja\n"
        "SM1;Sushi-Menü klein;Sushi;16,90;;ja\n"
        "25;Chop Suey;Hauptgerichte;12,50;;ja\n"
        "25G;Soße Chop Suey;Hauptgerichte;2,00;;ja\n",
    )
    return tenant


@pytest.mark.parametrize(
    "answer",
    ["S12.", "SM1", "die 25g", "S0001", "S zwölf.", "SM eins.", "Sushi zwölf."],
)
def test_prefixed_number_is_not_an_ingredient(session, sushi, answer):
    """Codex PR #155: as an answer to "Wogegen?", a card number with letters
    is an order, not an ingredient - no "Keine S12" for the kitchen."""
    _, turns = _bestellung(
        session, sushi, "Pho Bo, ich habe eine Allergie.", answer, "Erdnüsse."
    )
    assert "Wogegen" in " ".join(turns[2].say)
    [order] = orders(session)
    assert _notes(session, order) == ["WICHTIG: Keine Erdnüsse. Grund: Allergie"]


@pytest.mark.parametrize("answer", ["Fünf-Gewürze-Pulver.", "Zwei Sachen: Milch."])
def test_ingredient_with_number_word_is_not_an_order(session, tenant, answer):
    """Codex PR #155: a number word inside an ingredient does not turn the
    answer into an order - the note is recorded and the question not repeated."""
    _, turns = _bestellung(session, tenant, "Pho Bo, ich habe eine Allergie.", answer)
    assert "Wogegen" not in " ".join(turns[2].say)
    [order] = orders(session)
    assert len(_notes(session, order)) == 1


def test_additive_is_an_ingredient(session, tenant):
    """Code review PR #155: "E621" as an answer to "Wogegen?" is an additive,
    not a card number - the allergy is recorded."""
    _bestellung(session, tenant, "Pho Bo, ich habe eine Allergie.", "Gegen E621.")
    [order] = orders(session)
    assert any("E621" in (note or "") for note in _notes(session, order))


@pytest.mark.parametrize(
    ("answer", "ingredient"),
    [("B12.", "B12"), ("Vitamin B12.", "Vitamin B12"), ("Gegen E 621.", "E 621")],
)
def test_compact_ingredient_is_not_a_card_number(session, tenant, answer, ingredient):
    """Codex PR #155 (P2): the menu decides what a card number is, not the
    import grammar. This menu has no prefix B and no prefix E, so "B12",
    "Vitamin B12" and the spaced additive "E 621" are the ingredient - the
    note is recorded and the question not repeated."""
    _, turns = _bestellung(session, tenant, "Pho Bo, ich habe eine Allergie.", answer)
    assert "Wogegen" not in " ".join(turns[2].say)
    [order] = orders(session)
    assert _notes(session, order) == [f"WICHTIG: Keine {ingredient}. Grund: Allergie"]


def test_the_menu_decides_what_a_card_number_is(session, tenant):
    """The same answer "B12" is an order on a menu whose numbers carry the
    prefix B: the question stays open, no "Keine B12" for the kitchen."""
    _add_dishes(session, tenant, "B12;Gebratener Reis;Beilagen;4,50;;ja\n")
    _, turns = _bestellung(
        session, tenant, "Pho Bo, ich habe eine Allergie.", "B12.", "Erdnüsse."
    )
    assert "Wogegen" in " ".join(turns[2].say)
    [order] = orders(session)
    assert _notes(session, order) == ["WICHTIG: Keine Erdnüsse. Grund: Allergie"]


@pytest.mark.parametrize("answer", ["Die 12 mit Reis.", "Sesam, und die 12."])
def test_number_on_the_menu_in_a_sentence_is_an_order(session, tenant, answer):
    """A number the menu has, next to other words, is still an order: the
    question stays open instead of "Keine die 12 mit Reis" for the kitchen."""
    _, turns = _bestellung(
        session, tenant, "Pho Bo, ich habe eine Allergie.", answer, "Erdnüsse."
    )
    assert "Wogegen" in " ".join(turns[2].say)
    [order] = orders(session)
    assert _notes(session, order) == ["WICHTIG: Keine Erdnüsse. Grund: Allergie"]


SUSHI_MENU = MenuNumbers.from_items(
    [
        ("12", "Suppen"),
        ("13", "Suppen"),
        ("S1", "Sushi"),
        ("S12", "Sushi"),
        ("SM1", "Sushi"),
        ("25G", "Hauptgerichte"),
    ]
)


@pytest.mark.parametrize(
    ("answer", "order"),
    [
        # On the menu, in every spelling.
        ("S12", True),
        ("s012", True),
        ("S zwölf bitte", True),
        ("Sushi zwölf", True),
        ("Nummer 12", True),
        ("Milch und S12", True),
        ("Ich nehme noch die 13", True),
        # A form the menu has, a number it lacks: ask again, note nothing.
        ("S13", True),
        ("S1000", True),
        ("12 oder 13", True),
        ("Nummer 99 mit Reis", True),
        ("Eine Cola bitte", True),
        # Neither a prefix nor a number of this menu.
        ("B12", False),
        ("Vitamin B12", False),
        ("Vitamin B 99", False),
        ("Gegen E621", False),
        ("Gegen E 621", False),
        ("Glutamat 621", False),
        ("Q10", False),
        ("Fünf-Gewürze-Pulver", False),
        ("Zwei Sachen: Milch", False),
        ("Erdnüsse", False),
    ],
)
def test_order_or_ingredient_follows_the_menu(answer, order):
    assert _orders_something(answer, SUSHI_MENU) is order


@pytest.mark.parametrize(
    ("answer", "order"),
    [("S12", False), ("Sushi zwölf", False), ("Nummer 12", True), ("die 25g", True)],
)
def test_without_a_menu_only_plain_numbers_are_orders(answer, order):
    """A menu without prefixes knows no "S12"; a plain number by rule A stays
    an order."""
    assert _orders_something(answer, MenuNumbers()) is order


def test_text_phone_reads_the_numbers_of_the_active_menu(session, sushi):
    """`SimCall` hands the scripted model the tenant's menu: active dishes
    only, compared like search_menu (`canonical_card`)."""
    from sim.session import menu_numbers

    menu = menu_numbers(session, sushi.id)
    assert menu.card.prefixes == {"s", "sm"}
    assert {"s12", "sm1", "25g", "13"} <= menu.numbers
    assert "50" not in menu.numbers  # "Altes Gericht" is inactive


# --- Open points from reviews, text phone (Codex PR #133 and #139) -----------------


@pytest.fixture
def side_dish(session, tenant):
    """A dish whose option "Reis" stands in two groups: the domain answers
    `open` with `groups` and asks which one is meant."""
    plan = parse(
        {
            **KARTE,
            MENU_FILE: KARTE[MENU_FILE] + "55;Gemüsepfanne;Hauptgerichte;9,50;;ja\n",
            OPTIONS_FILE: KARTE[OPTIONS_FILE]
            + "55;Beilage;Reis;0,00;nein;nein\n"
            + "55;Beilage;Nudeln;1,00;nein;nein\n"
            + "55;Extra;Reis;2,00;nein;nein\n",
        }
    )
    assert plan.ok, plan.errors
    apply(session, tenant.id, plan, now=NOW)
    return tenant


def _chosen(session, order) -> list[tuple[str, str]]:
    [options] = session.scalars(
        select(OrderItem.options).where(OrderItem.order_id == order.id)
    )
    return [(o["group"], o["option"]) for o in options]


@pytest.mark.parametrize(
    ("answer", "group"),
    [("Extra.", "Extra"), ("Als Beilage bitte.", "Beilage")],
)
def test_group_answer_adds_the_option(session, side_dish, answer, group):
    """The answer to "Meinen Sie Reis bei Beilage oder bei Extra?" is the group,
    not a dish: the option enters the order in that group."""
    _, turns = _bestellung(session, side_dish, "Die 55 mit Reis.", answer)
    assert "Meinen Sie Reis bei Beilage oder bei Extra?" in " ".join(turns[1].say)
    assert "search_menu" not in str(turns[2].tools)
    [order] = orders(session)
    assert order.status == "confirmed"
    assert _chosen(session, order) == [(group, "Reis")]


def test_group_answer_repeats_the_surcharge(session, side_dish):
    """Codex PR #178: the surcharge of the chosen group is said at once, as for
    an option named directly - from the menu, never from the script."""
    _, turns = _bestellung(session, side_dish, "Die 55 mit Reis.", "Extra.")
    assert "mit Reis, 2 Euro Aufpreis" in " ".join(turns[2].say)


def test_group_question_is_the_only_question(session, side_dish):
    """No "Darf es noch etwas sein?" next to it, and an answer that names no
    group leaves the question open instead of being searched as a dish."""
    _, turns = _bestellung(
        session, side_dish, "Die 55 mit Reis.", "Wie bitte?", "Extra."
    )
    assert "Darf es noch etwas sein?" not in " ".join(turns[1].say)
    assert "Meinen Sie Reis bei Beilage oder bei Extra?" in " ".join(turns[2].say)
    assert "search_menu" not in str(turns[2].tools)
    [order] = orders(session)
    assert _chosen(session, order) == [("Extra", "Reis")]


@pytest.mark.parametrize("answer", ["Nein.", "Weder noch."])
def test_rejected_group_question_adds_no_option(session, side_dish, answer):
    """A no drops the option, never one of the groups: the dish stays as it is
    on the menu, and the readback shows it."""
    _bestellung(session, side_dish, "Die 55 mit Reis.", answer)
    [order] = orders(session)
    assert order.status == "confirmed"
    assert positions(session, order) == [("55", 1, [])]


def test_group_named_with_a_no_is_asked_again(session, side_dish):
    """ "Nicht als Beilage" names a group and rejects it: no choice either way."""
    _, turns = _bestellung(
        session, side_dish, "Die 55 mit Reis.", "Nicht als Beilage.", "Extra."
    )
    assert "Meinen Sie Reis bei Beilage oder bei Extra?" in " ".join(turns[2].say)
    [order] = orders(session)
    assert _chosen(session, order) == [("Extra", "Reis")]


@pytest.mark.parametrize(
    ("answer", "taken"),
    [
        ("Keine davon, lieber Frühlingsrollen.", ("23", 1, [])),
        ("Nein, lieber zweimal die 24.", ("24", 2, [])),
    ],
)
def test_rejection_with_replacement_is_searched(session, tenant, answer, taken):
    """Codex PR #133: the rejection closes the question, the dish named in the
    same answer is searched - with its own quantity, not the two soups'."""
    _, turns = _suppe_und(session, tenant, answer)
    assert "Meinen Sie" not in " ".join(turns[2].say)
    [order] = orders(session)
    assert positions(session, order) == [taken]


@pytest.mark.parametrize(
    ("answer", "rest"),
    [
        ("Keine davon, lieber Frühlingsrollen.", "Frühlingsrollen."),
        ("Nein, die 23", "die 23"),
        ("Weder noch, dann zwei Pho Bo", "zwei Pho Bo"),
        # A standalone rejection: nothing to search.
        ("Keine davon.", None),
        ("Nein danke.", None),
        # Courtesy only, no dish (Codex PR #178).
        ("Nein, vielen Dank.", None),
        ("Nein, danke schön.", None),
        ("Nein, danke sehr.", None),
        ("Nein danke, bitte die 23.", "die 23."),
        ("Nein, nicht die.", None),
        ("Nein, ich weiß nicht.", None),
        ("Nein, lieber nichts.", None),
        # "weder ... noch" rejects what it names (Codex PR #178).
        ("Weder die 12 noch die 13.", None),
        # "Keine Suppe" negates the dish, it is not a rejection plus a dish.
        ("Keine Suppe, lieber die 23.", None),
        # Not a rejection at all.
        ("Lieber Frühlingsrollen.", None),
    ],
)
def test_replacement_after_a_rejection(answer, rest):
    from sim.scripted_order import _replacement

    assert _replacement(answer) == rest


def test_offered_dish_after_a_no_keeps_the_quantity(session, tenant):
    """ "Nein, die 13" names one of the offered dishes: a choice, with the
    quantity of the question, not a new search with its own."""
    _, turns = _suppe_und(session, tenant, "Nein, die 13.")
    assert "search_menu" not in str(turns[2].tools)
    [order] = orders(session)
    assert positions(session, order) == [("13", 2, [])]


def test_standalone_rejection_searches_nothing(session, tenant):
    _, turns = replay(
        session,
        case(
            "Ich moechte etwas zum Abholen bestellen.",
            "Eine Suppe.",
            "Nein, nicht die.",
        ),
        tenant,
        now=NOW,
    )
    assert turns[2].tools == []
    assert "Was möchten Sie bestellen?" in " ".join(turns[2].say)


EIGHT = {"number": "31", "name": "8 Schätze"}


@pytest.mark.parametrize(
    ("said_words", "hit", "quantity"),
    [
        # Codex PR #133: found via the alias "acht schaetze", the 8 is the name.
        ("acht schaetze", EIGHT, 1),
        ("Acht Schätze", EIGHT, 1),
        ("8 Schätze", ACHT, 1),
        ("8 Schätze", EIGHT, 1),
        # A quantity in front of the name still counts.
        ("zwei acht schaetze", EIGHT, 2),
        ("zweimal acht schaetze", EIGHT, 2),
        ("acht Frühlingsrollen", FRUEHLING, 8),
    ],
)
def test_digit_in_the_name_against_a_number_word(said_words, hit, quantity):
    assert _quantity(said_words, hit) == quantity


def test_allergy_in_the_opening_sentence_is_an_order(session, tenant):
    """Codex PR #139: the first sentence announces a pickup and names the
    guest's own allergy - a note on the position (E14), not a callback."""
    replay(
        session,
        case(
            "Ich moechte Pho Bo mit Erdnussallergie zum Abholen.",
            "Nein, das wars.",
            "Auf den Namen Mueller.",
            "0721 5551234",
            "Ja.",
        ),
        tenant,
        now=NOW,
    )
    session.expire_all()
    assert list(session.scalars(select(Callback))) == []
    [order] = orders(session)
    assert order.status == "confirmed"
    assert positions(session, order) == [("13", 1, [])]
    assert _notes(session, order) == ["WICHTIG: Keine Erdnuss. Grund: Allergie"]


@pytest.mark.parametrize(
    "opening",
    [
        # The question about allergens goes to the team, pickup or not.
        "Ist die Pho Bo allergenfrei? Ich moechte sie zum Abholen.",
        "Ich moechte bestellen, koennen Sie das auch liefern?",
        # "Karte" next to the pickup stays what it was before the order.
        "Koennen Sie mir die Speisekarte vorlesen? Ich moechte bestellen.",
        # No pickup named: the allergy alone starts no order.
        "Guten Tag, ich habe eine Erdnussallergie.",
    ],
)
def test_out_of_scope_in_the_opening_sentence_stays_a_callback(
    session, tenant, opening
):
    replay(session, case(opening, "0721 5551234"), tenant, now=NOW)
    session.expire_all()
    assert [c.reason for c in session.scalars(select(Callback))] == ["out_of_scope"]
    assert orders(session) == []


@pytest.mark.parametrize(
    "lines",
    [
        # No dish the allergy could be noted on.
        ("Ich habe eine Erdnussallergie und moechte etwas zum Abholen bestellen.",),
        # The dish is not on the menu.
        ("Ich moechte Schnitzel mit Erdnussallergie zum Abholen.",),
        ("Guten Tag.", "Schnitzel mit Erdnussallergie zum Abholen."),
        # A question about the dish, worded with the guest's allergy (Codex PR #178).
        (
            "Ist in der Pho etwas, gegen das ich allergisch sein koennte? "
            "Ich moechte sie zum Abholen.",
        ),
    ],
)
def test_allergy_that_reaches_no_position_goes_to_the_team(session, tenant, lines):
    """The allergy in the sentence that announces the pickup is a note only if
    the search puts it on a dish. Otherwise it must not get lost: the team
    calls back, as before the order."""
    replay(session, case(*lines, "0721 5551234"), tenant, now=NOW)
    session.expire_all()
    assert [c.reason for c in session.scalars(select(Callback))] == ["out_of_scope"]
    assert orders(session) == []


def test_allergy_on_a_sold_out_dish_goes_to_the_team(session, tenant):
    session.execute(
        update(MenuItem)
        .where(MenuItem.tenant_id == tenant.id, MenuItem.number == "13")
        .values(sold_out_until=datetime(2026, 9, 15, 23, 0, tzinfo=BERLIN))
    )
    session.commit()
    replay(
        session,
        case("Ich moechte Pho Bo mit Erdnussallergie zum Abholen.", "0721 5551234"),
        tenant,
        now=NOW,
    )
    session.expire_all()
    assert [c.reason for c in session.scalars(select(Callback))] == ["out_of_scope"]


def test_allergy_on_an_ambiguous_dish_goes_to_the_team(session, tenant):
    """Codex PR #178: "Suppe" is two dishes. The choice can still be rejected,
    and the allergy would go with it - so it is not noted yet, the team calls
    back as before the order."""
    replay(
        session,
        case(
            "Ich moechte eine Suppe mit Erdnussallergie zum Abholen.",
            "0721 5551234",
        ),
        tenant,
        now=NOW,
    )
    session.expire_all()
    assert [c.reason for c in session.scalars(select(Callback))] == ["out_of_scope"]
    assert orders(session) == []
