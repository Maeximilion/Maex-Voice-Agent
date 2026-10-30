"""gui/router: Betriebsansicht, HTMX-Fragment, eigene Dateien, Zustand ohne Mandant."""

import uuid
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from api.config import settings
from api.core.time import DAY_STARTS_AT, business_day
from api.db import get_db
from api.domain.status import config as switches
from api.gui.router import _page_response
from api.main import app
from api.models import Call, CallModeWindow, Reservation, ServiceConfig
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
        c = TestClient(app)
        c.cookies.set("csrf_token", "testtoken")
        yield c
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


def test_the_view_can_go_on_the_home_screen(client):
    """T-3.7: the manifest and its icons are served, and every page links the
    manifest with credentials, which Caddy's Basic-Auth needs."""
    manifest = client.get("/gui/static/manifest.json").json()

    assert manifest["start_url"] == "/gui/" and manifest["display"] == "fullscreen"
    for icon in manifest["icons"]:
        image = client.get(icon["src"])
        assert image.status_code == 200 and image.content.startswith(b"\x89PNG")
    page = client.get("/gui/").text
    assert (
        '<link rel="manifest" href="/gui/static/manifest.json" '
        'crossorigin="use-credentials">' in page
    )


@pytest.mark.latency
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

HX = {"HX-Request": "true", "X-CSRF-Token": "testtoken"}


def config_von(engine, tenant_id) -> ServiceConfig:
    with Session(engine) as s:
        config = s.get_one(ServiceConfig, tenant_id)
        s.expunge(config)
        return config


def test_kopfzeile_zeigt_die_knoepfe(client, tenant_id):
    body = client.get("/gui/").text

    assert 'id="kopfzeile"' in body
    for knopf in (
        "KI pausieren",
        "Lieferung aus",
        "Wartezeit Abholung 15 Minuten weniger",
        "Wartezeit Abholung 15 Minuten mehr",
        "Wartezeit Lieferung 15 Minuten weniger",
        "Wartezeit Lieferung 15 Minuten mehr",
    ):
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


def mode_button(body: str, mode: str) -> str:
    """The opening tag of the switch's button for `mode`."""
    return body.split(f'hx-post="/gui/kopfzeile/modus/{mode}"')[1].split(">")[0]


def test_the_header_offers_the_three_modes(client, tenant_id):
    """T-8.1: the mode is switched on the tablet (docs/02 §4, docs/06 §3); the
    one that holds is marked, in words and for the screen reader."""
    body = client.get("/gui/fragments/kopfzeile").text

    for mode in ("shadow", "overflow", "primary"):
        assert f'hx-post="/gui/kopfzeile/modus/{mode}"' in body
    assert 'aria-pressed="true"' in mode_button(body, "shadow")
    assert 'aria-pressed="false"' in mode_button(body, "primary")


def test_more_ai_asks_first_and_less_ai_is_one_tap(client, engine, tenant_id):
    """The way back is one tap without thinking (docs/09 §4); a step that lets
    the agent take more calls asks first, as switching it on after a pause."""
    with Session(engine) as s:
        s.get_one(ServiceConfig, tenant_id).call_mode = "overflow"
        s.commit()

    body = client.get("/gui/fragments/kopfzeile").text

    assert "hx-confirm" in mode_button(body, "primary")
    assert "hx-confirm" not in mode_button(body, "shadow")


def test_a_tap_switches_the_mode(client, engine, tenant_id):
    response = client.post("/gui/kopfzeile/modus/overflow", headers=HX)

    assert response.status_code == 200
    assert config_von(engine, tenant_id).call_mode == "overflow"
    assert "KI springt ein" in response.text
    assert 'aria-pressed="true"' in mode_button(response.text, "overflow")


@pytest.mark.parametrize("mode", ["paused", "an", "einspringen"])
def test_the_mode_switch_takes_only_the_three_modes(client, engine, tenant_id, mode):
    assert client.post(f"/gui/kopfzeile/modus/{mode}", headers=HX).status_code == 404

    assert config_von(engine, tenant_id).call_mode == "shadow"


def test_a_paused_header_offers_no_mode(client, engine, tenant_id):
    """Only "KI einschalten", which asks first, ends the emergency stop."""
    client.post("/gui/kopfzeile/ki-pausieren", headers=HX)

    body = client.get("/gui/fragments/kopfzeile").text

    assert "/gui/kopfzeile/modus/" not in body


def test_the_header_names_the_window_that_holds(client, engine, tenant_id):
    with Session(engine) as s:
        for weekday in range(7):
            s.add(
                CallModeWindow(
                    tenant_id=tenant_id,
                    weekday=weekday,
                    starts_at=time(0),
                    ends_at=time(23, 30),
                    call_mode="primary",
                )
            )
        s.commit()

    body = client.get("/gui/fragments/kopfzeile").text

    assert "KI nimmt an" in body and "nach Plan bis 23:30" in body
    assert 'aria-pressed="true"' in mode_button(body, "primary")


def test_wartezeit_lieferung_plus_15_laesst_abholung_stehen(client, engine, tenant_id):
    vorher = config_von(engine, tenant_id)

    body = client.post("/gui/kopfzeile/wartezeit/lieferung/15", headers=HX).text

    nachher = config_von(engine, tenant_id)
    assert nachher.delivery_wait_minutes == vorher.delivery_wait_minutes + 15
    assert nachher.pickup_wait_minutes == vorher.pickup_wait_minutes
    assert f"Wartezeit Lieferung {nachher.delivery_wait_minutes} Min" in body


def test_wartezeit_abholung_minus_15_und_knopf_aus_an_der_grenze(
    client, engine, tenant_id
):
    vorher = config_von(engine, tenant_id)

    client.post("/gui/kopfzeile/wartezeit/abholung/-15", headers=HX)
    nachher = config_von(engine, tenant_id)
    assert nachher.pickup_wait_minutes == max(
        vorher.pickup_wait_minutes - 15, switches.MIN_WAIT_MINUTES
    )

    for _ in range(5):
        body = client.post("/gui/kopfzeile/wartezeit/abholung/-15", headers=HX).text
    assert f"Wartezeit Abholung {switches.MIN_WAIT_MINUTES} Min" in body
    knopf = body.split('aria-label="Wartezeit Abholung 15 Minuten weniger"')[1]
    assert knopf.split(">")[0].strip() == "disabled"


@pytest.mark.parametrize(
    "pfad",
    [
        "/gui/kopfzeile/wartezeit/abholung/20",
        "/gui/kopfzeile/wartezeit/abholung/0",
        "/gui/kopfzeile/wartezeit/tisch/15",
        "/gui/kopfzeile/wartezeit/15",
        "/gui/kopfzeile/lieferung/vielleicht",
    ],
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

    response = client.post("/gui/kopfzeile/wartezeit/abholung/15", headers=HX)

    assert response.status_code == 409
    assert "Bitte nochmal tippen" in response.text
    assert "Traceback" not in response.text


@pytest.mark.latency
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
    response = client.post("/gui/kopfzeile/wartezeit/abholung/20", headers=HX)

    assert response.headers["content-type"].startswith("application/json")


class _BrokenSession:
    """A database that went away mid-shift: every use raises."""

    def __getattr__(self, _name):
        raise OperationalError(
            "SELECT 1", {}, Exception("server closed the connection")
        )


def test_unbehandelter_fehler_kommt_nicht_als_200(engine):
    """Audit 27.10.2026: app.js swaps every answer below 400 into the page.

    The JSON envelope answered 200 for an unhandled error, so a database outage
    during "KI pausieren" put `{"ok":false,...}` where the header and its pause
    button stood. Below /gui an error must carry an error status.
    """
    app.dependency_overrides[get_db] = lambda: _BrokenSession()
    try:
        c = TestClient(app, raise_server_exceptions=False)
        c.cookies.set("csrf_token", "testtoken")
        response = c.post("/gui/kopfzeile/ki-pausieren", headers=HX)
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code == 500
    assert response.headers["content-type"].startswith("application/json")
    assert "server closed" not in response.text


def test_ungueltige_eingabe_kommt_nicht_als_200(client):
    response = client.post("/gui/rueckrufe/keine-uuid/erledigt", headers=HX)

    assert response.status_code == 422


# --- Spalte Rueckrufe (T-3.4) -------------------------------------------------


def rueckruf(
    engine, tenant_id, *, reason="not_understood", summary="Bitte zurueckrufen"
):
    from api.models import Callback

    with Session(engine) as s:
        call = Call(
            tenant_id=tenant_id,
            external_session_id=uuid.uuid4().hex,
            started_at=datetime.now(BERLIN),
            delete_after=datetime.now(BERLIN).date(),
        )
        s.add(call)
        s.flush()
        cb = Callback(
            tenant_id=tenant_id,
            call_id=call.id,
            phone="+4917612345678",
            reason=reason,
            summary=summary,
        )
        s.add(cb)
        s.commit()
        return cb.id


def test_spalte_rueckrufe_zeigt_karte(client, engine, tenant_id):
    rueckruf(engine, tenant_id, reason="complaint", summary="Letzte Lieferung war kalt")

    body = client.get("/gui/").text

    assert 'id="rueckrufe-liste"' in body
    assert "Beschwerde" in body and "Letzte Lieferung war kalt" in body
    assert 'href="tel:+4917612345678"' in body and "Erledigt" in body
    # Code-Werte bleiben im Attribut fuer app.js, nie im sichtbaren Text.
    assert 'data-reason="complaint"' in body and ">complaint<" not in body


def test_rueckrufe_leerzustand(client, tenant_id):
    body = client.get("/gui/fragments/rueckrufe").text

    assert "Keine R&uuml;ckrufe offen." in body and "<html" not in body


def test_beschwerde_steht_oben(client, engine, tenant_id):
    rueckruf(engine, tenant_id, summary="Erster normaler")
    rueckruf(engine, tenant_id, reason="complaint", summary="Spaete Beschwerde")

    body = client.get("/gui/fragments/rueckrufe").text

    assert body.index("Spaete Beschwerde") < body.index("Erster normaler")


def test_erledigt_nimmt_die_karte_weg(client, engine, tenant_id):
    cb_id = rueckruf(engine, tenant_id, summary="Weg damit")

    response = client.post(f"/gui/rueckrufe/{cb_id}/erledigt", headers=HX)

    assert response.status_code == 200
    assert "Weg damit" not in response.text
    assert "Keine R&uuml;ckrufe offen." in response.text


def test_erledigt_ohne_hx_request_verboten(client, engine, tenant_id):
    cb_id = rueckruf(engine, tenant_id, summary="Bleibt stehen")

    assert client.post(f"/gui/rueckrufe/{cb_id}/erledigt").status_code == 403
    assert "Bleibt stehen" in client.get("/gui/fragments/rueckrufe").text


def test_unbekannter_rueckruf_liefert_frische_liste(client, engine, tenant_id):
    """Anderes Tablet war schneller: kein Fehler, die aktuelle Liste ist die Antwort."""
    rueckruf(engine, tenant_id, summary="Noch offen")

    response = client.post(f"/gui/rueckrufe/{uuid.uuid4()}/erledigt", headers=HX)

    assert response.status_code == 200 and "Noch offen" in response.text


def test_rueckrufe_ohne_mandant_klartext(client):
    response = client.get("/gui/fragments/rueckrufe")

    assert response.status_code == 503 and "Keine Betriebsdaten" in response.text


@pytest.mark.latency
def test_rueckrufe_bleiben_schnell(client, engine, tenant_id):
    for i in range(20):
        rueckruf(engine, tenant_id, summary=f"Gast {i}")

    assert p95_ms(lambda: client.get("/gui/fragments/rueckrufe")) < 300


def test_ton_wird_im_tap_freigeschaltet():
    """Befund Codex P1 (PR #112): Safari schaltet Web Audio nur innerhalb einer Geste frei.

    Entsteht der AudioContext erst beim ersten Rueckruf, bleibt er auf dem Tablet
    stumm. Der Tap-Handler muss ihn anlegen. Kein JS-Testlauf im Projekt, deshalb
    der Vertrag als Textpruefung.
    """
    js = (Path(__file__).resolve().parents[1] / "gui" / "static" / "app.js").read_text(
        encoding="utf-8"
    )
    handler = js.split("function freischalten()")[1].split("\n  }\n")[0]

    assert "new Ctx()" in handler and "resume()" in handler
    assert 'addEventListener("touchstart", freischalten' in js
    assert 'addEventListener("click", freischalten' in js


def test_spalten_zeigen_ihre_anzahl(client, engine, tenant_id):
    """Ein Blick genuegt (docs/06 §1 Regel 7): die Zahl steht in der Ueberschrift."""
    rueckruf(engine, tenant_id)
    rueckruf(engine, tenant_id)
    reservierung(engine, tenant_id)

    rueck = client.get("/gui/fragments/rueckrufe").text
    heute_ = client.get("/gui/fragments/heute").text

    assert '<span class="count">2</span>' in rueck
    assert '<span class="count">1</span>' in heute_


def test_tablet_schrift_mindestens_18px():
    """docs/06 §1 Regel 3: Notiz, Abzeichen und Zusammenfassung nicht kleiner als 18 px.

    Die Klassen stammen aus dem Desktop-Mockup (13/12 px); die Betriebsansicht
    muss sie ueberschreiben. Und dunkles "Erledigt" braucht dunkle Schrift auf
    hellem Gruen, sonst ist es kaum lesbar.
    """
    css = (
        Path(__file__).resolve().parents[1] / "gui" / "static" / "app.css"
    ).read_text(encoding="utf-8")

    assert ".betrieb .note{font-size:18px}" in css
    assert ".betrieb .badge{font-size:18px" in css
    assert (
        "button.primary{background:var(--accent);border-color:var(--accent);color:var(--surface)}"
        in css
    )


@pytest.mark.parametrize(
    "cookie, header",
    [
        (None, "testtoken"),
        ("testtoken", None),
        ("testtoken", "wrong-token"),
        ("", "testtoken"),
        ("caf\u00e9", "testtoken"),
    ],
)
def test_switch_rejects_invalid_csrf(client, engine, tenant_id, cookie, header):
    client.cookies.clear()
    headers = {"HX-Request": "true"}
    if cookie is not None:
        headers["Cookie"] = f'csrf_token="{cookie}"'.encode("latin-1")
    if header is not None:
        headers["X-CSRF-Token"] = header
    before = config_von(engine, tenant_id).call_mode
    response = client.post("/gui/kopfzeile/ki-pausieren", headers=headers)
    assert response.status_code == 403
    assert config_von(engine, tenant_id).call_mode == before


def test_page_issues_csrf_token_and_accepts_it(client, tenant_id):
    client.cookies.clear()
    response = client.get("/gui/")
    token = client.cookies.get("csrf_token")
    assert token and len(token) >= 32
    # From a file: the proxy's CSP refuses inline scripts (#252 finding 11).
    assert '<script src="/gui/static/base.js" defer>' in response.text
    hook = client.get("/gui/static/base.js").text
    assert 'e.detail.headers["X-CSRF-Token"]' in hook
    assert "SameSite=lax" in response.headers["set-cookie"]
    assert (
        client.post(
            "/gui/kopfzeile/ki-pausieren",
            headers={
                "HX-Request": "true",
                "X-CSRF-Token": token,
            },
        ).status_code
        == 200
    )
    assert client.get("/gui/").cookies.get("csrf_token") == token


def test_page_replaces_a_csrf_cookie_it_could_never_accept(client, tenant_id):
    client.cookies.clear()
    response = client.get(
        "/gui/", headers={"Cookie": 'csrf_token="caf\u00e9"'.encode("latin-1")}
    )
    token = response.cookies.get("csrf_token")
    assert token and token.isascii() and len(token) >= 32


def test_https_page_marks_csrf_cookie_secure(client, tenant_id):
    client.cookies.clear()
    response = client.get("https://testserver/gui/")
    assert "Secure" in response.headers["set-cookie"]


def test_production_csrf_cookie_is_secure_behind_http_proxy(monkeypatch):
    monkeypatch.setattr(settings, "env", "production")
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/gui/dev/console",
            "scheme": "http",
            "server": ("api", 8000),
            "headers": [],
        }
    )
    response = _page_response(request, "dev/console.html", {})
    assert "Secure" in response.headers["set-cookie"]


# --- holiday suggestions (T-9.5) ----------------------------------------------


def feiertag(engine, tenant_id, *, days_ahead=10, status="suggested"):
    from api.models import SpecialDay

    day = business_day(tz_name="Europe/Berlin") + timedelta(days=days_ahead)
    with Session(engine) as s:
        row = SpecialDay(
            tenant_id=tenant_id,
            date=day,
            note="Allerheiligen",
            status=status,
            like_weekday=6,
        )
        s.add(row)
        s.commit()
        return row.id, day


def test_ein_feiertagsvorschlag_steht_mit_drei_antworten_auf_der_seite(
    client, engine, tenant_id
):
    sid, _ = feiertag(engine, tenant_id)
    feiertag(engine, tenant_id, days_ahead=-1)  # past: no card

    body = client.get("/gui").text

    assert body.count("Allerheiligen") == 1
    assert "Wie Sonntag behandeln?" in body
    for choice in ("wie", "geschlossen", "normal"):
        assert f"/gui/feiertage/{sid}/{choice}" in body


def test_ohne_vorschlag_bleibt_der_kasten_leer(client, tenant_id):
    assert "Feiertage" not in client.get("/gui").text


def test_wie_sonntag_nimmt_die_karte_weg_und_gilt(client, engine, tenant_id):
    from api.models import SpecialDay

    sid, _ = feiertag(engine, tenant_id)

    response = client.post(f"/gui/feiertage/{sid}/wie", headers=HX)

    assert response.status_code == 200
    assert "Allerheiligen" not in response.text
    with Session(engine) as s:
        row = s.get(SpecialDay, sid)
        assert (row.status, row.like_weekday, row.closed) == ("set", 6, False)


def test_eine_antwort_ohne_hx_request_ist_verboten(client, engine, tenant_id):
    sid, _ = feiertag(engine, tenant_id)

    assert client.post(f"/gui/feiertage/{sid}/geschlossen").status_code == 403
    assert "Allerheiligen" in client.get("/gui").text


def test_eine_unbekannte_antwort_ist_404_und_ein_alter_vorschlag_kein_fehler(
    client, engine, tenant_id
):
    sid, _ = feiertag(engine, tenant_id)
    assert (
        client.post(f"/gui/feiertage/{sid}/vielleicht", headers=HX).status_code == 404
    )

    unknown = client.post(f"/gui/feiertage/{uuid.uuid4()}/normal", headers=HX)

    assert unknown.status_code == 200
    assert "Allerheiligen" in unknown.text
