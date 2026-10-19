"""JSON-Logging. Jede Zeile trägt call_id und request_id, sofern im Kontext gesetzt."""

import json
import logging
import sys
import time
import uuid
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from datetime import UTC, datetime

from fastapi import Request, Response

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
call_id_var: ContextVar[str | None] = ContextVar("call_id", default=None)

REQUEST_ID_HEADER = "X-Request-ID"


def bind_call_id(call_id: str | None) -> None:
    """Von Tools aufgerufen, sobald die call_id aus dem Request bekannt ist."""
    call_id_var.set(call_id)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        line = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": request_id_var.get(),
            "call_id": call_id_var.get(),
        }
        extra = getattr(record, "extra", None)
        if isinstance(extra, dict):
            line.update(extra)
        if record.exc_info:
            line["exc"] = self.formatException(record.exc_info)
        return json.dumps(line, ensure_ascii=False)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    # Uvicorn bringt eigene Handler mit; die würden sonst zweimal und unformatiert loggen.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(name).handlers[:] = []
        logging.getLogger(name).propagate = True
    # Die Middleware loggt jede Anfrage mit Dauer und request_id; das Access-Log wäre ein Duplikat ohne Kontext.
    logging.getLogger("uvicorn.access").propagate = False


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def log(logger: logging.Logger, level: int, msg: str, **fields: object) -> None:
    logger.log(level, msg, extra={"extra": fields})


async def request_context_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Setzt request_id je Request, misst die Dauer und schreibt eine Zeile ins Log."""
    request_id = request.headers.get(REQUEST_ID_HEADER) or uuid.uuid4().hex
    token_request = request_id_var.set(request_id)
    token_call = call_id_var.set(None)
    started = time.perf_counter()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
    finally:
        log(
            get_logger("api.request"),
            logging.INFO,
            "request",
            method=request.method,
            path=request.url.path,
            status=status,
            duration_ms=round((time.perf_counter() - started) * 1000, 1),
        )
        request_id_var.reset(token_request)
        call_id_var.reset(token_call)
    response.headers[REQUEST_ID_HEADER] = request_id
    return response
