"""Einstiegspunkt der Agent-API.

Stand: Gerüst. Die Tools kommen in api/tools/ dazu, siehe docs/04_API_TOOLS.md.
Jede Antwort folgt der Hülle aus docs/04 §1. Der Agent liest unsere Antworten vor,
deshalb darf nie ein Stacktrace nach außen gelangen.
"""

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from api.config import settings
from api.core import envelope
from api.core.auth import require_token
from api.core.errors import AppError
from api.core.logging import configure_logging, get_logger, request_context_middleware
from api.tools.router import router as tools_router

configure_logging(settings.log_level)
logger = get_logger("api")

app = FastAPI(title="Maex Voice-Agent API", version="0.1.0")
app.middleware("http")(request_context_middleware)
app.include_router(tools_router)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "env": settings.env}


@app.post("/v1/tools/ping", dependencies=[Depends(require_token)])
def ping() -> JSONResponse:
    """Beweist, dass Auth und Antwort-Hülle stehen. Wird später entfernt."""
    return envelope.ok({"pong": True})


@app.exception_handler(AppError)
def app_error_handler(_request: Request, exc: AppError) -> JSONResponse:
    return envelope.from_error(exc)


@app.exception_handler(RequestValidationError)
def validation_error_handler(
    _request: Request, exc: RequestValidationError
) -> JSONResponse:
    first = exc.errors()[0] if exc.errors() else {}
    field = ".".join(str(p) for p in first.get("loc", ()) if p != "body")
    return envelope.fail(
        "invalid_input", f"{field}: {first.get('msg', 'ungültige Eingabe')}"
    )


@app.exception_handler(HTTPException)
def http_exception_handler(_request: Request, exc: HTTPException) -> JSONResponse:
    return envelope.fail("invalid_input", str(exc.detail), status=exc.status_code)


@app.exception_handler(Exception)
def unhandled_exception_handler(_request: Request, exc: Exception) -> JSONResponse:
    logger.error("unbehandelter Fehler", exc_info=exc)
    return envelope.fail(
        "service_unavailable", "interner Fehler", say=envelope.SAY_ON_FAILURE
    )
