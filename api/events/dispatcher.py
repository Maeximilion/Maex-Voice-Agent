"""Dispatcher: leert die Outbox Richtung n8n (docs/11 §events, docs/03 §outbox).

Eigener Prozess, nie im heissen Pfad: `python -m api.jobs.cold_path` (mit dem
Waechter fuer den Kuechenbon) oder allein `python -m api.events.dispatcher`. Faellt n8n
aus, bleibt der Vorgang gebucht und das Ereignis wartet. Jeder Versand traegt die
Ereignis-id als Idempotenz-Schluessel, damit ein wiederholter Zustellversuch in n8n
nicht zu einem zweiten Vorgang fuehrt.

Den Kuechenbon (`order.confirmed`) stellt nicht der Dispatcher zu, sondern die
Druckbruecke im Restaurant holt ihn ab (domain/ordering/handover.py, T-4.6).
"""

import logging
import signal
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from types import FrameType
from typing import Protocol

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.config import settings
from api.core.logging import configure_logging, get_logger, log
from api.db import SessionLocal
from api.events.outbox import (
    BACKOFF_SECONDS,
    MAX_ATTEMPTS,
    mark_attempt_failed,
    mark_sent,
)
from api.events.types import KITCHEN
from api.models import OutboxEvent

logger = get_logger("api.events.dispatcher")

__all__ = ["BACKOFF_SECONDS", "MAX_ATTEMPTS", "dispatch_once", "send_to_n8n"]


class Sender(Protocol):
    """Zustellweg. Wirft bei Misserfolg; der Rueckgabewert traegt keine Information."""

    def __call__(self, event: OutboxEvent) -> None: ...


@dataclass
class Result:
    sent: int = 0
    retried: int = 0
    failed: int = 0

    @property
    def handled(self) -> int:
        return self.sent + self.retried + self.failed


def send_to_n8n(event: OutboxEvent, client: httpx.Client | None = None) -> None:
    """Standard-Zustellweg: ein POST auf den n8n-Webhook, 4xx und 5xx werfen."""
    auth = None
    if settings.n8n_basic_auth_user:
        auth = (settings.n8n_basic_auth_user, settings.n8n_basic_auth_password)
    poster = client.post if client is not None else httpx.post
    response = poster(
        settings.n8n_webhook_url,
        json={
            "id": str(event.id),
            "tenant_id": str(event.tenant_id),
            "event_type": event.event_type,
            "payload": event.payload,
        },
        # n8n wertet den Schluessel aus, um denselben Vorgang nicht zweimal zu verarbeiten.
        headers={"X-Idempotency-Key": str(event.id)},
        auth=auth,
        timeout=settings.n8n_timeout_seconds,
    )
    response.raise_for_status()


def _claim_due(session: Session, now: datetime) -> OutboxEvent | None:
    """Faelliges Ereignis mit Zeilensperre holen.

    SKIP LOCKED statt Warten: laufen zwei Dispatcher, nimmt jeder andere Zeilen,
    statt sich gegenseitig zu blockieren oder dasselbe Ereignis doppelt zu senden.
    """
    return session.execute(
        select(OutboxEvent)
        .where(
            OutboxEvent.status == "pending",
            OutboxEvent.next_attempt_at <= now,
            OutboxEvent.event_type.not_in(KITCHEN),
        )
        .order_by(OutboxEvent.next_attempt_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    ).scalar_one_or_none()


def _on_failure(
    event: OutboxEvent, exc: Exception, now: datetime, result: Result
) -> None:
    if mark_attempt_failed(event, f"{type(exc).__name__}: {exc}", now):
        result.failed += 1
        # Alarm: ab hier holt das niemand mehr nach, ein Mensch muss ran (docs/02 §Ausfall).
        log(
            logger,
            logging.ERROR,
            "Alarm: Outbox-Ereignis endgueltig fehlgeschlagen",
            event_id=str(event.id),
            event_type=event.event_type,
            attempts=event.attempts,
            last_error=event.last_error,
        )
        return
    result.retried += 1
    log(
        logger,
        logging.WARNING,
        "Zustellung fehlgeschlagen, neuer Versuch geplant",
        event_id=str(event.id),
        event_type=event.event_type,
        attempts=event.attempts,
        next_attempt_at=event.next_attempt_at.isoformat(),
        last_error=event.last_error,
    )


def dispatch_once(
    session: Session,
    send: Sender = send_to_n8n,
    *,
    limit: int | None = None,
    now: datetime | None = None,
) -> Result:
    """Arbeitet bis zu `limit` faellige Ereignisse ab, jedes in einer eigenen Transaktion."""
    result = Result()
    limit = settings.dispatcher_batch if limit is None else limit
    for _ in range(limit):
        moment = now or datetime.now(UTC)
        event = _claim_due(session, moment)
        if event is None:
            session.commit()
            break
        try:
            send(event)
        except Exception as exc:  # noqa: BLE001 - jeder Zustellfehler fuehrt zum Backoff
            _on_failure(event, exc, moment, result)
        else:
            mark_sent(event, moment)
            result.sent += 1
        # Commit gibt die Zeilensperre frei; der naechste Durchlauf sieht den neuen Stand.
        session.commit()
    return result


def _pass(
    session: Session,
    send: Sender,
    tick: Callable[[Session, datetime], object] | None,
) -> None:
    """Ein Durchlauf: erst versenden, dann der Waechter. Keiner reisst den anderen mit."""
    try:
        result = dispatch_once(session, send)
        if result.handled:
            log(
                logger,
                logging.INFO,
                "Outbox abgearbeitet",
                sent=result.sent,
                retried=result.retried,
                failed=result.failed,
            )
    except Exception as exc:  # noqa: BLE001 - der Dispatcher darf nie sterben
        session.rollback()
        log(logger, logging.ERROR, "Dispatcher-Durchlauf abgebrochen", error=str(exc))
    if tick is None:
        return
    try:
        tick(session, datetime.now(UTC))
    except Exception as exc:  # noqa: BLE001 - der Dispatcher darf nie sterben
        session.rollback()
        log(logger, logging.ERROR, "Waechter abgebrochen", error=str(exc))


def run_forever(
    send: Sender = send_to_n8n,
    tick: Callable[[Session, datetime], object] | None = None,
) -> None:
    """Endlosschleife fuer den Containerbetrieb. SIGTERM beendet nach dem laufenden Ereignis.

    `tick` laeuft je Durchlauf nach dem Versand, in eigener Fehlerbehandlung:
    der Waechter fuer den Kuechenbon (api/jobs/cold_path.py) haengt hier.
    """
    stop = False

    def _stop(_signum: int, _frame: FrameType | None) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    log(
        logger,
        logging.INFO,
        "Dispatcher gestartet",
        interval=settings.dispatcher_interval_seconds,
    )
    while not stop:
        session = SessionLocal()
        try:
            _pass(session, send, tick)
        finally:
            session.close()
        time.sleep(settings.dispatcher_interval_seconds)
    log(logger, logging.INFO, "Dispatcher beendet")


if __name__ == "__main__":
    configure_logging(settings.log_level)
    run_forever()
