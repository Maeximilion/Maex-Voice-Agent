"""Einstiegspunkt der Agent-API.

Stand: Gerüst. Die Tools kommen in api/tools/ dazu, siehe docs/04_API_TOOLS.md.
Jede Antwort folgt der Hülle aus docs/04 §1. Der Agent liest unsere Antworten vor,
deshalb darf nie ein Stacktrace nach außen gelangen.
"""

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException

from api.config import settings
from api.core import envelope
from api.core.auth import require_token
from api.core.errors import AppError
from api.core.logging import configure_logging, get_logger, request_context_middleware
from api.core.tool_log import tool_call_log_middleware
from api.db import get_db
from api.domain.calls import end_call, start_call
from api.gui import mount_gui
from api.kitchen.router import router as kitchen_router
from api.schemas.calls import EndCallRequest, StartCallRequest
from api.tools.router import router as tools_router

configure_logging(settings.log_level)
logger = get_logger("api")

# The interactive description and the schema list every path. They help while
# developing and tell everyone else what to aim at, so they exist in dev only.
_DESCRIBED = settings.env == "dev"
app = FastAPI(
    title="Maex Voice-Agent API",
    version="0.1.0",
    docs_url="/docs" if _DESCRIBED else None,
    redoc_url="/redoc" if _DESCRIBED else None,
    openapi_url="/openapi.json" if _DESCRIBED else None,
)
app.middleware("http")(tool_call_log_middleware)
app.middleware("http")(request_context_middleware)
app.include_router(tools_router)
app.include_router(kitchen_router)
mount_gui(app)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "env": settings.env}


@app.post("/v1/tools/ping", dependencies=[Depends(require_token)])
def ping() -> JSONResponse:
    """Beweist, dass Auth und Antwort-Hülle stehen. Wird später entfernt."""
    return envelope.ok({"pong": True})


@app.post("/v1/calls/start", dependencies=[Depends(require_token)])
def calls_start(
    body: StartCallRequest, session: Session = Depends(get_db)
) -> JSONResponse:
    """Anruf-Log beginnt (docs/04 §Endpunkte). Liefert die call_id für alle
    folgenden Tool-Aufrufe der Plattform."""
    result = start_call(session, body)
    return envelope.ok(result.model_dump(mode="json"))


@app.post("/v1/calls/end", dependencies=[Depends(require_token)])
def calls_end(body: EndCallRequest, session: Session = Depends(get_db)) -> JSONResponse:
    """Anruf-Log schließt (docs/04 §Endpunkte)."""
    result = end_call(session, body)
    return envelope.ok(result.model_dump(mode="json"))


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
    """Starlette's class, the parent of FastAPI's: registered for the child
    only, the errors the framework raises itself (unknown path, wrong method, a
    body its parser gives up on) answered `{"detail": ...}` past the envelope."""
    code = "not_found" if exc.status_code == 404 else "invalid_input"
    response = envelope.fail(code, str(exc.detail), status=exc.status_code)
    # What the framework says besides the body, `Allow` on a 405 for one.
    response.headers.update(exc.headers or {})
    return response


@app.exception_handler(Exception)
def unhandled_exception_handler(_request: Request, exc: Exception) -> JSONResponse:
    logger.error("unbehandelter Fehler", exc_info=exc)
    return envelope.fail(
        "service_unavailable", "interner Fehler", say=envelope.SAY_ON_FAILURE
    )
