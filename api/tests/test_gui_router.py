"""gui/router: Betriebsansicht, HTMX-Fragment, eigene Dateien, Zustand ohne Mandant."""

import uuid
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from api.core.time import DAY_STARTS_AT, business_day
from api.db import get_db
from api.main import app
from api.models import Call, Reservation, ServiceConfig
from api.tests.conftest import p95_ms
from scripts.seed import seed

BERLIN = ZoneInfo("Europe/Berlin")


def heute(hh: int, mm: int = 0) -> datetime:
    """Immer der laufende Betriebstag - die Ansicht zeigt nur ihn.

    Betriebstag, nicht Kalendertag: zwischen 00:00 und 05:00 Uhr laeuft noch der
    Tag davor (core/time.py), eine Uhrzeit vor 05:00 gehoert also schon auf das
    Kalenderdatum danach. Mit dem Kalenderdatum lief der Test in diesen fuenf
    Stunden ins Leere - die Reservierung fiel aus dem Fenster, das die Ansicht
    abfragt, und die Spalte Heute war leer.
    """
    tag = business_day(tz_name="Europe/Berlin")
    if time(hh, mm) < DAY_STARTS_AT:
        tag += timedelta(days=1)
    return datetime.combine(tag, time(hh, mm), tzinfo=BERLIN)


@pytest.fixture(autouse=True)
def feste_uhr(monkeypatch):
    """Anlage und Abfrage sehen denselben Zeitpunkt.

    Ohne das holen sich Testhelfer und Endpunkt den Betriebstag zu zwei
    verschiedenen Augenblicken: ein Lauf, der um 04:59 anlegt und um 05:00
    abfragt, legt auf den einen Betriebstag und fragt den naechsten ab. Die
    Uhr steht deshalb fest auf 12:00 Ortszeit des laufenden Betriebstags -
    weit genug von der Grenze, dass kein Lauf sie zufaellig ueberschreitet.
    """
    mittag = datetime.combine(
        business_day(tz_name="Europe/Berlin"), time(12, 0), tzinfo=BERLIN
    ).astimezone(UTC)
    monkeypatch.setattr("api.core.time.utcnow", lambda: mittag)


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


def reservierung(engine, tenant_id, *, name="Müller", hh=19, party_size=4, note=None):
    with Session(engine) as s:
        call = Call(
            tenant_id=tenant_id,
            external_session_id="ext",
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
                guest_name=name,
                phone="+4972215551234",
                party_size=party_size,
                reserved_for=heute(hh),
                note=note,
                idempotency_key=uuid.uuid4().hex,
            )
        )
        s.commit()


def test_betriebsansicht_zeigt_reservierung(client, engine, tenant_id):
    reservierung(engine, tenant_id, name="Müller", hh=19, note="Fenster")

    response = client.get("/gui/")

    assert response.status_code == 200
    body = response.text
    assert "Heute" in body and "Müller" in body
    assert "19:00" in body and "4 Personen" in body and "Fenster" in body
    # Kopfzeile aus service_config, nicht aus dem Code (CLAUDE.md §2 Regel 1).
    assert "KI hört mit" in body and "Lieferung an" in body


def test_kopfzeile_folgt_dem_modus(client, engine, tenant_id):
    with Session(engine) as s:
        config = s.get(ServiceConfig, tenant_id)
        config.call_mode = "paused"
        config.delivery_enabled = False
        s.commit()

    body = client.get("/gui/").text

    assert "KI ist aus" in body and "Lieferung aus" in body
    assert "KI nimmt an" not in body


def test_fragment_ohne_reservierung_zeigt_leerzustand(client, tenant_id):
    response = client.get("/gui/fragments/heute")

    assert response.status_code == 200
    assert "Noch keine Reservierungen" in response.text
    # Fragment ohne Seitengeruest, damit HTMX es direkt einsetzen kann.
    assert "<html" not in response.text


def test_fragment_liefert_nur_die_liste(client, engine, tenant_id):
    reservierung(engine, tenant_id, name="Schmidt", hh=20, party_size=1)

    body = client.get("/gui/fragments/heute").text

    assert "Schmidt" in body and "1 Person<" in body and "<html" not in body


def test_ohne_mandant_klartext_statt_stacktrace(client):
    """Frisch migrierte Datenbank ohne seed: die Seite muss trotzdem lesbar bleiben."""
    for pfad in ("/gui/", "/gui/fragments/heute"):
        response = client.get(pfad)
        assert response.status_code == 503
        assert "Keine Betriebsdaten" in response.text
        assert "Traceback" not in response.text


def test_eigene_dateien_werden_ausgeliefert(client):
    css = client.get("/gui/static/app.css")
    htmx = client.get("/gui/static/htmx.min.js")

    assert css.status_code == 200 and "--accent" in css.text
    assert htmx.status_code == 200 and len(htmx.content) > 10_000


def test_fragment_bleibt_schnell(client, engine, tenant_id):
    for i in range(20):
        reservierung(engine, tenant_id, name=f"Gast {i}", hh=17 + i % 5)

    assert p95_ms(lambda: client.get("/gui/fragments/heute")) < 300


def test_caddy_schuetzt_die_gui():
    """Befund Codex P1: `/gui/*` zeigt Gastnamen und Telefonnummern.

    Die Anwendung selbst kennt keinen Browser-Zugang (docs/13 §Caddy), also muss
    der Reverse-Proxy im eingecheckten Produktionsstapel wirklich davorstehen -
    sonst liegen personenbezogene Daten offen im Netz.
    """
    caddyfile = Path(__file__).resolve().parents[2] / "deploy" / "Caddyfile"
    text = caddyfile.read_text()

    assert "basic_auth" in text
    assert "/gui" in text
    # Kein Zugang im Repo: Benutzer und Hash kommen aus der Umgebung (CLAUDE.md §8).
    assert "{$GUI_BASIC_AUTH_USER}" in text and "{$GUI_BASIC_AUTH_HASH}" in text


# --- Kopfzeile: Knoepfe (T-3.2) ---------------------------------------------

HX = {"HX-Request": "true"}


def config_von(engine, tenant_id) -> ServiceConfig:
    with Session(engine) as s:
        config = s.get(ServiceConfig, tenant_id)
        s.expunge(config)
        return config


def test_kopfzeile_zeigt_die_knoepfe(client, tenant_id):
    body = client.get("/gui/").text

    assert 'id="kopfzeile"' in body
    for knopf in ("KI pausieren", "Lieferung aus", "Wartezeit +15", "Wartezeit +30"):
        assert knopf in body


def test_not_aus_ein_tap_ohne_rueckfrage(client, engine, tenant_id):
    response = client.post("/gui/kopfzeile/ki-pausieren", headers=HX)

    assert response.status_code == 200
    assert config_von(engine, tenant_id).call_mode == "paused"
    body = response.text
    # Nur die Kopfzeile, grau, roter Punkt mit Text, Einschalten fragt nach.
    assert "<html" not in body and 'class="aus"' in body
    assert "KI ist aus" in body and "KI einschalten" in body and "hx-confirm" in body
    assert 'hx-post="/gui/kopfzeile/ki-pausieren"' not in body


def test_pausieren_fragt_nie_nach(client, tenant_id):
    """Der Not-Aus darf keine Sekunde kosten (docs/06 §3)."""
    body = client.get("/gui/fragments/kopfzeile").text
    knopf = body.split('hx-post="/gui/kopfzeile/ki-pausieren"')[1].split(">")[0]

    assert "hx-confirm" not in knopf


def test_einschalten_stellt_den_modus_wieder_her(client, engine, tenant_id):
    client.post("/gui/kopfzeile/ki-pausieren", headers=HX)

    body = client.post("/gui/kopfzeile/ki-einschalten", headers=HX).text

    # Seed steht auf shadow; genau dahin geht es zurueck.
    assert config_von(engine, tenant_id).call_mode == "shadow"
    assert "KI hört mit" in body and "KI pausieren" in body


def test_lieferung_aus_und_an(client, engine, tenant_id):
    body = client.post("/gui/kopfzeile/lieferung/aus", headers=HX).text
    assert config_von(engine, tenant_id).delivery_enabled is False
    assert 'hx-post="/gui/kopfzeile/lieferung/an"' in body

    client.post("/gui/kopfzeile/lieferung/an", headers=HX)
    assert config_von(engine, tenant_id).delivery_enabled is True


def test_wartezeit_plus_15(client, engine, tenant_id):
    vorher = config_von(engine, tenant_id)

    body = client.post("/gui/kopfzeile/wartezeit/15", headers=HX).text

    nachher = config_von(engine, tenant_id)
    assert nachher.pickup_wait_minutes == vorher.pickup_wait_minutes + 15
    assert nachher.delivery_wait_minutes == vorher.delivery_wait_minutes + 15
    assert (
        f"Wartezeit {nachher.pickup_wait_minutes} / {nachher.delivery_wait_minutes} Min"
        in body
    )


@pytest.mark.parametrize(
    "pfad", ["/gui/kopfzeile/wartezeit/20", "/gui/kopfzeile/lieferung/vielleicht"]
)
def test_unbekannter_knopf_aendert_nichts(client, engine, tenant_id, pfad):
    vorher = config_von(engine, tenant_id)

    assert client.post(pfad, headers=HX).status_code == 404

    nachher = config_von(engine, tenant_id)
    assert nachher.pickup_wait_minutes == vorher.pickup_wait_minutes
    assert nachher.delivery_enabled == vorher.delivery_enabled


def test_fremde_seite_kann_nicht_schalten(client, engine, tenant_id):
    """Ohne HX-Request kein Schalten: Basic-Auth allein haelt fremde Formulare nicht auf."""
    response = client.post("/gui/kopfzeile/ki-pausieren")

    assert response.status_code == 403
    assert config_von(engine, tenant_id).call_mode == "shadow"


def test_schalten_ohne_mandant_klartext(client):
    response = client.post("/gui/kopfzeile/ki-pausieren", headers=HX)

    assert response.status_code == 503
    assert "Keine Betriebsdaten" in response.text


def test_fehlende_konfiguration_rote_leiste(client, engine, tenant_id):
    """Mandant da, service_config weg: rote Leiste in der Kopfzeile, kein Stacktrace."""
    with Session(engine) as s:
        s.delete(s.get(ServiceConfig, tenant_id))
        s.commit()

    response = client.post("/gui/kopfzeile/wartezeit/15", headers=HX)

    assert response.status_code == 409
    assert "Bitte nochmal tippen" in response.text
    assert "Traceback" not in response.text


def test_schalten_bleibt_schnell(client, tenant_id):
    assert p95_ms(lambda: client.post("/gui/kopfzeile/lieferung/aus", headers=HX)) < 300
    assert p95_ms(lambda: client.get("/gui/fragments/kopfzeile")) < 300


def test_nur_html_fehler_ersetzen_die_kopfzeile():
    """Befund Codex P2 (PR #111): Fehler ausserhalb von AppError kommen als JSON.

    main.py uebersetzt HTTPException und unbehandelte Fehler in die JSON-Huelle.
    Tauscht app.js die ungeprueft ein, steht JSON statt Knoepfen in der Kopfzeile.
    Kein JS-Testlauf im Projekt, deshalb der Vertrag als Textpruefung.
    """
    gui = Path(__file__).resolve().parents[1] / "gui"
    js = (gui / "static" / "app.js").read_text(encoding="utf-8")
    base = (gui / "templates" / "base.html").read_text(encoding="utf-8")

    assert 'getResponseHeader("Content-Type")' in js and "text/html" in js
    # Alles andere zeigt die rote Leiste mit "Nochmal versuchen" (docs/06 §5).
    assert 'id="stoerung"' in base and "Nochmal versuchen" in base


def test_fremder_status_kommt_als_json(client, tenant_id):
    """Warum der Test oben noetig ist: ein 404 aus dem Router ist JSON, kein HTML."""
    response = client.post("/gui/kopfzeile/wartezeit/20", headers=HX)

    assert response.headers["content-type"].startswith("application/json")
