"""Einstiegspunkt der Agent-API.

Stand: Gerüst. Die Tools kommen in api/tools/ dazu — siehe docs/04_API_TOOLS.md.
Wichtig: Jede Antwort folgt der Hülle aus docs/04_API_TOOLS.md §1. Der Agent
liest unsere Antworten vor, deshalb darf nie ein Stacktrace nach außen gelangen.
"""

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse

from api.config import settings

app = FastAPI(title="Maex Voice-Agent API", version="0.1.0")


def require_token(authorization: str = Header(default="")) -> None:
    """Bearer-Token prüfen. Gilt für alle /v1/tools/*-Endpunkte."""
    expected = f"Bearer {settings.agent_api_token}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="unauthorized")


def ok(data: dict, say: str | None = None) -> JSONResponse:
    return JSONResponse({"ok": True, "data": data, "say": say})


def fail(code: str, message: str, say: str | None = None, status: int = 200) -> JSONResponse:
    # Status 200, damit die Voice-Plattform die Antwort dem Agenten vorlegen kann.
    return JSONResponse(
        {"ok": False, "error": {"code": code, "message": message}, "say": say},
        status_code=status,
    )


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "env": settings.env}


@app.post("/v1/tools/ping", dependencies=[Depends(require_token)])
def ping() -> JSONResponse:
    """Beweist, dass Auth und Antwort-Hülle stehen. Wird später entfernt."""
    return ok({"pong": True})


@app.exception_handler(HTTPException)
def http_exception_handler(_request, exc: HTTPException) -> JSONResponse:
    return fail("invalid_input", str(exc.detail), status=exc.status_code)


@app.exception_handler(Exception)
def unhandled_exception_handler(_request, _exc: Exception) -> JSONResponse:
    return fail(
        "service_unavailable",
        "interner Fehler",
        say="Bei mir gibt es gerade eine technische Störung. Ich verbinde Sie mit dem Restaurant.",
        status=200,
    )
