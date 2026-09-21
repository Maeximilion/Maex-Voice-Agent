"""gui/sse: Ereignisstrom der Spalte "Heute" - Signal, Lebenszeichen, Trennung, DB weg."""

import asyncio
import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy.exc import OperationalError

from api.gui import sse

TENANT = uuid.uuid4()
TZ = "Europe/Berlin"


class FakeRequest:
    """Ersetzt starlette.Request: der Strom fragt nur, ob der Browser noch dran ist."""

    def __init__(self, disconnect_at: int | None = None) -> None:
        self.disconnect_at = disconnect_at
        self.calls = 0

    async def is_disconnected(self) -> bool:
        self.calls += 1
        return self.disconnect_at is not None and self.calls > self.disconnect_at


@pytest.fixture(autouse=True)
def feste_kopfzeile(monkeypatch):
    """Die meisten Tests hier pruefen die Spalte Heute; die Kopfzeile steht still."""
    monkeypatch.setattr(sse, "_header_token", lambda *_: "h0")
    monkeypatch.setattr(sse, "_callbacks_token", lambda *_: "c0")


def drain(stream: AsyncIterator[str]) -> list[str]:
    async def run() -> list[str]:
        return [chunk async for chunk in stream]

    return asyncio.run(run())


def stream(request, **kw) -> AsyncIterator[str]:
    kw.setdefault("poll_seconds", 0)
    return sse.today_event_stream(request, TENANT, TZ, **kw)


def test_erstes_ereignis_direkt_nach_dem_verbinden(monkeypatch):
    """Was waehrend einer Trennung gebucht wurde, ist sofort wieder auf dem Tablet."""
    monkeypatch.setattr(sse, "_token", lambda *_: "1:2026-09-15T18:00:00+00:00")

    chunks = drain(stream(FakeRequest(), max_ticks=1))

    assert chunks[0].startswith("retry: ")
    assert chunks[1] == "event: header\ndata: h0\n\n"
    assert chunks[2] == "event: today\ndata: 1:2026-09-15T18:00:00+00:00\n\n"


def test_nur_bei_aenderung_ein_ereignis(monkeypatch):
    tokens = iter(["0:-", "0:-", "1:x"])
    monkeypatch.setattr(sse, "_token", lambda *_: next(tokens))

    ereignisse = [c for c in drain(stream(FakeRequest(), max_ticks=3)) if "today" in c]

    assert ereignisse == ["event: today\ndata: 0:-\n\n", "event: today\ndata: 1:x\n\n"]


def test_lebenszeichen_haelt_die_verbindung(monkeypatch):
    monkeypatch.setattr(sse, "_token", lambda *_: "0:-")

    chunks = drain(stream(FakeRequest(), max_ticks=3, heartbeat_seconds=0))

    assert chunks.count(": keepalive\n\n") == 2


def test_getrennter_browser_beendet_den_strom(monkeypatch):
    monkeypatch.setattr(sse, "_token", lambda *_: "0:-")
    request = FakeRequest(disconnect_at=1)

    chunks = drain(stream(request, max_ticks=50))

    assert len([c for c in chunks if "today" in c]) == 1
    assert request.calls == 2


def test_datenbank_weg_meldet_statt_abzubrechen(monkeypatch):
    """Die gelbe Leiste ist die richtige Antwort, ein toter Strom nicht (docs/06 §5)."""

    def kaputt(*_):
        raise OperationalError("select", {}, Exception("keine Verbindung"))

    monkeypatch.setattr(sse, "_token", kaputt)

    chunks = drain(stream(FakeRequest(), max_ticks=2))

    assert chunks.count("event: problem\ndata: db\n\n") == 2


def test_endpunkt_liefert_ereignisstrom(monkeypatch):
    """Der Strom haengt am Endpunkt und traegt die Koepfe gegen puffernde Proxies."""
    from fastapi.testclient import TestClient

    from api.gui import router as gui_router
    from api.main import app

    monkeypatch.setattr(gui_router, "_tenant_snapshot", lambda: (None, "UTC"))

    with TestClient(app) as client, client.stream("GET", "/gui/events") as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert next(response.iter_lines()) == "event: problem"


@pytest.mark.parametrize("wert", [sse.POLL_SECONDS, sse.HEARTBEAT_SECONDS])
def test_takte_sind_gesetzt(wert):
    assert wert > 0


def test_strom_haelt_keine_request_session_fest(monkeypatch):
    """Befund Codex P2: ein offener Strom darf keine Verbindung aus dem Pool binden.

    Ein Tablet haelt den Strom stundenlang. Haengt die Session an der Anfrage,
    ist ihre Verbindung genauso lange belegt und ein paar Geraete legen den Pool
    und damit den heissen Pfad lahm.
    """
    import uuid as _uuid

    from fastapi.testclient import TestClient

    from api.gui import router as gui_router
    from api.main import app

    geschlossen: list[bool] = []

    class Tenant:
        id = _uuid.uuid4()
        timezone = "Europe/Berlin"

    class TrackingSession:
        def scalars(self, *_):
            return self

        def order_by(self, *_):
            return self

        def first(self):
            return Tenant()

        def close(self):
            geschlossen.append(True)

    monkeypatch.setattr(gui_router, "SessionLocal", TrackingSession)
    monkeypatch.setattr(sse, "_token", lambda *_: "0:-")
    # Der echte Strom laeuft endlos; hier genuegt ein Takt. Geprueft wird die
    # Session des Endpunkts, nicht die Schleife.
    monkeypatch.setattr(
        gui_router,
        "today_event_stream",
        lambda request, tenant_id, tz_name: sse.today_event_stream(
            request, tenant_id, tz_name, poll_seconds=0, max_ticks=1
        ),
    )

    with TestClient(app) as client, client.stream("GET", "/gui/events") as response:
        assert response.status_code == 200
        next(response.iter_lines())
        # Der Strom laeuft noch, die Session ist trotzdem schon zurueckgegeben.
        assert geschlossen == [True]


def test_erholung_nimmt_die_gelbe_leiste_weg(monkeypatch):
    """Befund Codex P2: nach einem DB-Aussetzer muss ein Signal kommen, auch ohne Aenderung.

    Sonst bleibt die gelbe Leiste stehen, bis zufaellig jemand reserviert.
    """
    zustand = iter(["0:-", "fehler", "0:-"])

    def token(*_):
        wert = next(zustand)
        if wert == "fehler":
            raise OperationalError("select", {}, Exception("keine Verbindung"))
        return wert

    monkeypatch.setattr(sse, "_token", token)

    chunks = drain(stream(FakeRequest(), max_ticks=3))

    assert chunks[1:] == [
        "event: header\ndata: h0\n\n",
        "event: today\ndata: 0:-\n\n",
        "event: callbacks\ndata: c0\n\n",
        "event: problem\ndata: db\n\n",
        # Nach der Erholung alle Signale, damit jeder Bereich frisch ist.
        "event: header\ndata: h0\n\n",
        "event: today\ndata: 0:-\n\n",
        "event: callbacks\ndata: c0\n\n",
    ]


def test_umgeschaltete_kopfzeile_sendet_nur_header(monkeypatch):
    """Pausiert ein Tablet die KI, sehen es alle - ohne die Liste neu zu laden."""
    monkeypatch.setattr(sse, "_token", lambda *_: "0:-")
    kopf = iter(["h0", "h0", "h1"])
    monkeypatch.setattr(sse, "_header_token", lambda *_: next(kopf))

    chunks = drain(stream(FakeRequest(), max_ticks=3))

    assert [c for c in chunks if c.startswith("event:")] == [
        "event: header\ndata: h0\n\n",
        "event: today\ndata: 0:-\n\n",
        "event: callbacks\ndata: c0\n\n",
        "event: header\ndata: h1\n\n",
    ]


def test_neuer_rueckruf_sendet_nur_callbacks(monkeypatch):
    """Ein neuer Rückruf lädt nur seine Spalte nach, nicht Kopfzeile und Liste."""
    monkeypatch.setattr(sse, "_token", lambda *_: "0:-")
    rueck = iter(["0:x", "1:y"])
    monkeypatch.setattr(sse, "_callbacks_token", lambda *_: next(rueck))

    chunks = drain(stream(FakeRequest(), max_ticks=2))

    assert [c for c in chunks if c.startswith("event:")][-1] == (
        "event: callbacks\ndata: 1:y\n\n"
    )
    assert len([c for c in chunks if c.startswith("event:")]) == 4


def test_kopfzeile_ohne_datenbank_meldet_problem(monkeypatch):
    monkeypatch.setattr(sse, "_token", lambda *_: "0:-")

    def kaputt(*_):
        raise OperationalError("select", {}, Exception("keine Verbindung"))

    monkeypatch.setattr(sse, "_header_token", kaputt)

    chunks = drain(stream(FakeRequest(), max_ticks=1))

    assert chunks[1:] == ["event: problem\ndata: db\n\n"]


def test_ein_takt_braucht_eine_sitzung(monkeypatch):
    """Drei Fingerabdruecke, eine Sitzung.

    Je Takt und Tablet eine Entnahme aus dem Pool, nicht drei - bei zwei
    Sekunden Takt summiert sich das allein fuers Nachsehen (Review PR #117).
    """
    geoeffnet: list[object] = []
    geschlossen: list[object] = []

    class ZaehlendeSitzung:
        def __init__(self):
            geoeffnet.append(self)

        def close(self):
            geschlossen.append(self)

    monkeypatch.setattr(sse, "SessionLocal", ZaehlendeSitzung)
    monkeypatch.setattr(sse, "_header_token", lambda *_: "h")
    monkeypatch.setattr(sse, "_token", lambda *_: "t")
    monkeypatch.setattr(sse, "_callbacks_token", lambda *_: "c")

    tokens = sse._tokens(uuid.uuid4(), "Europe/Berlin")

    assert tokens == {"header": "h", "today": "t", "callbacks": "c"}
    assert len(geoeffnet) == 1 and len(geschlossen) == 1
