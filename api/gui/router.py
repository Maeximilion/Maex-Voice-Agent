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
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.core.errors import AppError
from api.core.logging import get_logger
from api.core.time import to_local
from api.db import SessionLocal, get_db
from api.domain.callbacks import list_open, mark_done
from api.domain.ordering import board as order_board
from api.domain.ordering import correction
from api.domain.reservations import list_today
from api.domain.status import config as switches
from api.gui import orders_view
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


# Anlass des Rückrufs in Worten des Teams (docs/06 §1 Regel 2), nie der Code-Wert.
CALLBACK_LABELS = {
    "complaint": ("danger", "Beschwerde"),
    "not_understood": ("warn", "Nicht verstanden"),
    "human_requested": ("accent", "Will Mitarbeiter sprechen"),
    "out_of_scope": ("accent", "Anderes Anliegen"),
}


def _callback_rows(session: Session, tenant: Tenant) -> list[dict]:
    rows = []
    for cb in list_open(session, tenant.id):
        tone, label = CALLBACK_LABELS.get(cb.reason, ("accent", "Rückruf"))
        rows.append(
            {
                "id": str(cb.callback_id),
                "time": _clock(cb.created_at, tenant.timezone),
                "phone": cb.phone,
                "reason": cb.reason,
                "summary": cb.summary,
                "tone": tone,
                "label": label,
            }
        )
    return rows


def _order_rows(session: Session, tenant: Tenant) -> list[dict]:
    return [
        orders_view.card(o, tenant.timezone)
        for o in order_board.list_new(session, tenant.id, tenant.timezone)
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
            "callbacks": _callback_rows(session, tenant),
            "orders": _order_rows(session, tenant),
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


@router.get(
    "/fragments/rueckrufe", response_class=HTMLResponse, include_in_schema=False
)
def rueckrufe_fragment(
    request: Request, session: Session = Depends(get_db)
) -> HTMLResponse:
    """Nur die Liste der Spalte "Rückrufe". Holt die Seite nach jedem Ereignis."""
    tenant = _tenant(session)
    if tenant is None:
        return templates.TemplateResponse(
            request, "fragments/rueckrufe.html", {"problem": NO_TENANT}, 503
        )
    return templates.TemplateResponse(
        request,
        "fragments/rueckrufe.html",
        {"callbacks": _callback_rows(session, tenant)},
    )


@router.post(
    "/rueckrufe/{callback_id}/erledigt",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(_require_htmx)],
)
def rueckruf_erledigt(
    request: Request, callback_id: uuid.UUID, session: Session = Depends(get_db)
) -> HTMLResponse:
    """Erledigt: die Karte verschwindet, die Spalte kommt neu zurueck.

    Ein Rueckruf, den es nicht (mehr) gibt, ist kein Fehler fuer das Team - ein
    anderes Tablet war schneller oder die Seite ist alt. Die frische Liste ist
    dann genau die richtige Antwort.
    """
    tenant = _tenant(session)
    if tenant is None:
        return templates.TemplateResponse(
            request, "fragments/rueckrufe.html", {"problem": NO_TENANT}, 503
        )
    try:
        mark_done(session, tenant.id, callback_id)
    except AppError as exc:
        session.rollback()
        logger.info("Rueckruf nicht erledigt: %s", exc.message)
    return templates.TemplateResponse(
        request,
        "fragments/rueckrufe.html",
        {"callbacks": _callback_rows(session, tenant)},
    )


# --- Spalte "Neue Bestellungen" (T-4.7) ---------------------------------------------

ORDER_FAILED = "Das hat nicht geklappt. Bitte nochmal tippen."
# Nach dem Speichern holt app.js die Spalte neu, die Korrektur schliesst sich.
ORDERS_CHANGED = "bestellungen-geaendert"


def _orders_fragment(
    request: Request,
    session: Session,
    tenant: Tenant | None,
    problem: str | None = None,
) -> HTMLResponse:
    if tenant is None:
        return templates.TemplateResponse(
            request, "fragments/bestellungen.html", {"problem": NO_TENANT}, 503
        )
    return templates.TemplateResponse(
        request,
        "fragments/bestellungen.html",
        {"orders": _order_rows(session, tenant), "order_problem": problem},
    )


@router.get(
    "/fragments/bestellungen", response_class=HTMLResponse, include_in_schema=False
)
def orders_fragment(
    request: Request, session: Session = Depends(get_db)
) -> HTMLResponse:
    """Nur die Spalte "Neue Bestellungen". Holt die Seite nach jedem Ereignis."""
    return _orders_fragment(request, session, _tenant(session))


def _order_tap(
    request: Request, session: Session, change: Callable[[uuid.UUID], object]
) -> HTMLResponse:
    """Passt oder Nochmal senden: die Spalte kommt frisch zurueck.

    Eine Bestellung, die es nicht mehr gibt oder die ein anderes Tablet schon
    abgehakt hat, ist fuer das Team kein Fehler - die frische Spalte zeigt den
    Stand. Nur ein echter Widerspruch (Passt auf roter Karte) bekommt eine Zeile.
    """
    tenant = _tenant(session)
    if tenant is None:
        return _orders_fragment(request, session, None)
    problem = None
    try:
        change(tenant.id)
    except AppError as exc:
        session.rollback()
        logger.info("Bestellung nicht umgestellt: %s", exc.message)
        if exc.code == "conflict":
            problem = ORDER_FAILED
    return _orders_fragment(request, session, tenant, problem)


@router.post(
    "/bestellungen/{order_id}/passt",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(_require_htmx)],
)
def approve_order_tap(
    request: Request, order_id: uuid.UUID, session: Session = Depends(get_db)
) -> HTMLResponse:
    """Keine Rueckfrage: nur Loeschen und Stornieren fragen nach (docs/06 §1 Regel 6)."""
    return _order_tap(
        request, session, lambda t: order_board.approve_order(session, t, order_id)
    )


@router.post(
    "/bestellungen/{order_id}/nochmal-senden",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(_require_htmx)],
)
def resend_order_tap(
    request: Request, order_id: uuid.UUID, session: Session = Depends(get_db)
) -> HTMLResponse:
    return _order_tap(
        request, session, lambda t: order_board.resend_order(session, t, order_id)
    )


async def _form(request: Request) -> dict[str, str]:
    """Formularfelder eines HTMX-Taps (application/x-www-form-urlencoded).

    Mit der Standardbibliothek statt ueber Form(): das braeuchte python-multipart
    als zusaetzliche Abhaengigkeit, nur um drei Textfelder zu lesen. Async, damit
    der Koerper im Event-Loop gelesen wird und der Endpunkt selbst synchron bleibt.
    """
    body = (await request.body()).decode("utf-8", errors="replace")
    return {k: v[-1] for k, v in parse_qs(body, keep_blank_values=True).items()}


def _correction_panel(
    request: Request,
    order_id: uuid.UUID,
    plan: correction.CorrectionPlan | None,
    edit: orders_view.EditState | None,
    message: str | None = None,
    status: int = 200,
) -> HTMLResponse:
    context: dict = {"order_id": str(order_id), "message": message}
    if plan is not None and edit is not None:
        try:
            context.update(orders_view.edit_lines(plan, edit))
        except AppError as exc:
            # Stand passt nicht zu den Positionen: Meldung statt Fehler 500.
            context["message"], status = exc.say, 409
    return templates.TemplateResponse(
        request, "fragments/korrektur.html", context, status
    )


@router.get(
    "/bestellungen/{order_id}/korrigieren",
    response_class=HTMLResponse,
    include_in_schema=False,
)
def correction_open(
    request: Request, order_id: uuid.UUID, session: Session = Depends(get_db)
) -> HTMLResponse:
    tenant = _tenant(session)
    if tenant is None:
        return _correction_panel(request, order_id, None, None, NO_TENANT, 503)
    try:
        plan = correction.preview_correction(session, tenant.id, order_id)
    except AppError as exc:
        return _correction_panel(
            request, order_id, None, None, exc.say or ORDER_FAILED, 409
        )
    return _correction_panel(request, order_id, plan, orders_view.fresh_state(plan))


@router.post(
    "/bestellungen/{order_id}/korrigieren/vorschau",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(_require_htmx)],
)
def correction_preview(
    request: Request,
    order_id: uuid.UUID,
    form: dict[str, str] = Depends(_form),
    session: Session = Depends(get_db),
) -> HTMLResponse:
    """Ein Tap in der Korrektur: anwenden, neu rechnen, Karte zurueck. Schreibt nichts."""
    tenant = _tenant(session)
    if tenant is None:
        return _correction_panel(request, order_id, None, None, NO_TENANT, 503)
    op, number = form.get("op", ""), form.get("number", "")
    try:
        edit = orders_view.parse_state(form.get("state", ""))
        before = correction.preview_correction(
            session, tenant.id, order_id, edit.request()
        )
    except AppError as exc:
        # Veralteter oder kaputter Stand: nichts raten, neu oeffnen lassen.
        return _correction_panel(
            request, order_id, None, None, exc.say or ORDER_FAILED, 409
        )

    def find(number: str) -> uuid.UUID | None:
        item = correction.find_by_number(session, tenant.id, number)
        return item.id if item else None

    snapshot = edit.dump()
    try:
        message = orders_view.apply_op(edit, op, before, number, find)
        plan = correction.preview_correction(
            session, tenant.id, order_id, edit.request()
        )
    except AppError as exc:
        # Der Tap passt nicht (Menge, Auswahl): der alte Stand bleibt, mit Zeile.
        return _correction_panel(
            request,
            order_id,
            before,
            orders_view.parse_state(snapshot),
            exc.say or ORDER_FAILED,
            422,
        )
    return _correction_panel(request, order_id, plan, edit, message)


@router.post(
    "/bestellungen/{order_id}/korrigieren",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(_require_htmx)],
)
def correction_save(
    request: Request,
    order_id: uuid.UUID,
    form: dict[str, str] = Depends(_form),
    session: Session = Depends(get_db),
) -> HTMLResponse:
    """Mit Grund speichern. Danach schliesst die Korrektur und die Spalte laedt neu."""
    tenant = _tenant(session)
    if tenant is None:
        return _correction_panel(request, order_id, None, None, NO_TENANT, 503)
    try:
        edit = orders_view.parse_state(form.get("state", ""))
    except AppError as exc:
        return _correction_panel(request, order_id, None, None, exc.say, 409)
    try:
        correction.apply_correction(
            session, tenant.id, order_id, edit.request(), form.get("reason", "")
        )
    except AppError as exc:
        session.rollback()
        logger.info("Korrektur nicht gespeichert: %s", exc.message)
        say = exc.say or ORDER_FAILED
        try:
            plan = correction.preview_correction(
                session, tenant.id, order_id, edit.request()
            )
        except AppError:
            return _correction_panel(request, order_id, None, None, say, 409)
        return _correction_panel(request, order_id, plan, edit, say, 422)
    response = HTMLResponse("")
    response.headers["HX-Trigger"] = ORDERS_CHANGED
    return response


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
