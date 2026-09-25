"""Die Druckbruecke: Bons abholen, drucken, zurueckmelden (T-4.6).

    python -m printbridge            # Dauerbetrieb
    python -m printbridge --once     # ein Durchlauf, zum Ausprobieren
    python -m printbridge --test     # Probebon ohne Server, nur mit Drucker

Einstellungen aus der Umgebung (printbridge/README.md):
MAEX_SERVER_URL, MAEX_KITCHEN_TOKEN, MAEX_TENANT_ID, MAEX_PRINTER,
MAEX_PRINTER_WIDTH (48), MAEX_STATE_FILE, MAEX_INTERVAL_SECONDS (2).
"""

import argparse
import logging
import os
import sys
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from printbridge.client import Server, ServerError
from printbridge.escpos import DEFAULT_WIDTH, local_now, render
from printbridge.state import PrintedLog
from printbridge.transport import Printer, from_spec

logger = logging.getLogger("printbridge")

SAMPLE = {
    "order_id": "00000000-0000-0000-0000-000000000000",
    "type": "pickup",
    "pickup_code": "A0",
    "customer_name": "Probedruck",
    "revision": 0,
    "correction_reason": None,
    "total_cents": 1250,
    "items": [
        {
            "number": "0",
            "name": "Probebon Umlaute äöü ß",
            "quantity": 1,
            "options": [],
            "note": "Kein echter Auftrag",
        }
    ],
}


def run_once(
    server: Server,
    printer: Printer,
    log: PrintedLog,
    width: int = DEFAULT_WIDTH,
    clock: Callable[[], datetime] = local_now,
) -> int:
    """Ein Durchlauf. Liefert die Zahl der gedruckten Bons.

    Erst drucken, dann ins Protokoll, dann zurueckmelden: faellt die Meldung aus,
    kommt der Bon nach der Leihfrist wieder und wird am Protokoll erkannt. Eine
    gescheiterte Meldung haelt die uebrigen Bons nicht auf - die Kueche bekommt
    alles, was schon abgeholt ist; der Fehler kommt am Ende.
    """
    printed = 0
    lost: list[ServerError] = []

    def ack(event_id: str, ok: bool, error: str | None = None) -> None:
        try:
            server.ack(event_id, ok, error)
        except ServerError as exc:
            logger.error("Rueckmeldung fuer %s fehlgeschlagen: %s", event_id, exc)
            lost.append(exc)

    for claimed in server.claim():
        ticket: dict[str, Any] = claimed["ticket"]
        order_id = str(ticket["order_id"])
        revision = int(ticket.get("revision") or 0)
        if log.already(order_id, revision):
            logger.info("Bon %s schon gedruckt, kein zweiter Zettel", claimed["id"])
            ack(claimed["id"], True)
            continue
        try:
            view = {
                **ticket,
                "ready_time": claimed.get("ready_time"),
                "print_time": claimed.get("print_time"),
            }
            printer.send(render(view, width, clock()))
        except Exception as exc:  # noqa: BLE001 - jeder Druckfehler wird gemeldet
            logger.error("Druck fehlgeschlagen: %s", exc)
            ack(claimed["id"], False, str(exc))
            continue
        try:
            log.record(order_id, revision)
        except OSError as exc:
            # Gedruckt ist gedruckt: trotzdem melden, sonst kaeme der Bon nach
            # der Leihfrist ein zweites Mal aus dem Drucker.
            logger.error("Druckprotokoll nicht geschrieben: %s", exc)
        ack(claimed["id"], True)
        printed += 1
    if lost:
        raise lost[0]
    return printed


def _env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None or value == "":
        raise SystemExit(f"Einstellung fehlt: {name} (siehe printbridge/README.md)")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="printbridge", description=__doc__)
    parser.add_argument("--once", action="store_true", help="ein Durchlauf, dann Ende")
    parser.add_argument("--test", action="store_true", help="Probebon, ohne Server")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )

    printer = from_spec(_env("MAEX_PRINTER"))
    width = int(_env("MAEX_PRINTER_WIDTH", str(DEFAULT_WIDTH)))
    if args.test:
        printer.send(render(SAMPLE, width))
        logger.info("Probebon gesendet")
        return 0

    server = Server(
        _env("MAEX_SERVER_URL"), _env("MAEX_KITCHEN_TOKEN"), _env("MAEX_TENANT_ID")
    )
    log = PrintedLog(Path(_env("MAEX_STATE_FILE", "printbridge_state.json")))
    interval = float(_env("MAEX_INTERVAL_SECONDS", "2"))
    logger.info("Druckbruecke gestartet")
    while True:
        try:
            run_once(server, printer, log, width)
        except Exception as exc:  # noqa: BLE001 - die Bruecke darf nie sterben
            # Server weg: der Waechter auf dem Server macht die Karte rot.
            logger.error("Durchlauf abgebrochen: %s", exc)
        if args.once:
            return 0
        time.sleep(interval)


if __name__ == "__main__":
    sys.exit(main())
