"""Ereignisstrom der Betriebsansicht (docs/11 §gui, docs/06 §1 Regel 5: live, ohne Nachladen).

Der Strom schickt keine Daten, nur ein Signal: "an der Spalte Heute hat sich etwas
geändert". Die Seite holt das Fragment danach selbst. Das hält die Nutzlast klein
und die Darstellung an genau einer Stelle - im Jinja2-Template.

Warum abfragen statt LISTEN/NOTIFY: Reservierungen entstehen nicht nur in diesem
Prozess. sim/ schreibt aus dem Terminal, der Dispatcher läuft eigenständig, später
kommt die Telefonie dazu. Ein prozessinterner Kanal würde diese Buchungen nie
sehen. Die Abfrage ist eine Zeile Aggregat auf einem Index (docs/03
ix_reservations_tenant_reserved_for), das trägt eine Handvoll Tablets.
"""

import asyncio
import time
import uuid
from collections.abc import AsyncIterator

from fastapi import Request
from sqlalchemy.exc import SQLAlchemyError

from api.core.logging import get_logger
from api.db import SessionLocal
from api.domain.reservations import today_change_token

logger = get_logger("api.gui.sse")

POLL_SECONDS = 2.0
# Ohne Lebenszeichen schliesst ein Reverse-Proxy den Strom nach einer stillen
# Minute; der Browser verbindet dann neu und die GUI blinkt (docs/13 §Caddy).
HEARTBEAT_SECONDS = 15.0
# Der Browser wartet nach einem Abbruch so lange, bevor er neu verbindet.
RETRY_MS = 3000


def _token(tenant_id: uuid.UUID, tz_name: str) -> str:
    session = SessionLocal()
    try:
        return today_change_token(session, tenant_id, tz_name)
    finally:
        session.close()


async def today_event_stream(
    request: Request,
    tenant_id: uuid.UUID,
    tz_name: str,
    poll_seconds: float = POLL_SECONDS,
    heartbeat_seconds: float = HEARTBEAT_SECONDS,
    max_ticks: int | None = None,
) -> AsyncIterator[str]:
    """Server-Sent-Events. `max_ticks` begrenzt die Schleife in Tests."""
    yield f"retry: {RETRY_MS}\n\n"
    last_token: str | None = None
    last_send = time.monotonic()
    # Nach einem Aussetzer haengt die gelbe Leiste im Browser fest, bis ein
    # Ereignis kommt. Ohne dieses Merkmal waere das erst die naechste echte
    # Aenderung - der Betrieb saehe eine Stoerung, die laengst vorbei ist.
    failed = False
    ticks = 0
    while max_ticks is None or ticks < max_ticks:
        ticks += 1
        if await request.is_disconnected():
            return
        try:
            token = await asyncio.to_thread(_token, tenant_id, tz_name)
        except SQLAlchemyError as exc:
            # Kein Abbruch: die Seite zeigt die gelbe Leiste (docs/06 §5) und der
            # naechste Durchlauf holt sie wieder weg, ohne Neuverbindung.
            logger.warning("Datenbank fuer den Ereignisstrom nicht erreichbar: %s", exc)
            failed = True
            last_send = time.monotonic()
            yield "event: problem\ndata: db\n\n"
        else:
            if token != last_token or failed:
                last_token = token
                failed = False
                last_send = time.monotonic()
                # Beim Verbinden einmal senden: was waehrend einer Trennung
                # gebucht wurde, ist damit sofort auf dem Tablet.
                yield f"event: today\ndata: {token}\n\n"
            elif time.monotonic() - last_send >= heartbeat_seconds:
                last_send = time.monotonic()
                yield ": keepalive\n\n"
        await asyncio.sleep(poll_seconds)
