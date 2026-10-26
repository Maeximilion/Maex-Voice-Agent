"""Cold-path process: the dispatcher towards n8n, the watchdog for the kitchen slip
and the deletion job (api/jobs/retention.py).

`python -m api.jobs.cold_path` (docker-compose, service `dispatcher`). The watchdog
belongs to the domain logic (domain/ordering/handover.py), the dispatcher to events/;
both are wired together here so that events/ does not have to know the domain logic.

The watchdog runs in its own thread with its own cadence. If n8n hangs, one pass of
the dispatcher takes up to 20 x 10 s of timeout - on the same cadence the card would
have stayed green for minutes during exactly a second outage (Codex PR #143).
The deletion job has a thread of its own for the same reason.
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
from api.jobs.retention import purge

logger = get_logger("api.jobs.cold_path")


class Stop(Protocol):
    def is_set(self) -> bool: ...

    def wait(self, timeout: float | None = None) -> bool: ...


def watch_forever(
    stop: Stop,
    interval: float,
    session_factory: Callable[[], Session] = SessionLocal,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    job: Callable[[Session, datetime], object] = sweep,
) -> None:
    """Loop of the watchdog or the deletion job. An error ends only the pass,
    never the thread."""
    while not stop.is_set():
        session = session_factory()
        try:
            job(session, clock())
        except Exception as exc:  # noqa: BLE001 - the watchdog must never die
            session.rollback()
            log(
                logger,
                logging.ERROR,
                "Watchdog pass aborted",
                job=getattr(job, "__name__", str(job)),
                error=str(exc),
            )
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
    threading.Thread(
        target=watch_forever,
        args=(threading.Event(), settings.retention_interval_seconds),
        kwargs={"job": purge},
        name="retention",
        daemon=True,
    ).start()
    run_forever(send_to_n8n)
