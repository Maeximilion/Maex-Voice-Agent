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
    assert chunks[1] == "event: today\ndata: 1:2026-09-15T18:00:00+00:00\n\n"


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

    from api.db import get_db
    from api.main import app

    class FakeSession:
        def scalars(self, *_):
            return self

        def order_by(self, *_):
            return self

        def first(self):
            return None

    def fake_db():
        yield FakeSession()

    app.dependency_overrides[get_db] = fake_db
    try:
        with TestClient(app) as client, client.stream("GET", "/gui/events") as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            assert next(response.iter_lines()) == "event: problem"
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.parametrize("wert", [sse.POLL_SECONDS, sse.HEARTBEAT_SECONDS])
def test_takte_sind_gesetzt(wert):
    assert wert > 0
