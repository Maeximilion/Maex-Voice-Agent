"""Die Antwort-Hülle aus docs/04 §1. Jede Antwort der Agent-API geht hier durch."""

from typing import Any

from fastapi.responses import JSONResponse

from api.core.errors import AppError

SAY_ON_FAILURE = "Bei mir gibt es gerade eine technische Störung. Ich verbinde Sie mit dem Restaurant."


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
