"""Betriebsansicht: Router, Ereignisstrom, Vorlagen, eigene Dateien."""

import os

from fastapi import FastAPI
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from starlette.types import Scope

__all__ = ["mount_gui"]


class _RevalidatedStatic(StaticFiles):
    """The view's own files, asked for again on every use (`no-cache`).

    Without a cache header a browser keeps a script for hours by guesswork, and
    a page from a newer build then runs the script of an older one. The ETag
    turns the question into a 304 without a body."""

    def file_response(
        self,
        full_path: str | os.PathLike[str],
        stat_result: os.stat_result,
        scope: Scope,
        status_code: int = 200,
    ) -> Response:
        response = super().file_response(full_path, stat_result, scope, status_code)
        response.headers["Cache-Control"] = "no-cache"
        return response


def mount_gui(app: FastAPI) -> None:
    """Seiten unter /gui, eigene Dateien unter /gui/static (docs/13 §Caddy).

    Der Import steht bewusst in der Funktion: sonst haengt am Paket ein
    Name `router`, der das Modul `api.gui.router` verdeckt.
    """
    from api.config import settings
    from api.gui.access import front
    from api.gui.access import router as access_router
    from api.gui.router import STATIC, router

    app.include_router(front)
    app.include_router(access_router)
    app.include_router(router)
    if settings.env == "dev":
        from api.gui.dev import router as dev_router

        app.include_router(dev_router)
    app.mount(
        "/gui/static", _RevalidatedStatic(directory=str(STATIC)), name="gui-static"
    )
