"""Betriebsansicht: Router, Ereignisstrom, Vorlagen, eigene Dateien."""

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from api.gui.router import STATIC, router

__all__ = ["mount_gui", "router"]


def mount_gui(app: FastAPI) -> None:
    """Seiten unter /gui, eigene Dateien unter /gui/static (docs/13 §Caddy)."""
    app.include_router(router)
    app.mount("/gui/static", StaticFiles(directory=str(STATIC)), name="gui-static")
