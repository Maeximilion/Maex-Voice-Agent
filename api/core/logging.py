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
from sqlalchemy.exc import DBAPIError

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
            exc = record.exc_info[1]
            if exc is None or _database_error(exc) is None:
                line["exc"] = self.formatException(record.exc_info)
            else:
                line["exc"] = error_text(exc)
        return json.dumps(line, ensure_ascii=False)


def error_text(exc: BaseException) -> str:
    """What a log line may say about an error: its text, except for a database
    error. Postgres writes the failing row into its message ("DETAIL: Failing
    row contains (name, phone, note)"), past hide_parameters, so only the types
    and the constraint go out (repo audit of 27.10.2026, finding 11)."""
    db = _database_error(exc)
    if db is None:
        return str(exc)
    text = f"{type(db).__name__} ({type(db.orig).__name__})"
    if db is not exc:
        text = f"{type(exc).__name__} from {text}"
    constraint = getattr(getattr(db.orig, "diag", None), "constraint_name", None)
    return f"{text} on {constraint}" if constraint else text


def _database_error(exc: BaseException | None) -> DBAPIError | None:
    """The database error in an exception's chain, the way a traceback prints it."""
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        if isinstance(exc, DBAPIError):
            return exc
        seen.add(id(exc))
        exc = exc.__cause__ or exc.__context__
    return None


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    # Uvicorn bringt eigene Handler mit; die würden sonst zweimal und unformatiert loggen.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers[:] = []
        # Die Middleware loggt jede Anfrage mit Dauer und request_id; das Access-Log wäre ein Duplikat ohne Kontext.
        logger.propagate = name != "uvicorn.access"


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
