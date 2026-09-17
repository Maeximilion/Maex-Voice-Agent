"""Schreibt jeden `/v1/tools/*`-Aufruf mit Dauer und Ergebnis in `calls.tool_calls`
(docs/03 §calls, docs/04 §Gemeinsame Regeln: "Jeder Aufruf landet mit Dauer und
Ergebnis in calls.tool_calls").

Liest die call_id aus dem Request-Body statt aus dem Logging-Kontext: welche
Middleware zuerst läuft, hängt von der Registrierungsreihenfolge in main.py ab,
der Body dagegen ist unabhängig davon immer da. Best-effort: ein Fehler beim
Schreiben (kaputte call_id, DB kurz weg) darf die Antwort an den Agenten nie
verzögern oder verwerfen.
"""

import contextlib
import json
import logging
import time
from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from sqlalchemy import text
from starlette.concurrency import run_in_threadpool

from api.core.logging import get_logger, log
from api.db import get_db

TOOLS_PREFIX = "/v1/tools/"
# ping traegt keine call_id und ist kein Fachwerkzeug (docs/04 nennt es nicht).
SKIP_NAMES = {"ping"}

logger = get_logger("api.tool_calls")


async def tool_call_log_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    path = request.url.path
    is_tool = (
        path.startswith(TOOLS_PREFIX) and path[len(TOOLS_PREFIX) :] not in SKIP_NAMES
    )
    # Vor call_next lesen: Starlette cacht den Body dabei auf dem Request-Objekt,
    # sodass der Endpunkt ihn danach noch normal parsen kann. Nach call_next ist
    # der Stream bereits verbraucht ("Stream consumed").
    call_id = await _call_id_from_body(request) if is_tool else None
    started = time.perf_counter()
    response = await call_next(request)
    if not is_tool or not call_id:
        return response

    # call_next liefert eine StreamingResponse, die den Inhalt der eigentlichen
    # Antwort erst beim Durchlaufen von body_iterator preisgibt. Einmal gelesen,
    # muss sie durch eine gleichwertige Antwort ersetzt werden, sonst bekommt der
    # Client einen leeren Body.
    body = b"".join([chunk async for chunk in response.body_iterator])
    response = Response(
        content=body,
        status_code=response.status_code,
        headers=dict(response.headers),
        media_type=response.media_type,
    )

    entry = {
        "name": path[len(TOOLS_PREFIX) :],
        "duration_ms": round((time.perf_counter() - started) * 1000, 1),
        **_outcome(body),
    }
    try:
        # _append blockiert synchron auf der DB; im Event-Loop wuerde das jeden
        # anderen gleichzeitigen Tool-Aufruf ausbremsen, deshalb in den Thread-Pool.
        await run_in_threadpool(_append, request, call_id, entry)
    except Exception:  # noqa: BLE001 - Protokoll darf die Antwort nie kippen
        log(logger, logging.WARNING, "tool_calls nicht geschrieben", call_id=call_id)
    return response


async def _call_id_from_body(request: Request) -> str | None:
    try:
        payload = json.loads(await request.body())
    except (ValueError, TypeError):
        return None
    call_id = payload.get("call_id") if isinstance(payload, dict) else None
    return call_id if isinstance(call_id, str) else None


def _outcome(body: bytes) -> dict:
    try:
        parsed = json.loads(body)
    except (ValueError, TypeError):
        return {"ok": False}
    if parsed.get("ok") is True:
        return {"ok": True}
    error_code = (parsed.get("error") or {}).get("code")
    return {"ok": False, "error_code": error_code} if error_code else {"ok": False}


def _append(request: Request, call_id: str, entry: dict) -> None:
    """Atomarer Anhang per jsonb `||`: kein Lesen-vor-Schreiben, also kein Wettlauf
    zwischen zwei Tool-Aufrufen desselben Anrufs. Die Session kommt über dieselbe
    get_db-Factory wie der Request, damit Tests mit eigener Wegwerf-DB (dependency
    override) auch hier landen statt in einer unbeteiligten Datenbank."""
    factory = request.app.dependency_overrides.get(get_db, get_db)
    gen = factory()
    session = next(gen)
    try:
        session.execute(
            text(
                "UPDATE calls SET tool_calls = tool_calls || CAST(:entry AS jsonb) "
                "WHERE id = CAST(:call_id AS uuid)"
            ),
            {"entry": json.dumps(entry), "call_id": call_id},
        )
        session.commit()
    finally:
        with contextlib.suppress(StopIteration):
            next(gen)
