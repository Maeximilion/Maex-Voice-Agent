"""Seiten und HTMX-Fragmente der Betriebsansicht (docs/06 §3, docs/11 §gui).

Die GUI ist eine Eingangsschicht: sie darf `domain/` benutzen, enthält aber selbst
keine Fachlogik (docs/11 §1). Was hier steht, ist Auswahl und Darstellung.

Zugang: in Stufe 1 schützt der Reverse-Proxy den Pfad `/gui/*` (Basic-Auth oder
VPN, docs/13 §Caddy), nicht die Anwendung. Deshalb hängt hier bewusst keine
Token-Abhängigkeit - ein Browser schickt keinen Bearer-Kopf mit.
"""

import uuid
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.core.errors import AppError
from api.core.logging import get_logger
from api.core.time import to_local
from api.db import SessionLocal, get_db
from api.domain.reservations import list_today
from api.domain.status import config as switches
from api.gui.sse import today_event_stream
from api.models import ServiceConfig, Tenant

TEMPLATES = Path(__file__).parent / "templates"
STATIC = Path(__file__).parent / "static"

templates = Jinja2Templates(directory=str(TEMPLATES))
router = APIRouter(prefix="/gui", tags=["gui"])
logger = get_logger("api.gui.router")

NO_TENANT = "Keine Betriebsdaten gefunden. Bitte das Team benachrichtigen."
SWITCH_FAILED = "Umschalten hat nicht geklappt. Bitte nochmal tippen."
# Die Knoepfe der Kopfzeile (docs/06 §3). Nur diese Schritte kommen vom Tablet.
WAIT_STEPS = (15, 30)

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
    """Werte der Kopfzeile, immer frisch aus service_config."""
    config = session.get(ServiceConfig, tenant.id)
    base = {"tenant": tenant, "wait_steps": WAIT_STEPS}
    if config is None:
        return {
            **base,
            "mode_tone": "danger",
            "mode_label": "KI ist aus",
            "config": None,
        }
    tone, label = MODE_LABELS.get(config.call_mode, ("danger", "KI ist aus"))
    return {**base, "mode_tone": tone, "mode_label": label, "config": config}


def _require_htmx(hx_request: str | None = Header(default=None)) -> None:
    """Schreibende Taps nur von der eigenen Seite.

    Der Proxy schuetzt /gui/* mit Basic-Auth, und die schickt der Browser auch bei
    einem Formular von einer fremden Seite mit. Einen eigenen Kopf wie HX-Request
    darf eine fremde Seite ohne CORS-Freigabe nicht setzen - das genuegt hier als
    Schutz gegen untergeschobene Klicks, ohne Token im Formular.
    """
    if hx_request != "true":
        raise HTTPException(status_code=403, detail="Nur aus der Betriebsansicht")


def _switch(
    request: Request, session: Session, change: Callable[[uuid.UUID], object]
) -> HTMLResponse:
    """Einen Schalter umlegen und die neue Kopfzeile zurueckgeben."""
    tenant = _tenant(session)
    if tenant is None:
        return templates.TemplateResponse(
            request, "fragments/kopfzeile.html", {"header_problem": NO_TENANT}, 503
        )
    status = 200
    problem = None
    try:
        change(tenant.id)
    except AppError as exc:
        session.rollback()
        logger.warning("Kopfzeile nicht umgeschaltet: %s", exc.message)
        status, problem = 409, SWITCH_FAILED
    return templates.TemplateResponse(
        request,
        "fragments/kopfzeile.html",
        {**_header(session, tenant), "header_problem": problem},
        status,
    )


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


@router.get(
    "/fragments/kopfzeile", response_class=HTMLResponse, include_in_schema=False
)
def kopfzeile_fragment(
    request: Request, session: Session = Depends(get_db)
) -> HTMLResponse:
    """Nur die Kopfzeile. Holt die Seite nach jedem Ereignis "header"."""
    tenant = _tenant(session)
    if tenant is None:
        return templates.TemplateResponse(
            request, "fragments/kopfzeile.html", {"header_problem": NO_TENANT}, 503
        )
    return templates.TemplateResponse(
        request, "fragments/kopfzeile.html", _header(session, tenant)
    )


@router.post(
    "/kopfzeile/ki-pausieren",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(_require_htmx)],
)
def ki_pausieren(request: Request, session: Session = Depends(get_db)) -> HTMLResponse:
    """Not-Aus: ein Tap, keine Rueckfrage (docs/06 §3)."""
    return _switch(request, session, lambda t: switches.pause_ai(session, t))


@router.post(
    "/kopfzeile/ki-einschalten",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(_require_htmx)],
)
def ki_einschalten(
    request: Request, session: Session = Depends(get_db)
) -> HTMLResponse:
    """Rueckfrage stellt der Knopf selbst (hx-confirm), hier wird nur geschaltet."""
    return _switch(request, session, lambda t: switches.resume_ai(session, t))


@router.post(
    "/kopfzeile/lieferung/{state}",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(_require_htmx)],
)
def lieferung(
    request: Request, state: str, session: Session = Depends(get_db)
) -> HTMLResponse:
    if state not in ("an", "aus"):
        raise HTTPException(status_code=404)
    enabled = state == "an"
    return _switch(
        request, session, lambda t: switches.set_delivery(session, t, enabled)
    )


@router.post(
    "/kopfzeile/wartezeit/{step}",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(_require_htmx)],
)
def wartezeit(
    request: Request, step: int, session: Session = Depends(get_db)
) -> HTMLResponse:
    if step not in WAIT_STEPS:
        raise HTTPException(status_code=404)
    return _switch(request, session, lambda t: switches.raise_wait(session, t, step))


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
