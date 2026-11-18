"""The push to the operator's `ops` topic when a way into the dashboard
changes (docs/23 §5): a new or removed passkey, a new or removed TOTP secret,
a used recovery code, a passkey's copy, and on the host a new account and the
break glass. A takeover must not be silent.

The outbox pattern of a customer's stack (`api/events/`), in the operator
database (#827 F3): the request only writes a row (`queue`), and the sender,
a thread of the dashboard's own process, posts it to the push server later
(`deliver_once`) the way a stack's monitor publishes (`api/jobs/monitor.py`).
A push server that is down or refuses never blocks the request and never
loses the push: the row waits with growing pauses, up to ten minutes, for as
long as it takes. The push server is the operator stack's own `push`
(`deploy/docker-compose.operator.yml`). Outside dev and test the dashboard
does not start without `OPERATOR_PUSH_URL` and `OPERATOR_PUSH_TOKEN`
(`config.py`); in dev and test the rows then wait and no sender runs.
"""

import logging
import threading
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from api.core.logging import error_text, get_logger, log
from api.dashboard.config import settings
from api.dashboard.db import SessionLocal
from api.dashboard.models import OperatorPush

logger = get_logger("api.dashboard.push")

TOPIC = "ops"
TITLE = "Operator sign-in changed"
# ntfy's "high": the phone makes itself heard.
PRIORITY = 4
TIMEOUT_SECONDS = 3.0
# How often the sender looks for a due push, and how many it sends at most in
# one round; a round sends one after the other, so one slow answer delays the
# rest by its timeout at most.
INTERVAL_SECONDS = 5.0
BATCH = 20
# The waits and the cut error text of a customer's outbox (`api/events/
# outbox.py`, a test keeps them equal), copied: importing that module would
# load every customer table into the dashboard.
BACKOFF_SECONDS = (5, 30, 120, 600)
LAST_ERROR_MAX = 500


def queue(db: Session, text: str, at: datetime) -> None:
    """Hold one push for the sender, due at once. Commits: the change it
    reports is committed already, and its audit row with it."""
    db.add(OperatorPush(message=text, next_attempt_at=at))
    db.commit()


def configured() -> bool:
    return bool(settings.operator_push_url and settings.operator_push_token)


def _send(message: str) -> None:
    """One POST to the push server; raises when it refuses or does not answer."""
    response = httpx.post(
        settings.operator_push_url,
        json={"topic": TOPIC, "title": TITLE, "message": message, "priority": PRIORITY},
        headers={"Authorization": f"Bearer {settings.operator_push_token}"},
        timeout=TIMEOUT_SECONDS,
        trust_env=False,
    )
    response.raise_for_status()


def _claim_due(db: Session, now: datetime) -> OperatorPush | None:
    # SKIP LOCKED as the dispatcher's: a second process never sends a row twice.
    return db.execute(
        select(OperatorPush)
        .where(OperatorPush.sent_at.is_(None), OperatorPush.next_attempt_at <= now)
        .order_by(OperatorPush.next_attempt_at, OperatorPush.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    ).scalar_one_or_none()


def deliver_once(db: Session, now: datetime) -> int:
    """Send what is due, oldest first, each row in its own transaction. How
    many the push server took. Nothing is claimed without a push server."""
    if not configured():
        return 0
    sent = 0
    for _ in range(BATCH):
        row = _claim_due(db, now)
        if row is None:
            break
        row.attempts += 1
        try:
            _send(row.message)
        except Exception as problem:  # noqa: BLE001  every failure waits and retries
            # After the last step of the ladder it stays at the longest wait.
            wait = BACKOFF_SECONDS[min(row.attempts, len(BACKOFF_SECONDS)) - 1]
            row.next_attempt_at = now + timedelta(seconds=wait)
            row.last_error = error_text(problem)[:LAST_ERROR_MAX]
            log(
                logger,
                logging.WARNING,
                "Operator push not sent, retried later",
                push_id=row.id,
                attempts=row.attempts,
                error=row.last_error,
            )
            # The server is down or refuses: the next round tries again,
            # instead of every due row waiting out its own timeout now.
            db.commit()
            break
        row.sent_at = now
        row.last_error = None
        sent += 1
        db.commit()
    db.commit()
    return sent


def stuck(db: Session) -> int:
    """Pushes that failed past the whole ladder of waits (about 13 minutes of
    trying): a wrong token or a push server that is gone, which otherwise
    shows only as warnings in the journal. The home page says so (#853)."""
    return (
        db.scalar(
            select(func.count())
            .select_from(OperatorPush)
            .where(
                OperatorPush.sent_at.is_(None),
                OperatorPush.attempts > len(BACKOFF_SECONDS),
            )
        )
        or 0
    )


def _run(stop: threading.Event) -> None:
    while not stop.wait(INTERVAL_SECONDS):
        try:
            with SessionLocal() as db:
                deliver_once(db, datetime.now(UTC))
        except Exception as problem:  # noqa: BLE001  the sender outlives a bad round
            log(
                logger,
                logging.ERROR,
                "Operator push round failed",
                error=error_text(problem),
            )


def start() -> threading.Event | None:
    """Start the sender beside the dashboard; the event stops it. None, and a
    warning, when no push server is set: the pushes then wait in the table."""
    if not configured():
        log(logger, logging.WARNING, "No operator push server, pushes wait")
        return None
    stop = threading.Event()
    threading.Thread(
        target=_run, args=(stop,), name="operator-push", daemon=True
    ).start()
    return stop
