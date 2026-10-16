"""Weg zum Drucker: Netzwerkdrucker (TCP 9100) oder Windows-Warteschlange (USB am Kassenrechner).

`from_spec` liest die Angabe aus der Umgebung:
- `tcp:192.168.1.50` oder `tcp:192.168.1.50:9100`
- `windows:EPSON TM-T20II Küche` (Name wie in "Drucker und Scanner")

Beide Wege pruefen vor dem Druck, ob der Drucker bereit ist, und werfen sonst:
die Bruecke meldet den Fehler, und die Karte im Tablet wird rot, statt dass ein
Bon still im Papierfach eines ausgeschalteten Druckers verschwindet.
"""

import contextlib
import importlib
import socket
import time
from typing import Any, Protocol

DLE_EOT = b"\x10\x04"


class PrinterError(Exception):
    """Drucker nicht bereit oder Druck fehlgeschlagen. Die Meldung geht ins Tablet-Log."""


class Printer(Protocol):
    def send(self, data: bytes) -> None: ...


class TcpPrinter:
    """Epson mit Netzwerkkarte, roher Druck auf Port 9100."""

    def __init__(self, host: str, port: int = 9100, timeout: float = 5.0):
        self.host, self.port, self.timeout = host, port, timeout

    def _status(self, conn: socket.socket, n: int) -> int | None:
        """Echtzeit-Status (DLE EOT n). None, wenn der Drucker nicht antwortet."""
        conn.sendall(DLE_EOT + bytes([n]))
        try:
            reply = conn.recv(1)
        except TimeoutError:
            return None
        return reply[0] if reply else None

    def send(self, data: bytes) -> None:
        try:
            with socket.create_connection((self.host, self.port), self.timeout) as conn:
                conn.settimeout(min(self.timeout, 2.0))
                printer = self._status(conn, 1)
                if printer is not None and printer & 0x08:
                    raise PrinterError("Drucker offline (Deckel offen oder Fehler)")
                paper = self._status(conn, 4)
                if paper is not None and paper & 0x60 == 0x60:
                    raise PrinterError("Papier leer")
                conn.settimeout(self.timeout)
                conn.sendall(data)
        except OSError as exc:
            raise PrinterError(
                f"Drucker {self.host}:{self.port} nicht erreichbar: {exc}"
            ) from exc


# Aus der Windows-API (winspool): Status-Bits, bei denen nichts gedruckt wird.
PRINTER_STATUS_BLOCKING = {
    0x00000002: "Fehler",
    0x00000008: "Papierstau",
    0x00000010: "Papier leer",
    0x00000080: "offline",
    0x00400000: "Tuer offen",
}
PRINTER_ATTRIBUTE_WORK_OFFLINE = 0x00000400
# Fehler, offline, Papier leer, Geraetewarteschlange blockiert, Eingriff noetig
JOB_STATUS_ERROR = 0x00000002 | 0x00000020 | 0x00000040 | 0x00000200 | 0x00000400
JOB_STATUS_PRINTED = 0x00000080
JOB_CONTROL_DELETE = 5


class WindowsPrinter:
    """Drucker in der Windows-Warteschlange, roh (RAW) ueber pywin32.

    Die Warteschlange nimmt einen Auftrag auch an, wenn der Drucker aus ist. Darum
    wartet `send`, bis der Auftrag die Warteschlange verlassen hat. Haengt er fest,
    wird er geloescht und der Fehler gemeldet - sonst druckte er spaeter noch
    einmal, nachdem die Bruecke ihn schon wiederholt hat.
    """

    def __init__(
        self,
        name: str,
        api: Any = None,
        wait_seconds: float = 15.0,
        sleep: Any = time.sleep,
    ):
        self.name = name
        self._api = api
        self.wait_seconds = wait_seconds
        self.sleep = sleep

    @property
    def api(self) -> Any:
        if self._api is None:
            try:
                self._api = importlib.import_module("win32print")
            except ImportError as exc:
                raise PrinterError("pywin32 fehlt: pip install pywin32") from exc
        return self._api

    def _check_ready(self, handle: Any) -> None:
        info = self.api.GetPrinter(handle, 2)
        if info.get("Attributes", 0) & PRINTER_ATTRIBUTE_WORK_OFFLINE:
            raise PrinterError(f"{self.name}: offline geschaltet")
        status = info.get("Status", 0)
        for bit, text in PRINTER_STATUS_BLOCKING.items():
            if status & bit:
                raise PrinterError(f"{self.name}: {text}")

    def _wait_done(self, handle: Any, job_id: int) -> None:
        waited = 0.0
        while True:
            jobs = {j["JobId"]: j for j in self.api.EnumJobs(handle, 0, 999, 1)}
            job = jobs.get(job_id)
            if job is None or job.get("Status", 0) & JOB_STATUS_PRINTED:
                return  # Auftrag hat die Warteschlange verlassen: gedruckt
            if job.get("Status", 0) & JOB_STATUS_ERROR or waited >= self.wait_seconds:
                self.api.SetJob(handle, job_id, 0, None, JOB_CONTROL_DELETE)
                raise PrinterError(f"{self.name}: Auftrag haengt in der Warteschlange")
            self.sleep(0.5)
            waited += 0.5

    def send(self, data: bytes) -> None:
        api = self.api
        try:
            handle = api.OpenPrinter(self.name)
        except Exception as exc:
            raise PrinterError(f"{self.name}: nicht gefunden ({exc})") from exc
        job_id = None
        try:
            self._check_ready(handle)
            job_id = api.StartDocPrinter(handle, 1, ("Maex Kuechenbon", None, "RAW"))
            try:
                api.StartPagePrinter(handle)
                api.WritePrinter(handle, data)
                api.EndPagePrinter(handle)
            finally:
                api.EndDocPrinter(handle)
            self._wait_done(handle, job_id)
        except PrinterError:
            raise
        except Exception as exc:
            # Der Auftrag steht schon in der Warteschlange: loeschen, sonst druckt
            # Windows ihn spaeter noch, zusaetzlich zur Wiederholung (Codex PR #143).
            if job_id is not None:
                self._cancel(handle, job_id)
            raise PrinterError(f"{self.name}: {exc}") from exc
        finally:
            api.ClosePrinter(handle)

    def _cancel(self, handle: Any, job_id: int) -> None:
        # Schon weg ist auch gut: dann druckt ihn Windows ebenfalls nicht mehr.
        with contextlib.suppress(Exception):
            self.api.SetJob(handle, job_id, 0, None, JOB_CONTROL_DELETE)


def from_spec(spec: str) -> Printer:
    kind, _, rest = spec.partition(":")
    if kind == "tcp" and rest:
        host, _, port = rest.partition(":")
        return TcpPrinter(host, int(port) if port else 9100)
    if kind == "windows" and rest:
        return WindowsPrinter(rest)
    raise ValueError(
        f"Druckerangabe unbekannt: {spec!r} (tcp:<host>[:port] oder windows:<Name>)"
    )
