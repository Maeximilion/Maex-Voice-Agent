"""Prozess des kalten Pfads: Dispatcher Richtung n8n plus Waechter fuer den Kuechenbon.

`python -m api.jobs.cold_path` (docker-compose, Dienst `dispatcher`). Der Waechter
gehoert zur Fachlogik (domain/ordering/handover.py), der Dispatcher zu events/;
hier werden beide zusammengesteckt, damit events/ die Fachlogik nicht kennen muss.

Der Waechter laeuft in einem eigenen Faden mit eigenem Takt. Haengt n8n, braucht
ein Durchlauf des Dispatchers bis zu 20 x 10 s Timeout - im selben Takt waere
die Karte genau waehrend eines zweiten Ausfalls minutenlang gruen geblieben
(Codex PR #143).
"""

import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy.orm import Session

from api.config import settings
from api.core.logging import configure_logging, get_logger, log
from api.db import SessionLocal
from api.domain.ordering.handover import sweep
from api.events.dispatcher import run_forever, send_to_n8n

logger = get_logger("api.jobs.cold_path")


class Stop(Protocol):
    def is_set(self) -> bool: ...

    def wait(self, timeout: float | None = None) -> bool: ...


def watch_forever(
    stop: Stop,
    interval: float,
    session_factory: Callable[[], Session] = SessionLocal,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> None:
    """Waechter-Schleife. Ein Fehler beendet nur den Durchlauf, nie den Faden."""
    while not stop.is_set():
        session = session_factory()
        try:
            sweep(session, clock())
        except Exception as exc:  # noqa: BLE001 - der Waechter darf nie sterben
            session.rollback()
            log(logger, logging.ERROR, "Waechter abgebrochen", error=str(exc))
        finally:
            session.close()
        stop.wait(interval)


if __name__ == "__main__":
    configure_logging(settings.log_level)
    threading.Thread(
        target=watch_forever,
        args=(threading.Event(), settings.dispatcher_interval_seconds),
        name="kitchen-watch",
        daemon=True,
    ).start()
    run_forever(send_to_n8n)
