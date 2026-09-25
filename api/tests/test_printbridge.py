"""Druckbruecke: Bonlayout, Druckerwege, Protokoll, Durchlauf bis zur Karte (T-4.6)."""

import json
import socket
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import Session

from api.config import settings
from api.db import get_db
from api.main import app
from api.models import Order, OutboxEvent
from api.tests.test_domain_confirm_order import _confirm, _draft, _mode
from api.tests.test_domain_draft_order import _call, _tenant
from printbridge.bridge import SAMPLE, main, run_once
from printbridge.client import Server, ServerError
from printbridge.escpos import CUT, render
from printbridge.state import PrintedLog
from printbridge.transport import (
    PrinterError,
    TcpPrinter,
    WindowsPrinter,
    from_spec,
)

TICKET = {
    "order_id": "11111111-1111-1111-1111-111111111111",
    "type": "pickup",
    "pickup_code": "A17",
    "customer_name": "Müller",
    "phone": "+4972215551234",
    "ready_at": "2026-09-15T16:30:00+00:00",
    "revision": 0,
    "correction_reason": None,
    "total_cents": 2580,
    "items": [
        {
            "number": "23",
            "name": "Phở Bò",
            "quantity": 2,
            "options": [{"group": "Fleisch", "option": "Huhn", "price_delta_cents": 0}],
            "note": "WICHTIG: Keine Erdnüsse. Grund: Allergie",
        }
    ],
}


# --- Bonlayout --------------------------------------------------------------------


def test_bon_enthaelt_was_die_kueche_braucht():
    data = render(TICKET, width=48, printed_at=datetime(2026, 9, 15, 18, 2, tzinfo=UTC))
    assert data.startswith(b"\x1b@\x1bt\x13")  # Init, PC858
    assert data.endswith(CUT)
    text = data.decode("cp858")
    assert "ABHOLUNG A17" in text
    assert "Name: Müller" in text  # Umlaut bleibt in PC858
    # "ở" kennt PC858 nicht und verliert den Akzent statt "?" zu werden; "ò" bleibt.
    assert "2x 23 Pho Bò" in text
    assert "+ Huhn" in text
    assert "\x1bE\x01   ! WICHTIG: Keine Erdnüsse." in text  # Hinweis fett
    assert "25,80 EUR" in text
    assert "Gedruckt 18:02" in text
    assert "KORREKTUR" not in text
    assert "5551234" not in text  # die Kueche braucht keine Telefonnummer


def test_korrektur_steht_oben_mit_grund_und_stand():
    ticket = {**TICKET, "revision": 2, "correction_reason": "wrong_quantity"}
    text = render(ticket).decode("cp858")
    assert text.index("KORREKTUR") < text.index("ABHOLUNG")
    assert "Grund: falsche Menge" in text
    assert "Stand 2" in text


def test_lange_zeilen_werden_umbrochen():
    ticket = {**TICKET, "items": [{**TICKET["items"][0], "name": "Sehr " * 20}]}
    text = render(ticket, width=32).decode("cp858")
    for line in text.split("\n"):
        plain = line.replace("\x1bE\x01", "").replace("\x1bE\x00", "")
        assert len(plain) <= 32 or plain.startswith("\x1b"), plain


# --- Druckerwege ------------------------------------------------------------------


class FakeNetPrinter:
    """Epson mit Netzwerkkarte: antwortet auf DLE EOT 1 und 4, sammelt den Rest."""

    def __init__(self, status: int | None = 0x12, paper: int | None = 0x12):
        self.status, self.paper = status, paper
        self.received = b""
        self.sock = socket.create_server(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        conn, _ = self.sock.accept()
        with conn:
            for reply in (self.status, self.paper):
                query = conn.recv(3)
                if not query:
                    return
                if reply is not None:
                    conn.sendall(bytes([reply]))
            while chunk := conn.recv(4096):
                self.received += chunk

    def close(self):
        self.thread.join(timeout=3)
        self.sock.close()


def test_netzwerkdrucker_bekommt_den_bon():
    fake = FakeNetPrinter()
    TcpPrinter("127.0.0.1", fake.port).send(b"BON")
    fake.close()
    assert fake.received == b"BON"


@pytest.mark.parametrize(
    ("status", "paper", "message"),
    [(0x1A, 0x12, "offline"), (0x12, 0x72, "Papier leer")],
)
def test_netzwerkdrucker_nicht_bereit(status, paper, message):
    fake = FakeNetPrinter(status, paper)
    with pytest.raises(PrinterError, match=message):
        TcpPrinter("127.0.0.1", fake.port).send(b"BON")
    fake.close()
    assert fake.received == b""


def test_netzwerkdrucker_ohne_statusantwort_druckt_trotzdem():
    fake = FakeNetPrinter(status=None, paper=None)
    TcpPrinter("127.0.0.1", fake.port, timeout=0.3).send(b"BON")
    fake.close()
    assert fake.received.endswith(b"BON")


def test_netzwerkdrucker_nicht_erreichbar():
    free = socket.create_server(("127.0.0.1", 0))
    port = free.getsockname()[1]
    free.close()
    with pytest.raises(PrinterError, match="nicht erreichbar"):
        TcpPrinter("127.0.0.1", port, timeout=0.5).send(b"BON")


class FakeWin32Print:
    """Genug von pywin32, um die Warteschlange nachzuspielen."""

    def __init__(self, status=0, attributes=0, jobs_after_write=()):
        self.status, self.attributes = status, attributes
        self.jobs_after_write = list(jobs_after_write)
        self.calls: list[str] = []
        self.data = b""

    def OpenPrinter(self, name):
        self.calls.append(f"open {name}")
        return "h"

    def GetPrinter(self, handle, level):
        return {"Status": self.status, "Attributes": self.attributes}

    def StartDocPrinter(self, handle, level, info):
        assert info[2] == "RAW"
        return 7

    def StartPagePrinter(self, handle):
        pass

    def WritePrinter(self, handle, data):
        self.data += data

    def EndPagePrinter(self, handle):
        pass

    def EndDocPrinter(self, handle):
        self.calls.append("end")

    def EnumJobs(self, handle, first, count, level):
        return [self.jobs_after_write.pop(0)] if self.jobs_after_write else []

    def SetJob(self, handle, job_id, level, info, command):
        self.calls.append(f"delete {job_id}")

    def ClosePrinter(self, handle):
        self.calls.append("close")


def test_windows_warteschlange_druckt_roh():
    api = FakeWin32Print(jobs_after_write=[{"JobId": 7, "Status": 0x10}])
    WindowsPrinter("EPSON TM-T20II Küche", api=api, sleep=lambda _s: None).send(b"BON")
    assert api.data == b"BON"
    assert api.calls == ["open EPSON TM-T20II Küche", "end", "close"]


def test_windows_drucker_aus_wird_gemeldet():
    api = FakeWin32Print(status=0x80)
    with pytest.raises(PrinterError, match="offline"):
        WindowsPrinter("Kueche", api=api).send(b"BON")
    assert api.data == b""
    assert api.calls[-1] == "close"


def test_windows_haengender_auftrag_wird_geloescht():
    # Die Warteschlange nimmt an, der Drucker druckt nie: nach der Wartezeit weg damit,
    # sonst kaeme er spaeter zusaetzlich zur Wiederholung aus dem Drucker.
    api = FakeWin32Print(jobs_after_write=[{"JobId": 7, "Status": 0}] * 10)
    printer = WindowsPrinter("Kueche", api=api, wait_seconds=1.0, sleep=lambda _s: None)
    with pytest.raises(PrinterError, match="haengt"):
        printer.send(b"BON")
    assert "delete 7" in api.calls


def test_druckerangabe():
    assert isinstance(from_spec("tcp:192.168.1.50"), TcpPrinter)
    assert from_spec("tcp:192.168.1.50:9101").port == 9101
    assert from_spec("windows:EPSON TM-T20II Küche").name == "EPSON TM-T20II Küche"
    with pytest.raises(ValueError):
        from_spec("usb")


# --- Protokoll --------------------------------------------------------------------


def test_protokoll_ueberlebt_neustart_und_vergisst_die_aeltesten(tmp_path):
    path = tmp_path / "state.json"
    log = PrintedLog(path, keep=2)
    log.record("a", 0)
    log.record("b", 1)
    log.record("c", 0)
    again = PrintedLog(path, keep=2)
    assert again.already("b", 1) and again.already("b", 0)
    assert not again.already("b", 2)
    assert not again.already("a", 0)
    assert "Müller" not in path.read_text(encoding="utf-8")


def test_kaputtes_protokoll_heisst_neu_anfangen(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{kaputt", encoding="utf-8")
    assert not PrintedLog(path).already("a", 0)


# --- Server-Verbindung ------------------------------------------------------------


def test_token_nie_ueber_http_ins_netz():
    with pytest.raises(ValueError, match="https"):
        Server("http://agent.example.com", "t", "x")
    Server("http://localhost:8000", "t", "x")
    with pytest.raises(ValueError, match="Token"):
        Server("https://agent.example.com", "", "x")


# --- Durchlauf gegen den echten Server --------------------------------------------


class _Response:
    def __init__(self, body: bytes):
        self.body = body

    def read(self) -> bytes:
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakePrinter:
    def __init__(self, fail: str | None = None):
        self.fail = fail
        self.bons: list[bytes] = []

    def send(self, data: bytes) -> None:
        if self.fail:
            raise PrinterError(self.fail)
        self.bons.append(data)


@dataclass
class Setup:
    server: Server
    opener: object
    tenant_id: str
    engine: object
    order_id: uuid.UUID

    def handover(self) -> str | None:
        with Session(self.engine) as s:
            return s.scalar(
                select(Order.handover_state).where(Order.id == self.order_id)
            )

    def expire_lease(self) -> None:
        """Leihfrist vorbei: der Bon ist wieder faellig."""
        with Session(self.engine) as s:
            s.execute(
                update(OutboxEvent)
                .where(OutboxEvent.status == "pending")
                .values(next_attempt_at=datetime.now(UTC) - timedelta(seconds=1))
            )
            s.commit()


@pytest.fixture
def setup(migrated_db_url, monkeypatch):
    engine = create_engine(migrated_db_url)
    with Session(engine) as s:
        tenant_id = _tenant(s, "Testbetrieb")
        _mode(s, tenant_id, "primary")
        call_id = _call(s, tenant_id)
        order_id = _draft(s, tenant_id, call_id)
        _confirm(s, tenant_id, call_id, order_id)

    def override_get_db():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    monkeypatch.setattr(settings, "kitchen_bridge_token", "bruecke-geheim")
    http = TestClient(app)

    def opener(request, timeout):
        response = http.post(
            urlsplit(request.full_url).path,
            content=request.data,
            headers=dict(request.header_items()),
        )
        return _Response(response.content)

    server = Server("http://localhost", "bruecke-geheim", str(tenant_id), opener=opener)
    try:
        yield Setup(server, opener, str(tenant_id), engine, order_id)
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def test_durchlauf_druckt_und_die_karte_wird_normal(setup, tmp_path):
    printer = FakePrinter()
    log = PrintedLog(tmp_path / "state.json")
    assert run_once(setup.server, printer, log) == 1
    assert b"ABHOLUNG A" in printer.bons[0]
    assert setup.handover() == "sent"
    assert run_once(setup.server, printer, log) == 0


def test_druckfehler_macht_die_karte_rot(setup, tmp_path):
    printer = FakePrinter(fail="Papier leer")
    assert run_once(setup.server, printer, PrintedLog(tmp_path / "s")) == 0
    assert setup.handover() == "failed"


def test_verlorene_rueckmeldung_druckt_nicht_zweimal(setup, tmp_path):
    printer = FakePrinter()
    log = PrintedLog(tmp_path / "state.json")

    class LostAck(Server):
        def ack(self, event_id, ok, error=None):
            raise ServerError("Netz weg")

    lossy = LostAck(
        "http://localhost", "bruecke-geheim", setup.tenant_id, opener=setup.opener
    )
    with pytest.raises(ServerError):
        run_once(lossy, printer, log)
    assert len(printer.bons) == 1
    assert setup.handover() == "pending"

    # Nach der Leihfrist kommt derselbe Bon wieder: erkannt, kein zweiter Zettel.
    setup.expire_lease()
    assert run_once(setup.server, printer, log) == 0
    assert len(printer.bons) == 1
    assert setup.handover() == "sent"


def test_falsches_token_wird_gemeldet(setup):
    wrong = Server("http://localhost", "falsch", setup.tenant_id, opener=setup.opener)
    with pytest.raises(ServerError, match="unauthorized"):
        wrong.claim()


def test_probebon_ohne_server(monkeypatch):
    sent: list[bytes] = []

    class Capture:
        def send(self, data):
            sent.append(data)

    monkeypatch.setenv("MAEX_PRINTER", "tcp:127.0.0.1")
    monkeypatch.setattr("printbridge.bridge.from_spec", lambda spec: Capture())
    assert main(["--test"]) == 0
    assert "Probebon Umlaute äöü ß" in sent[0].decode("cp858")
    assert SAMPLE["customer_name"] in sent[0].decode("cp858")
    assert json.dumps(SAMPLE)  # Probebon ist reines JSON wie ein echter Bon
    assert uuid.UUID(SAMPLE["order_id"])


def test_gescheiterte_meldung_haelt_die_uebrigen_bons_nicht_auf(tmp_path):
    tickets = [
        {"id": f"e{n}", "ticket": {**TICKET, "order_id": f"o{n}"}} for n in range(3)
    ]

    acked: list[str] = []

    class Flaky:
        def claim(self):
            return tickets

        def ack(self, event_id, ok, error=None):
            if event_id == "e0":
                raise ServerError("Netz kurz weg")
            acked.append(event_id)

    printer = FakePrinter()
    with pytest.raises(ServerError):
        run_once(Flaky(), printer, PrintedLog(tmp_path / "s"))
    assert len(printer.bons) == 3
    assert acked == ["e1", "e2"]
