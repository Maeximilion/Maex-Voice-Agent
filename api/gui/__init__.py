"""Betriebsansicht: Router, Ereignisstrom, Vorlagen, eigene Dateien."""

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

__all__ = ["mount_gui"]


def mount_gui(app: FastAPI) -> None:
    """Seiten unter /gui, eigene Dateien unter /gui/static (docs/13 §Caddy).

    Der Import steht bewusst in der Funktion: sonst haengt am Paket ein
    Name `router`, der das Modul `api.gui.router` verdeckt.
    """
    from api.config import settings
    from api.gui.router import STATIC, router

    app.include_router(router)
    if settings.env == "dev":
        from api.gui.dev import router as dev_router

        app.include_router(dev_router)
    app.mount("/gui/static", StaticFiles(directory=str(STATIC)), name="gui-static")
