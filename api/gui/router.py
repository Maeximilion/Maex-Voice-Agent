"""Seiten und HTMX-Fragmente der Betriebsansicht (docs/06 §3, docs/11 §gui).

Die GUI ist eine Eingangsschicht: sie darf `domain/` benutzen, enthält aber selbst
keine Fachlogik (docs/11 §1). Was hier steht, ist Auswahl und Darstellung.

Zugang: in Stufe 1 schützt der Reverse-Proxy den Pfad `/gui/*` (Basic-Auth oder
VPN, docs/13 §Caddy), nicht die Anwendung. Deshalb hängt hier bewusst keine
Token-Abhängigkeit - ein Browser schickt keinen Bearer-Kopf mit.
"""

import uuid
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.core.time import to_local
from api.db import SessionLocal, get_db
from api.domain.reservations import list_today
from api.gui.sse import today_event_stream
from api.models import ServiceConfig, Tenant

TEMPLATES = Path(__file__).parent / "templates"
STATIC = Path(__file__).parent / "static"

templates = Jinja2Templates(directory=str(TEMPLATES))
router = APIRouter(prefix="/gui", tags=["gui"])

NO_TENANT = "Keine Betriebsdaten gefunden. Bitte das Team benachrichtigen."

# Der Punkt der Kopfzeile: Farbe allein reicht nie, der Text steht daneben
# (docs/06 §1 Regel 4, §3 Kopfzeile).
MODE_LABELS = {
    "primary": ("ok", "KI nimmt an"),
    "overflow": ("warn", "KI springt ein"),
    "paused": ("danger", "KI ist aus"),
    "shadow": ("warn", "KI hört mit"),
}


def _tenant(session: Session) -> Tenant | None:
    """Stufe 1 fährt einen Mandanten. Der älteste ist er (wie in sim/session.py)."""
    return session.scalars(select(Tenant).order_by(Tenant.created_at)).first()


def _tenant_snapshot() -> tuple[uuid.UUID | None, str]:
    """Mandant und Zeitzone in einer eigenen, sofort geschlossenen Session."""
    session = SessionLocal()
    try:
        tenant = _tenant(session)
        if tenant is None:
            return None, "UTC"
        return tenant.id, tenant.timezone
    finally:
        session.close()


def _clock(value: datetime, tz_name: str) -> str:
    return to_local(value, tz_name).strftime("%H:%M")


def _today_rows(session: Session, tenant: Tenant) -> list[dict]:
    return [
        {
            "id": str(row.reservation_id),
            "time": _clock(row.reserved_for, tenant.timezone),
            "party_size": row.party_size,
            "guest_name": row.guest_name,
            "phone": row.phone,
            "note": row.note,
        }
        for row in list_today(session, tenant.id, tenant.timezone)
    ]


def _header(session: Session, tenant: Tenant) -> dict:
    """Werte der Kopfzeile. Knöpfe und Live-Aktualisierung kommen mit T-3.2."""
    config = session.get(ServiceConfig, tenant.id)
    if config is None:
        return {"mode_tone": "danger", "mode_label": "KI ist aus", "config": None}
    tone, label = MODE_LABELS.get(config.call_mode, ("danger", "KI ist aus"))
    return {"mode_tone": tone, "mode_label": label, "config": config}


@router.get("", response_class=HTMLResponse, include_in_schema=False)
@router.get("/", response_class=HTMLResponse, include_in_schema=False)
def betrieb(request: Request, session: Session = Depends(get_db)) -> HTMLResponse:
    """Betriebsansicht fürs Tablet."""
    tenant = _tenant(session)
    if tenant is None:
        return templates.TemplateResponse(
            request, "betrieb/index.html", {"problem": NO_TENANT, "today": []}, 503
        )
    return templates.TemplateResponse(
        request,
        "betrieb/index.html",
        {
            "tenant": tenant,
            "today": _today_rows(session, tenant),
            **_header(session, tenant),
        },
    )


@router.get("/fragments/heute", response_class=HTMLResponse, include_in_schema=False)
def heute_fragment(
    request: Request, session: Session = Depends(get_db)
) -> HTMLResponse:
    """Nur die Liste der Spalte "Heute". Holt die Seite nach jedem Ereignis."""
    tenant = _tenant(session)
    if tenant is None:
        return templates.TemplateResponse(
            request, "fragments/heute.html", {"problem": NO_TENANT, "today": []}, 503
        )
    return templates.TemplateResponse(
        request, "fragments/heute.html", {"today": _today_rows(session, tenant)}
    )


@router.get("/events", include_in_schema=False)
def events(request: Request) -> StreamingResponse:
    """Ereignisstrom für die Live-Aktualisierung.

    Bewusst ohne die Session der Anfrage: die wird erst nach dem Ende der
    Antwort geschlossen, und dieser Strom bleibt stundenlang offen. Jedes
    Tablet wuerde sonst eine Verbindung aus dem Pool binden und ein paar
    Geraete den heissen Pfad blockieren. Der Mandant wird einmal in einer
    kurzlebigen Session aufgeloest, danach holt sich jeder Takt des Stroms
    seine eigene (gui/sse.py).
    """
    tenant_id, tz_name = _tenant_snapshot()
    if tenant_id is None:
        return StreamingResponse(
            iter(["event: problem\ndata: tenant\n\n"]), media_type="text/event-stream"
        )
    return StreamingResponse(
        today_event_stream(request, tenant_id, tz_name),
        media_type="text/event-stream",
        # Ein puffernder Proxy haelt Ereignisse sonst zurueck, bis genug
        # Bytes zusammen sind - die Live-Ansicht waere dann nicht live.
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
