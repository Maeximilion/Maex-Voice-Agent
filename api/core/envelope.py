"""Die Antwort-Hülle aus docs/04 §1. Jede Antwort der Agent-API geht hier durch."""

from typing import Any

from fastapi.responses import JSONResponse

# Re-exported for the HTTP side (main.py, agent/loop.py); defined in core/errors.
from api.core.errors import SAY_ON_FAILURE as SAY_ON_FAILURE
from api.core.errors import AppError


def ok_body(data: dict[str, Any], say: str | None = None) -> dict[str, Any]:
    return {"ok": True, "data": data, "say": say}


def fail_body(code: str, message: str, say: str | None = None) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message}, "say": say}


def ok(data: dict[str, Any], say: str | None = None) -> JSONResponse:
    return JSONResponse(ok_body(data, say))


def fail(
    code: str, message: str, say: str | None = None, status: int = 200
) -> JSONResponse:
    return JSONResponse(fail_body(code, message, say), status_code=status)


def from_error(exc: AppError) -> JSONResponse:
    return fail(exc.code, exc.message, exc.say, exc.status)
