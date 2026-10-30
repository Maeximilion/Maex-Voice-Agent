"""Seiten und HTMX-Fragmente der Betriebsansicht (docs/06 §3, docs/11 §gui).

Die GUI ist eine Eingangsschicht: sie darf `domain/` benutzen, enthält aber selbst
keine Fachlogik (docs/11 §1). Was hier steht, ist Auswahl und Darstellung.

Zugang: in Stufe 1 schützt der Reverse-Proxy den Pfad `/gui/*` (Basic-Auth oder
VPN, docs/13 §Caddy), nicht die Anwendung. Deshalb hängt hier bewusst keine
Token-Abhängigkeit - ein Browser schickt keinen Bearer-Kopf mit.
"""

import secrets
import uuid
from collections.abc import Callable
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.config import LOCAL_ENVS, settings
from api.core.auth import require_gui_proxy
from api.core.errors import AppError
from api.core.logging import get_logger
from api.core.time import business_day, to_local
from api.db import SessionLocal, get_db
from api.domain.callbacks import list_open, mark_done
from api.domain.callbacks.board import NUMBER_OTHER, NUMBER_WITHHELD
from api.domain.menu import sold_out as dishes
from api.domain.ordering import board as order_board
from api.domain.ordering import correction
from api.domain.reservations import list_today
from api.domain.reservations.spoken import WEEKDAYS
from api.domain.status import config as switches
from api.domain.status import holidays as holiday_days
from api.domain.status.mode import CAUTION, mode_at
from api.gui import orders_view
from api.gui.sse import today_event_stream
from api.models import ServiceConfig, Tenant

TEMPLATES = Path(__file__).parent / "templates"
STATIC = Path(__file__).parent / "static"

templates = Jinja2Templates(directory=str(TEMPLATES))
# Every page, fragment and the event stream: only what came through Caddy (outside dev).
router = APIRouter(
    prefix="/gui", tags=["gui"], dependencies=[Depends(require_gui_proxy)]
)
logger = get_logger("api.gui.router")

NO_TENANT = "Keine Betriebsdaten gefunden. Bitte das Team benachrichtigen."
SWITCH_FAILED = "Umschalten hat nicht geklappt. Bitte nochmal tippen."
# Die Knoepfe der Kopfzeile (docs/06 §3). Nur diese Schritte kommen vom Tablet.
WAIT_STEP = 15
# Adresse in der Kopfzeile -> Service in service_config (T-3.6).
WAIT_SERVICES = {"abholung": "pickup", "lieferung": "delivery"}

# Der Punkt der Kopfzeile: Farbe allein reicht nie, der Text steht daneben
# (docs/06 §1 Regel 4, §3 Kopfzeile).
MODE_LABELS = {
    "primary": ("ok", "KI nimmt an"),
    "overflow": ("warn", "KI springt ein"),
    "paused": ("danger", "KI ist aus"),
    "shadow": ("warn", "KI hört mit"),
}
# The switch of the header (T-8.1), from the mode in which the agent does the
# least to the one in which it does the most (`domain/status/mode.py`). A step
# up asks first, a step down is one tap: the way back needs no thinking
# (docs/09 §4).
MODE_CHOICES = CAUTION


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


def _today_rows(session: Session, tenant: Tenant) -> list[dict]:
    return [
        {
            "id": str(row.reservation_id),
            "time": orders_view.clock(row.reserved_for, tenant.timezone),
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


# A number the call did not come from, in the team's words; none for the
# caller's own number (#252).
CALLBACK_NUMBER_NOTES = {
    NUMBER_OTHER: "Andere Nummer als der Anruf",
    NUMBER_WITHHELD: "Anruf ohne Rufnummer",
}


def _callback_rows(session: Session, tenant: Tenant) -> list[dict]:
    rows = []
    for cb in list_open(session, tenant.id):
        tone, label = CALLBACK_LABELS.get(cb.reason, ("accent", "Rückruf"))
        origin = cb.number_origin
        rows.append(
            {
                "id": str(cb.callback_id),
                "time": orders_view.clock(cb.created_at, tenant.timezone),
                "phone": cb.phone,
                "number_note": CALLBACK_NUMBER_NOTES.get(origin),
                "caller_id": cb.caller_id if origin == NUMBER_OTHER else None,
                "reason": cb.reason,
                "summary": cb.summary,
                "tone": tone,
                "label": label,
            }
        )
    return rows


def _holiday_rows(session: Session, tenant: Tenant) -> list[dict]:
    """Holidays the team has not answered yet (T-9.5), by date."""
    today = business_day(tz_name=tenant.timezone)
    return [
        {
            "id": str(s.id),
            "date": f"{WEEKDAYS[s.date.weekday()]}, {s.date:%d.%m.%Y}",
            "name": s.name,
            "like": WEEKDAYS[s.like_weekday],
        }
        for s in holiday_days.open_suggestions(session, tenant.id, today)
    ]


def _order_rows(session: Session, tenant: Tenant) -> list[dict]:
    return [
        orders_view.card(o, tenant.timezone)
        for o in order_board.list_new(session, tenant.id, tenant.timezone)
    ]


def _header(session: Session, tenant: Tenant) -> dict:
    """Werte der Kopfzeile, immer frisch aus service_config."""
    config = session.get(ServiceConfig, tenant.id)
    base = {
        "tenant": tenant,
        "wait_step": WAIT_STEP,
        "wait_min": switches.MIN_WAIT_MINUTES,
        "wait_max": switches.MAX_WAIT_MINUTES,
    }
    if config is None:
        return {
            **base,
            "mode_tone": "danger",
            "mode_label": "KI ist aus",
            "config": None,
        }
    holding = mode_at(session, tenant, config)
    tone, label = MODE_LABELS.get(holding.mode, ("danger", "KI ist aus"))
    rank = MODE_CHOICES.index(holding.mode) if holding.mode in MODE_CHOICES else -1
    return {
        **base,
        "mode_tone": tone,
        "mode_label": label,
        "mode_until": (
            to_local(holding.planned_until, tenant.timezone).strftime("%H:%M")
            if holding.planned_until
            else None
        ),
        "mode_choices": [
            (mode, MODE_LABELS[mode][1], mode == holding.mode, index > rank)
            for index, mode in enumerate(MODE_CHOICES)
        ],
        "config": config,
        "sold_out_count": dishes.sold_out_count(session, tenant.id),
    }


def _page_response(
    request: Request, template: str, context: dict, status_code: int = 200
) -> HTMLResponse:
    """Render a full page and issue the CSRF cookie base.html sends back as a header."""
    token = request.cookies.get("csrf_token")
    if not token or not token.isascii():
        # A token _require_htmx can never accept would lock every tap out.
        token = secrets.token_urlsafe(32)
    response = templates.TemplateResponse(
        request, template, context, status_code=status_code
    )
    response.set_cookie(
        "csrf_token",
        token,
        # Readable on purpose: base.html copies it into X-CSRF-Token at send
        # time, so every tab uses the cookie the browser holds right now.
        httponly=False,
        samesite="lax",
        secure=settings.env not in LOCAL_ENVS or request.url.scheme == "https",
    )
    return response


def _require_htmx(
    request: Request,
    hx_request: str | None = Header(default=None),
    x_csrf_token: str | None = Header(default=None),
) -> None:
    """Require the page's CSRF token for state-changing HTMX requests."""
    if hx_request != "true":
        raise HTTPException(status_code=403, detail="Only from the operations view")
    csrf_cookie = request.cookies.get("csrf_token")
    if (
        not csrf_cookie
        or not x_csrf_token
        or not csrf_cookie.isascii()
        or not x_csrf_token.isascii()
        or not secrets.compare_digest(csrf_cookie, x_csrf_token)
    ):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")


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
    context: dict = {"problem": NO_TENANT, "today": []}
    if tenant is not None:
        context = {
            "tenant": tenant,
            "today": _today_rows(session, tenant),
            "callbacks": _callback_rows(session, tenant),
            "orders": _order_rows(session, tenant),
            "holidays": _holiday_rows(session, tenant),
            **_header(session, tenant),
        }
    return _page_response(
        request, "betrieb/index.html", context, status_code=200 if tenant else 503
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
    "/kopfzeile/modus/{mode}",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(_require_htmx)],
)
def modus(
    request: Request, mode: str, session: Session = Depends(get_db)
) -> HTMLResponse:
    """The mode switch (T-8.1). A step up asks on the button (hx-confirm)."""
    if mode not in MODE_CHOICES:
        raise HTTPException(status_code=404)
    return _switch(request, session, lambda t: switches.set_mode(session, t, mode))


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
    "/kopfzeile/wartezeit/{service}/{step}",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(_require_htmx)],
)
def wartezeit(
    request: Request, service: str, step: int, session: Session = Depends(get_db)
) -> HTMLResponse:
    if service not in WAIT_SERVICES or step not in (-WAIT_STEP, WAIT_STEP):
        raise HTTPException(status_code=404)
    return _switch(
        request,
        session,
        lambda t: switches.change_wait(session, t, WAIT_SERVICES[service], step),
    )


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


# --- Holiday suggestions (T-9.5) -----------------------------------------------------

# The buttons of a suggestion -> the answer in domain/status/holidays.py.
HOLIDAY_ANSWERS = {
    "wie": holiday_days.LIKE,
    "geschlossen": holiday_days.CLOSED,
    "normal": holiday_days.OPEN,
}


@router.post(
    "/feiertage/{special_day_id}/{choice}",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(_require_htmx)],
)
def feiertag(
    request: Request,
    special_day_id: uuid.UUID,
    choice: str,
    session: Session = Depends(get_db),
) -> HTMLResponse:
    """The team's answer: the card goes, the list comes back fresh. A
    suggestion another tablet answered first is no error for the team."""
    if choice not in HOLIDAY_ANSWERS:
        raise HTTPException(status_code=404)
    tenant = _tenant(session)
    if tenant is None:
        return templates.TemplateResponse(
            request, "fragments/feiertage.html", {"holiday_problem": NO_TENANT}, 503
        )
    try:
        holiday_days.answer(session, tenant.id, special_day_id, HOLIDAY_ANSWERS[choice])
    except AppError as exc:
        session.rollback()
        logger.info("Holiday suggestion not answered: %s", exc.message)
    return templates.TemplateResponse(
        request,
        "fragments/feiertage.html",
        {"holidays": _holiday_rows(session, tenant)},
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
            problem = exc.say or ORDER_FAILED
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


@router.post(
    "/bestellungen/{order_id}/in-kasse/{version}",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(_require_htmx)],
)
def register_mark_tap(
    request: Request,
    order_id: uuid.UUID,
    version: str,
    session: Session = Depends(get_db),
) -> HTMLResponse:
    """ "In Kasse eingegeben" (T-11.6). No question back, like "Passt"."""
    return _order_tap(
        request,
        session,
        lambda t: order_board.mark_entered(session, t, order_id, version),
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


# --- "Gericht aus" (T-4.8) --------------------------------------------------------

DISH_FAILED = "Das hat nicht geklappt. Bitte nochmal tippen."
# Nach dem Tap holt app.js die Kopfzeile neu: die Zahl an der Kachel stimmt sofort.
DISHES_CHANGED = "gerichte-geaendert"
DISH_STATES = {"aus": True, "wieder-da": False}


def _dish_list(
    request: Request,
    session: Session,
    tenant: Tenant | None,
    query: str,
    template: str = "fragments/gericht_aus_liste.html",
    problem: str | None = None,
) -> HTMLResponse:
    if tenant is None:
        return templates.TemplateResponse(
            request,
            template,
            {"dishes": [], "query": query, "dish_problem": NO_TENANT},
            503,
        )
    return templates.TemplateResponse(
        request,
        template,
        {
            "dishes": dishes.list_switches(session, tenant.id, query),
            "query": query,
            "dish_problem": problem,
        },
    )


@router.get("/gericht-aus", response_class=HTMLResponse, include_in_schema=False)
def dishes_open(request: Request, session: Session = Depends(get_db)) -> HTMLResponse:
    """Der Kasten "Gericht aus" mit Suchfeld und allen aktiven Gerichten."""
    return _dish_list(
        request, session, _tenant(session), "", "fragments/gericht_aus.html"
    )


@router.get(
    "/fragments/gericht-aus", response_class=HTMLResponse, include_in_schema=False
)
def dishes_fragment(
    request: Request, q: str = "", session: Session = Depends(get_db)
) -> HTMLResponse:
    """Nur die Liste, gefiltert nach dem Suchfeld."""
    return _dish_list(request, session, _tenant(session), q)


@router.post(
    "/gericht-aus/{menu_item_id}/{state}",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(_require_htmx)],
)
def dish_switch(
    request: Request,
    menu_item_id: uuid.UUID,
    state: str,
    form: dict[str, str] = Depends(_form),
    session: Session = Depends(get_db),
) -> HTMLResponse:
    """Ein Tap: heute aus oder wieder da. Die Liste kommt frisch zurueck.

    Ein Gericht, das es nicht (mehr) gibt, ist fuer das Team kein Fehler - die
    frische Liste zeigt den Stand. Nur ein Datenbankfehler bekommt eine Zeile.
    """
    if state not in DISH_STATES:
        raise HTTPException(status_code=404)
    tenant = _tenant(session)
    query = form.get("q", "")
    if tenant is None:
        return _dish_list(request, session, None, query)
    problem = None
    try:
        dishes.set_sold_out(
            session, tenant.id, menu_item_id, DISH_STATES[state], tenant.timezone
        )
    except AppError as exc:
        session.rollback()
        logger.info("Gericht nicht umgeschaltet: %s", exc.message)
        if exc.code != "not_found":
            problem = DISH_FAILED
    response = _dish_list(request, session, tenant, query, problem=problem)
    response.headers["HX-Trigger"] = DISHES_CHANGED
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
