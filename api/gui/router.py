"""Seiten und HTMX-Fragmente der Betriebsansicht (docs/06 §3, docs/11 §gui).

Die GUI ist eine Eingangsschicht: sie darf `domain/` benutzen, enthält aber selbst
keine Fachlogik (docs/11 §1). Was hier steht, ist Auswahl und Darstellung.

Access: every route needs the restaurant's device session (`gui/session.py`,
#570), behind the proxy check; the sign-in pages are in `gui/access.py`.
"""

import json
import secrets
import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime
from datetime import time as dt_time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.config import LOCAL_ENVS, settings
from api.core import time as clock
from api.core.auth import require_gui_proxy
from api.core.envelope import fail_body
from api.core.errors import AppError, Conflict, InvalidInput
from api.core.logging import get_logger
from api.core.time import business_day, to_local
from api.db import SessionLocal, get_db
from api.domain.access.sessions import Access, devices, end_sessions
from api.domain.callbacks import list_history, list_open, mark_done, reopen
from api.domain.callbacks.board import HISTORY_DAYS, NUMBER_OTHER, NUMBER_WITHHELD
from api.domain.confirm import confirm_tablet
from api.domain.customers.phone import costly
from api.domain.menu import price_check
from api.domain.menu import sold_out as dishes
from api.domain.ordering import board as order_board
from api.domain.ordering import correction
from api.domain.reservations import create_tablet_reservation, floor_plan, list_today
from api.domain.reservations import notice as reservation_notice
from api.domain.reservations.manual import SOURCE_TABLET, WindowFull, describe
from api.domain.reservations.spoken import WEEKDAYS
from api.domain.status import bridge
from api.domain.status import config as switches
from api.domain.status import holidays as holiday_days
from api.domain.status import staff_changes as staff
from api.domain.status.mode import CAUTION, mode_at
from api.domain.status.waits import current_wait, standard_wait
from api.gui import calls_view, figures_view, orders_view
from api.gui.session import LOGIN, csrf_matches, require_admin, require_gui_session
from api.gui.sse import today_event_stream
from api.models import Reservation, ServiceConfig, Tenant
from api.schemas.floor_plan import NewArea, SaveArea
from api.schemas.reservations import TabletDraft, TabletReservationRequest

TEMPLATES = Path(__file__).parent / "templates"
STATIC = Path(__file__).parent / "static"

templates = Jinja2Templates(directory=str(TEMPLATES))
# Every page, fragment and the event stream: only what came through Caddy (outside
# dev), and only for a signed-in device (#570).
router = APIRouter(
    prefix="/gui",
    tags=["gui"],
    dependencies=[Depends(require_gui_proxy), Depends(require_gui_session)],
)
logger = get_logger("api.gui.router")

NO_TENANT = "Keine Betriebsdaten gefunden. Bitte das Team benachrichtigen."
SWITCH_FAILED = "Umschalten hat nicht geklappt. Bitte nochmal tippen."
# Die Knoepfe der Kopfzeile (docs/06 §3). Nur diese Schritte kommen vom Tablet.
WAIT_STEP = 15
# Adresse in der Kopfzeile -> Service in service_config (T-3.6).
WAIT_SERVICES = {"abholung": "pickup", "lieferung": "delivery"}
# The switches of the header, the words in the path are the words on the tablet.
SERVICE_SWITCHES = {
    "abholung": switches.set_pickup,
    "lieferung": switches.set_delivery,
    "reservierung": switches.set_reservations,
}
# Form fields of the notice sheet, Monday is 0 (T-3.13).
NOTICE_FIELDS = {f"tag-{weekday}" for weekday in range(7)}

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
BRIDGE_CHECK = "Zettel kommen nicht an: Kassen-PC und Internet prüfen."


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
            "tables": row.tables,
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


def _callback_row(cb, tz_name: str) -> dict:
    tone, label = CALLBACK_LABELS.get(cb.reason, ("accent", "Rückruf"))
    origin = cb.number_origin
    return {
        "id": str(cb.callback_id),
        "time": orders_view.clock(cb.created_at, tz_name),
        "phone": cb.phone,
        # A premium or service number: a warning and no one-tap link (#691).
        "costly": costly(cb.phone),
        "number_note": CALLBACK_NUMBER_NOTES.get(origin),
        "caller_id": cb.caller_id if origin == NUMBER_OTHER else None,
        "reason": cb.reason,
        "summary": cb.summary,
        "tone": tone,
        "label": label,
    }


def _callback_rows(session: Session, tenant: Tenant) -> list[dict]:
    return [_callback_row(cb, tenant.timezone) for cb in list_open(session, tenant.id)]


def _done_when(done_at, tz_name: str) -> str:
    """ "Erledigt 18:42", with the date when it was not today (local time)."""
    local = to_local(done_at, tz_name)
    if local.date() == to_local(clock.utcnow(), tz_name).date():
        return f"Erledigt {local:%H:%M}"
    return f"Erledigt {local:%d.%m.} {local:%H:%M}"


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


def _price_rows(session: Session, tenant: Tenant) -> list[dict]:
    """Register prices the team has not answered yet (T-4.9), in menu order."""
    return [
        {
            "id": str(d.menu_item_id),
            "number": d.number,
            "name": d.name,
            "price": orders_view.euro(d.price_cents),
            "register": orders_view.euro(d.register_cents),
            "register_cents": d.register_cents,
        }
        for d in price_check.open_differences(session, tenant.id)
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
    now = clock.utcnow()
    holding = mode_at(session, tenant, config, now)
    tone, label = MODE_LABELS.get(holding.mode, ("danger", "KI ist aus"))
    # The AI answers and nothing can be booked: not the green tick (T-3.10).
    if tone == "ok" and not (
        config.pickup_enabled or config.delivery_enabled or config.reservations_enabled
    ):
        tone = "warn"
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
        "services": [
            ("abholung", "Abholung", config.pickup_enabled),
            ("lieferung", "Lieferung", config.delivery_enabled),
            ("reservierung", "Reservierung", config.reservations_enabled),
        ],
        "waits": [
            (
                key,
                key.capitalize(),
                current_wait(config, service, now),
                standard_wait(config, service),
            )
            for key, service in WAIT_SERVICES.items()
        ],
        "config": config,
        "sold_out_count": dishes.sold_out_count(session, tenant.id),
        "bridge_problem": _bridge_problem(session, tenant),
    }


def _bridge_problem(session: Session, tenant: Tenant) -> str | None:
    """The banner for a silent print bridge (T-8.3, docs/06 §3), or None."""
    quiet = bridge.silence(session, tenant)
    if quiet is None:
        return None
    if quiet.since is None:
        return f"Druckbrücke hat sich noch nie gemeldet. {BRIDGE_CHECK}"
    since = to_local(quiet.since, tenant.timezone)
    today = to_local(clock.utcnow(), tenant.timezone).date()
    shown = since.strftime("%H:%M" if since.date() == today else "%d.%m. %H:%M")
    return f"Druckbrücke meldet sich nicht seit {shown}. {BRIDGE_CHECK}"


def _page_response(
    request: Request, template: str, context: dict, status_code: int = 200
) -> HTMLResponse:
    """Render a full page and issue the CSRF cookie base.html sends back as a header."""
    token = request.cookies.get("csrf_token")
    if not token or not token.isascii():
        # A token _require_htmx can never accept would lock every tap out.
        token = secrets.token_urlsafe(32)
    # The sign-in forms post it as a field (gui/access.py), HTMX as a header.
    response = templates.TemplateResponse(
        request, template, {**context, "csrf_token": token}, status_code=status_code
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
    if not csrf_matches(request.cookies.get("csrf_token"), x_csrf_token):
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
        status, problem = 409, exc.say or SWITCH_FAILED
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
            "prices": _price_rows(session, tenant),
            "checks": _check_rows(session, tenant),
            **_header(session, tenant),
        }
    return _page_response(
        request, "betrieb/index.html", context, status_code=200 if tenant else 503
    )


@router.get("/kennzahlen", response_class=HTMLResponse, include_in_schema=False)
def kennzahlen(request: Request, session: Session = Depends(get_db)) -> HTMLResponse:
    """Key figures: the four values of the last 7 days and the history (T-8.4)."""
    tenant = _tenant(session)
    context: dict = {"problem": NO_TENANT}
    if tenant is not None:
        context = {"tenant": tenant, **figures_view.page(session, tenant)}
    return _page_response(
        request, "betrieb/kennzahlen.html", context, status_code=200 if tenant else 503
    )


@router.get("/anrufe", response_class=HTMLResponse, include_in_schema=False)
def anrufe(
    request: Request,
    tag: str | None = None,
    ergebnis: str | None = None,
    anliegen: str | None = None,
    session: Session = Depends(get_db),
) -> HTMLResponse:
    """The call list of one business day, with its filters (T-8.7)."""
    tenant = _tenant(session)
    context: dict = {"problem": NO_TENANT}
    if tenant is not None:
        context = {
            "tenant": tenant,
            **calls_view.page(session, tenant, tag, ergebnis, anliegen),
        }
    return _page_response(
        request, "betrieb/anrufe.html", context, status_code=200 if tenant else 503
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


# --- History of done callbacks (T-3.15, #516) ------------------------------------


def _history_sheet(request: Request, session: Session, tenant: Tenant) -> HTMLResponse:
    rows = [
        {
            **_callback_row(cb, tenant.timezone),
            "done": _done_when(cb.done_at, tenant.timezone),
        }
        for cb in list_history(session, tenant.id)
    ]
    return templates.TemplateResponse(
        request,
        "fragments/rueckruf_verlauf.html",
        {"callbacks": rows, "days": HISTORY_DAYS},
    )


@router.get("/rueckrufe/verlauf", response_class=HTMLResponse, include_in_schema=False)
def rueckrufe_verlauf(
    request: Request, session: Session = Depends(get_db)
) -> HTMLResponse:
    """The sheet with the callbacks done in the last days, to undo a wrong tap."""
    tenant = _tenant(session)
    if tenant is None:
        raise HTTPException(status_code=503)
    return _history_sheet(request, session, tenant)


@router.post(
    "/rueckrufe/{callback_id}/wieder-offen",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(_require_htmx)],
)
def rueckruf_wieder_offen(
    request: Request, callback_id: uuid.UUID, session: Session = Depends(get_db)
) -> HTMLResponse:
    """Bring a card back; the column follows through the event stream.

    Like "Erledigt", an unknown or already open callback is no error for the
    team: the redrawn sheet shows the truth.
    """
    tenant = _tenant(session)
    if tenant is None:
        raise HTTPException(status_code=503)
    try:
        reopen(session, tenant.id, callback_id)
    except AppError as exc:
        session.rollback()
        logger.info("Rueckruf nicht wieder geoeffnet: %s", exc.message)
    return _history_sheet(request, session, tenant)


# --- Holiday suggestions (T-9.5) -----------------------------------------------------


@router.get(
    "/fragments/feiertage", response_class=HTMLResponse, include_in_schema=False
)
def feiertage_fragment(
    request: Request, session: Session = Depends(get_db)
) -> HTMLResponse:
    """Only the holiday list: the page fetches it after each event (#207)."""
    tenant = _tenant(session)
    if tenant is None:
        return templates.TemplateResponse(
            request, "fragments/feiertage.html", {"holiday_problem": NO_TENANT}, 503
        )
    return templates.TemplateResponse(
        request,
        "fragments/feiertage.html",
        {"holidays": _holiday_rows(session, tenant)},
    )


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


# --- Price differences of the register (T-4.9) ---------------------------------------

# The buttons of a card -> take the register's price, or keep ours.
PRICE_ANSWERS = {"uebernehmen": True, "verwerfen": False}


def _price_list(
    request: Request, session: Session, tenant: Tenant | None
) -> HTMLResponse:
    if tenant is None:
        return templates.TemplateResponse(
            request, "fragments/preise.html", {"price_problem": NO_TENANT}, 503
        )
    return templates.TemplateResponse(
        request, "fragments/preise.html", {"prices": _price_rows(session, tenant)}
    )


@router.get("/fragments/preise", response_class=HTMLResponse, include_in_schema=False)
def prices_fragment(
    request: Request, session: Session = Depends(get_db)
) -> HTMLResponse:
    """Only the price cards: the page fetches them after each `prices` event."""
    return _price_list(request, session, _tenant(session))


@router.post(
    "/preise/{menu_item_id}/{register_cents}/{choice}",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(_require_htmx)],
)
def price_answer(
    request: Request,
    menu_item_id: uuid.UUID,
    register_cents: int,
    choice: str,
    session: Session = Depends(get_db),
) -> HTMLResponse:
    """The team's answer to the price on its card; the list comes back fresh. A
    price that moved or an answer from another tablet is no error for the team."""
    if choice not in PRICE_ANSWERS:
        raise HTTPException(status_code=404)
    tenant = _tenant(session)
    if tenant is not None:
        try:
            price_check.decide(
                session,
                tenant.id,
                menu_item_id,
                register_cents,
                take=PRICE_ANSWERS[choice],
            )
        except AppError as exc:
            session.rollback()
            logger.info("Price difference not answered: %s", exc.message)
    return _price_list(request, session, tenant)


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
    request: Request,
    session: Session,
    change: Callable[[uuid.UUID], object],
    notice: Callable[[Any], str] | None = None,
) -> HTMLResponse:
    """Passt oder Nochmal senden: die Spalte kommt frisch zurueck.

    Eine Bestellung, die es nicht mehr gibt oder die ein anderes Tablet schon
    abgehakt hat, ist fuer das Team kein Fehler - die frische Spalte zeigt den
    Stand. Nur ein echter Widerspruch (Passt auf roter Karte) bekommt eine Zeile.
    """
    tenant = _tenant(session)
    if tenant is None:
        return _orders_fragment(request, session, None)
    problem = line = None
    try:
        result = change(tenant.id)
        # The card leaves the column at once: say what happened.
        line = notice(result) if notice else None
    except AppError as exc:
        session.rollback()
        logger.info("Bestellung nicht umgestellt: %s", exc.message)
        if exc.code == "conflict":
            problem = exc.say or ORDER_FAILED
    response = _orders_fragment(request, session, tenant, problem)
    if line:
        # An event, not a line in the column: the refresh after the tap would
        # remove it within a second. app.js shows it for a few seconds.
        response.headers["HX-Trigger"] = json.dumps({"order-notice": line})
    return response


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


@router.post(
    "/bestellungen/{order_id}/storniert/{version}",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(_require_htmx)],
)
def cancel_order_tap(
    request: Request,
    order_id: uuid.UUID,
    version: str,
    session: Session = Depends(get_db),
) -> HTMLResponse:
    """ "Storniert": the button asks first (hx-confirm, docs/06 §1 rule 6)."""
    return _order_tap(
        request,
        session,
        lambda t: order_board.cancel_order(session, t, order_id, version),
        lambda order: (
            f"Bestellung {order.pickup_code or order.customer_name}: Storniert"
        ),
    )


async def _form(request: Request) -> dict[str, str]:
    """Formularfelder eines HTMX-Taps (application/x-www-form-urlencoded).

    Mit der Standardbibliothek statt ueber Form(): das braeuchte python-multipart
    als zusaetzliche Abhaengigkeit, nur um drei Textfelder zu lesen. Async, damit
    der Koerper im Event-Loop gelesen wird und der Endpunkt selbst synchron bleibt.
    """
    body = (await request.body()).decode("utf-8", errors="replace")
    return {k: v[-1] for k, v in parse_qs(body, keep_blank_values=True).items()}


@router.get(
    "/wartezeit/{service}", response_class=HTMLResponse, include_in_schema=False
)
def wartezeit_blatt(
    request: Request, service: str, session: Session = Depends(get_db)
) -> HTMLResponse:
    """The sheet of one wait: today's value and the standard (T-3.9, T-3.14)."""
    tenant = _tenant(session)
    config = session.get(ServiceConfig, tenant.id) if tenant else None
    if service not in WAIT_SERVICES or config is None:
        raise HTTPException(status_code=404)
    name = WAIT_SERVICES[service]
    return templates.TemplateResponse(
        request,
        "fragments/wartezeit.html",
        {
            "key": service,
            "label": service.capitalize(),
            "minutes": current_wait(config, name, clock.utcnow()),
            "standard": standard_wait(config, name),
            "wait_min": switches.MIN_WAIT_MINUTES,
            "wait_max": switches.MAX_WAIT_MINUTES,
        },
    )


def _typed_minutes(text: str) -> int:
    """The digits the team typed; -1 for anything else, which every limit refuses."""
    text = text.strip()
    return int(text) if text.isascii() and text.isdigit() and len(text) <= 4 else -1


@router.post(
    "/kopfzeile/wartezeit/{service}",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(_require_htmx)],
)
def wartezeit_getippt(
    request: Request,
    service: str,
    form: dict[str, str] = Depends(_form),
    session: Session = Depends(get_db),
) -> HTMLResponse:
    if service not in WAIT_SERVICES:
        raise HTTPException(status_code=404)
    minutes = _typed_minutes(form.get("minuten", ""))
    return _switch(
        request,
        session,
        lambda t: switches.set_wait(session, t, WAIT_SERVICES[service], minutes),
    )


@router.post(
    "/kopfzeile/standardzeit/{service}",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(_require_htmx)],
)
def standardzeit(
    request: Request,
    service: str,
    form: dict[str, str] = Depends(_form),
    session: Session = Depends(get_db),
) -> HTMLResponse:
    if service not in WAIT_SERVICES:
        raise HTTPException(status_code=404)
    minutes = _typed_minutes(form.get("minuten", ""))
    return _switch(
        request,
        session,
        lambda t: switches.set_standard_wait(
            session, t, WAIT_SERVICES[service], minutes
        ),
    )


@router.post(
    "/kopfzeile/standardzeit/{service}/zurueck",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(_require_htmx)],
)
def standardzeit_zurueck(
    request: Request, service: str, session: Session = Depends(get_db)
) -> HTMLResponse:
    if service not in WAIT_SERVICES:
        raise HTTPException(status_code=404)
    return _switch(
        request,
        session,
        lambda t: switches.reset_wait(session, t, WAIT_SERVICES[service]),
    )


# After the wait routes: this pattern would take their paths too.
@router.post(
    "/kopfzeile/{service}/{state}",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(_require_htmx)],
)
def dienst(
    request: Request, service: str, state: str, session: Session = Depends(get_db)
) -> HTMLResponse:
    """Abholung, Lieferung and Reservierung on or off."""
    setter = SERVICE_SWITCHES.get(service)
    if setter is None or state not in ("an", "aus"):
        raise HTTPException(status_code=404)
    enabled = state == "an"
    return _switch(request, session, lambda t: setter(session, t, enabled))


def _correction_panel(
    request: Request,
    order_id: uuid.UUID,
    plan: correction.CorrectionPlan | None,
    edit: orders_view.EditState | None,
    message: str | None = None,
    status: int = 200,
    note: str | None = None,
) -> HTMLResponse:
    context: dict = {"order_id": str(order_id), "message": message, "note": note}
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
        orders_view.take_address(edit, form)
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
    if orders_view.take_address(edit, form):
        # Typed after the last tap: the team sees zone, fee and sum first.
        try:
            plan = correction.preview_correction(
                session, tenant.id, order_id, edit.request()
            )
        except AppError as exc:
            return _correction_panel(
                request, order_id, None, None, exc.say or ORDER_FAILED, 409
            )
        return _correction_panel(
            request, order_id, plan, edit, note=orders_view.ADDRESS_TAKEN
        )
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
            {"groups": [], "query": query, "dish_problem": NO_TENANT},
            503,
        )
    return templates.TemplateResponse(
        request,
        template,
        {
            "groups": dishes.group_switches(
                dishes.list_switches(session, tenant.id, query),
                sold_out_first=not query.strip(),
            ),
            "query": query,
            "dish_problem": problem,
        },
    )


def _notice_sheet(
    request: Request,
    session: Session,
    tenant: Tenant,
    message: str | None = None,
    tone: str = "danger",
    status: int = 200,
) -> HTMLResponse:
    notices = reservation_notice.weekday_notices(session, tenant.id)
    return templates.TemplateResponse(
        request,
        "fragments/vorlauf.html",
        {
            "days": [(d, WEEKDAYS[d], minutes) for d, minutes in notices.items()],
            "max_notice": reservation_notice.MAX_NOTICE_MINUTES,
            "message": message,
            "tone": tone,
        },
        status,
    )


@router.get("/vorlauf", response_class=HTMLResponse, include_in_schema=False)
def vorlauf(request: Request, session: Session = Depends(get_db)) -> HTMLResponse:
    """The sheet with the notice per weekday (T-3.13)."""
    tenant = _tenant(session)
    if tenant is None:
        raise HTTPException(status_code=503)
    return _notice_sheet(request, session, tenant)


@router.post(
    "/vorlauf",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(_require_htmx)],
)
def vorlauf_speichern(
    request: Request,
    form: dict[str, str] = Depends(_form),
    session: Session = Depends(get_db),
) -> HTMLResponse:
    tenant = _tenant(session)
    if tenant is None:
        raise HTTPException(status_code=503)
    minutes = {
        int(field.removeprefix("tag-")): _typed_minutes(value)
        for field, value in form.items()
        if field in NOTICE_FIELDS
    }
    try:
        reservation_notice.set_notices(session, tenant.id, minutes)
    except AppError as exc:
        session.rollback()
        logger.warning("Vorlauf nicht gespeichert: %s", exc.message)
        return _notice_sheet(
            request,
            session,
            tenant,
            exc.say or SWITCH_FAILED,
            status=409,
        )
    return _notice_sheet(request, session, tenant, "Gespeichert.", "ok")


# --- "Reservierung eintragen" (T-3.12, D30) --------------------------------------

# The sheet closes on this event and the board says what was entered.
RESERVATION_ENTERED = "reservierung-eingetragen"
ENTRY_INVALID = "Bitte Datum, Uhrzeit, Personen und Namen prüfen."
ENTRY_FAILED = "Das hat nicht geklappt. Bitte nochmal tippen."
ENTRY_NO_SUCH_TIME = "Diese Uhrzeit gibt es an dem Tag nicht (Zeitumstellung)."
ENTRY_FIELDS = ("datum", "uhrzeit", "personen", "name", "telefon", "hinweis", "tisch")


def _seating_choices(session: Session, tenant_id: uuid.UUID) -> list[tuple[str, str]]:
    """Each live table and each pair of the floor plan as (value, label); a pair's
    value is both ids, the smaller first, as the plan stores it."""
    choices = []
    for area in floor_plan.plan(session, tenant_id):
        by_id = {t["id"]: t for t in area["tables"]}
        choices += [
            (t["id"], f"{t['name']} ({t['seats']} Plätze)") for t in by_id.values()
        ]
        for a, b in area["combinations"]:
            first, second = sorted((by_id[a], by_id[b]), key=lambda t: t["name"])
            seats = first["seats"] + second["seats"]
            choices.append(
                (f"{a}+{b}", f"{first['name']} + {second['name']} ({seats} Plätze)")
            )
    return choices


def _entry_request(tenant: Tenant, form: dict[str, str]) -> TabletReservationRequest:
    """The typed form as the domain's request; anything unreadable is one sentence."""
    try:
        day = date.fromisoformat(form.get("datum", "").strip())
        at = dt_time.fromisoformat(form.get("uhrzeit", "").strip())
        local = datetime.combine(day, at, tzinfo=ZoneInfo(tenant.timezone))
        # A time the spring clock change skips would be stored an hour later
        # than the summary shows (Codex #558): it must read back the same.
        exists = local.astimezone(UTC).astimezone(local.tzinfo).time() == at
    except (ValueError, OverflowError) as exc:  # year 1 cannot move to UTC
        raise InvalidInput(f"entry form unreadable: {exc}", say=ENTRY_INVALID) from exc
    if not exists:
        raise InvalidInput("local time does not exist", say=ENTRY_NO_SUCH_TIME)
    try:
        return TabletReservationRequest(
            tenant_id=tenant.id,
            idempotency_key=form.get("schluessel", ""),
            guest_name=form.get("name", ""),
            phone=form.get("telefon") or None,
            party_size=int(form.get("personen", "").strip()),
            reserved_for=local,
            note=form.get("hinweis") or None,
            table_ids=[uuid.UUID(v) for v in form.get("tisch", "").split("+") if v],
        )
    except ValueError as exc:  # pydantic's ValidationError is one too
        raise InvalidInput(f"entry form unreadable: {exc}", say=ENTRY_INVALID) from exc


def _entry_sheet(
    request: Request,
    session: Session,
    tenant: Tenant,
    values: dict[str, str],
    message: str | None = None,
    status: int = 200,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "fragments/reservierung.html",
        {
            "values": values,
            "choices": _seating_choices(session, tenant.id),
            "message": message,
        },
        status,
    )


def _entry_summary(
    request: Request,
    tenant: Tenant,
    draft: TabletDraft,
    message: str | None = None,
    status: int = 200,
    blocked: bool = False,
) -> HTMLResponse:
    tz = tenant.timezone
    local = to_local(draft.reserved_for, tz)
    if draft.window_start is None or draft.window_end is None:
        load = "Zu dieser Zeit gibt es kein Reservierungsfenster."
    else:
        load = (
            f"Fenster {orders_view.clock(draft.window_start, tz)} bis "
            f"{orders_view.clock(draft.window_end, tz)} Uhr: {draft.booked_guests} "
            f"von {draft.max_guests} Plätzen belegt."
        )
    return templates.TemplateResponse(
        request,
        "fragments/reservierung.html",
        {
            "draft": draft,
            "when": f"{WEEKDAYS[local.weekday()]}, {local:%d.%m.%Y}, {local:%H:%M} Uhr",
            "load": load,
            "message": message,
            "blocked": blocked,
        },
        status,
    )


@router.get("/reservierung", response_class=HTMLResponse, include_in_schema=False)
def reservierung(request: Request, session: Session = Depends(get_db)) -> HTMLResponse:
    """The empty sheet: today, a new key per opened sheet (T-3.12)."""
    tenant = _tenant(session)
    if tenant is None:
        raise HTTPException(status_code=503)
    values = {
        "schluessel": uuid.uuid4().hex,
        "datum": business_day(tz_name=tenant.timezone).isoformat(),
    }
    return _entry_sheet(request, session, tenant, values)


@router.post(
    "/reservierung",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(_require_htmx)],
)
def reservierung_pruefen(
    request: Request,
    form: dict[str, str] = Depends(_form),
    session: Session = Depends(get_db),
) -> HTMLResponse:
    """The draft and its summary; a refusal keeps what was typed, and the key."""
    tenant = _tenant(session)
    if tenant is None:
        raise HTTPException(status_code=503)
    values = {f: form.get(f, "") for f in ("schluessel", *ENTRY_FIELDS)}
    try:
        draft = create_tablet_reservation(session, _entry_request(tenant, form))
    except AppError as exc:
        session.rollback()
        logger.info("Reservierung nicht angelegt: %s", exc.message)
        return _entry_sheet(
            request, session, tenant, values, exc.say or ENTRY_FAILED, 409
        )
    return _entry_summary(request, tenant, draft)


@router.post(
    "/reservierung/{reservation_id}/eintragen",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(_require_htmx)],
)
def reservierung_eintragen(
    request: Request,
    reservation_id: uuid.UUID,
    form: dict[str, str] = Depends(_form),
    session: Session = Depends(get_db),
) -> HTMLResponse:
    """The team's confirming tap (`confirm_tablet`); "trotzdem" is the word for a
    full window, asked on the button. Any refusal shows the summary again."""
    tenant = _tenant(session)
    if tenant is None:
        raise HTTPException(status_code=503)
    row = session.get(Reservation, reservation_id)
    if row is None or row.tenant_id != tenant.id or row.source != SOURCE_TABLET:
        raise HTTPException(status_code=404)
    name = row.guest_name
    try:
        confirm_tablet(
            session, tenant.id, reservation_id, override=form.get("trotzdem") == "1"
        )
    except AppError as exc:
        session.rollback()
        logger.info("Reservierung nicht eingetragen: %s", exc.message)
        draft = describe(session, row, ZoneInfo(tenant.timezone), clock.utcnow())
        return _entry_summary(
            request,
            tenant,
            draft,
            exc.say or ENTRY_FAILED,
            409,
            blocked=not isinstance(exc, WindowFull),
        )
    response = HTMLResponse("")
    response.headers["HX-Trigger"] = json.dumps(
        {RESERVATION_ENTERED: f"Reservierung für {name} eingetragen."}
    )
    return response


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
    session: Session = Depends(get_db),
) -> HTMLResponse:
    """One tap: off today or back. Answers with the one row in its new state,
    which the tablet swaps in place, so the list does not move under the finger.

    A dish that is gone (not found) is no error for the team: its row goes.
    Only a database error gets a line in the row.
    """
    if state not in DISH_STATES:
        raise HTTPException(status_code=404)
    tenant = _tenant(session)
    if tenant is None:
        return HTMLResponse("", 503)
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
    row = dishes.get_switch(session, tenant.id, menu_item_id)
    response = (
        HTMLResponse("")
        if row is None
        else templates.TemplateResponse(
            request,
            "fragments/gericht_aus_zeile.html",
            {"d": row, "dish_problem": problem},
        )
    )
    response.headers["HX-Trigger"] = DISHES_CHANGED
    return response


# The floor plan editor (T-3.11, docs/06 §3 "Raumplan"). The browser keeps a
# drag to itself and sends the whole room after each finished step; these routes
# only translate between JSON and domain/reservations/floor_plan.py.
PLAN_INVALID = "Der Plan enthält einen ungültigen Wert und ist nicht gespeichert."


async def _body(request: Request) -> bytes:
    """Read in the event loop, so the route itself stays synchronous (as `_form`)."""
    return await request.body()


def _plan_error(exc: AppError, status: int) -> JSONResponse:
    body = fail_body(exc.code, exc.message, exc.say)
    if isinstance(exc, floor_plan.StaleRevision):
        body["area"] = exc.current
    return JSONResponse(body, status_code=status)


def _plan_write(session: Session, write: Callable[[uuid.UUID], dict]) -> JSONResponse:
    tenant = _tenant(session)
    if tenant is None:
        return JSONResponse(fail_body("not_found", "no tenant", NO_TENANT), 503)
    try:
        return JSONResponse({"ok": True, "area": write(tenant.id)})
    except ValidationError:
        return JSONResponse(fail_body("invalid_input", "plan", PLAN_INVALID), 422)
    except AppError as exc:
        session.rollback()
        logger.info("floor plan not saved: %s", exc.message)
        return _plan_error(exc, exc.status if exc.status >= 400 else 422)


@router.get("/raumplan", response_class=HTMLResponse, include_in_schema=False)
def floor_plan_page(
    request: Request, session: Session = Depends(get_db)
) -> HTMLResponse:
    """The floor plan editor; its data comes from /gui/raumplan/plan."""
    tenant = _tenant(session)
    context: dict = {"tenant": tenant} if tenant else {"problem": NO_TENANT}
    return _page_response(
        request, "betrieb/raumplan.html", context, status_code=200 if tenant else 503
    )


@router.get("/raumplan/plan", include_in_schema=False)
def floor_plan_data(session: Session = Depends(get_db)) -> JSONResponse:
    tenant = _tenant(session)
    if tenant is None:
        return JSONResponse(fail_body("not_found", "no tenant", NO_TENANT), 503)
    return JSONResponse(
        {"ok": True, "areas": floor_plan.plan(session, tenant.id)},
        headers={"Cache-Control": "no-store"},
    )


@router.post(
    "/raumplan/bereiche", include_in_schema=False, dependencies=[Depends(_require_htmx)]
)
def floor_plan_new_area(
    body: bytes = Depends(_body), session: Session = Depends(get_db)
) -> JSONResponse:
    return _plan_write(
        session,
        lambda tenant_id: floor_plan.create_area(
            session, tenant_id, NewArea.model_validate_json(body)
        ),
    )


@router.put(
    "/raumplan/bereiche/{area_id}",
    include_in_schema=False,
    dependencies=[Depends(_require_htmx)],
)
def floor_plan_save(
    area_id: uuid.UUID,
    body: bytes = Depends(_body),
    session: Session = Depends(get_db),
) -> JSONResponse:
    return _plan_write(
        session,
        lambda tenant_id: floor_plan.save_area(
            session, tenant_id, area_id, SaveArea.model_validate_json(body)
        ),
    )


# --- Staff changes and their check cards (#570 part b, D34 point 4) ------------

NO_CHANGE = "Keine Änderung."
PHONE_HELD = "Gilt bis Betriebsschluss. Der Inhaber sieht es auf der Übersicht."
HOURS_HELD = "Gilt sofort. Der Inhaber sieht es auf der Übersicht."
HOURS_WAIT = "Wartet auf den Inhaber: die Karte steht auf der Übersicht."
NO_DATE = "Bitte einen Tag wählen."
NO_TIME = "Bitte Beginn und Ende als Uhrzeit angeben."
CARD_CHANGED = "Die Karte hat sich gerade geändert. Bitte noch einmal prüfen."
# The buttons of a check card -> keep or undo (`domain/status/staff_changes.py`).
CHECK_ANSWERS = {"uebernehmen": True, "zuruecknehmen": False}


def _check_rows(session: Session, tenant: Tenant) -> list[dict]:
    """The staff changes the owner has not decided, from today on, by date."""
    rows = []
    cards = staff.open_cards(session, tenant.id, business_day(tz_name=tenant.timezone))
    holding_on = staff.days_hours(
        session, tenant.id, [c.day for c in cards if c.kind == staff.DAY_HOURS]
    )
    for card in cards:
        if card.kind == staff.TEAM_PHONE:
            title, keep = "Teamnummer heute", "Als Standard übernehmen"
            before, after = card.before["number"], card.after["number"]
            holding = after
        else:
            title = f"Öffnungszeit {WEEKDAYS[card.day.weekday()]}, {card.day:%d.%m.}"
            keep = "Übernehmen"
            before = staff.DayHours.of(card.before).text()
            after = staff.DayHours.of(card.after).text()
            holding = holding_on[card.day].text()
        rows.append(
            {
                "id": card.id,
                "title": title,
                "keep": keep,
                "before": before,
                "after": after,
                "now": holding,
                "waiting": not card.held,
                "seen": card.updated_at.isoformat(),
            }
        )
    return rows


def _checks_fragment(
    request: Request, session: Session, note: str | None = None
) -> HTMLResponse:
    tenant = _tenant(session)
    if tenant is None:
        return templates.TemplateResponse(
            request, "fragments/pruefen.html", {"checks_problem": NO_TENANT}, 503
        )
    return templates.TemplateResponse(
        request,
        "fragments/pruefen.html",
        {"checks": _check_rows(session, tenant), "checks_note": note},
    )


@router.get("/fragments/pruefen", response_class=HTMLResponse, include_in_schema=False)
def checks_fragment(
    request: Request, session: Session = Depends(get_db)
) -> HTMLResponse:
    """Only the check cards: the page fetches them after each event."""
    return _checks_fragment(request, session)


@router.post(
    "/pruefen/{card_id}/{choice}",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(_require_htmx), Depends(require_admin)],
)
def check_answer(
    request: Request,
    card_id: uuid.UUID,
    choice: str,
    form: dict[str, str] = Depends(_form),
    session: Session = Depends(get_db),
) -> HTMLResponse:
    """The owner's answer, behind the admin PIN. A card another tablet
    decided first is no error: the fresh list is the answer. The button sends
    the card version the owner saw (`gesehen`); a card changed since, or a
    button without it, decides nothing and shows the fresh list (#596)."""
    if choice not in CHECK_ANSWERS:
        raise HTTPException(status_code=404)
    try:
        seen = datetime.fromisoformat(form.get("gesehen", ""))
    except ValueError:
        return _checks_fragment(request, session, CARD_CHANGED)
    tenant = _tenant(session)
    if tenant is not None:
        try:
            staff.decide(
                session, tenant.id, card_id, keep=CHECK_ANSWERS[choice], seen=seen
            )
        except Conflict:
            session.rollback()
            return _checks_fragment(request, session, CARD_CHANGED)
        except AppError as exc:
            session.rollback()
            logger.info("Check card not decided: %s", exc.message)
    return _checks_fragment(request, session)


def _phone_sheet(
    request: Request,
    session: Session,
    tenant: Tenant,
    message: str | None = None,
    tone: str = "danger",
    status: int = 200,
) -> HTMLResponse:
    config = session.get_one(ServiceConfig, tenant.id, populate_existing=True)
    holding = staff.team_phone_at(config, clock.utcnow())
    return templates.TemplateResponse(
        request,
        "fragments/teamnummer.html",
        {
            "holding": holding,
            "standard": config.team_phone,
            "choices": [n for n in staff.sheet_numbers() if n != holding],
            "message": message,
            "tone": tone,
        },
        status,
    )


@router.get("/einstellungen", response_class=HTMLResponse, include_in_schema=False)
def settings_sheet(request: Request) -> HTMLResponse:
    """One header button for the staff's settings, so the bar keeps its rows."""
    return templates.TemplateResponse(request, "fragments/einstellungen.html", {})


@router.get("/teamnummer", response_class=HTMLResponse, include_in_schema=False)
def team_phone_sheet(
    request: Request, session: Session = Depends(get_db)
) -> HTMLResponse:
    tenant = _tenant(session)
    if tenant is None:
        raise HTTPException(status_code=503)
    return _phone_sheet(request, session, tenant)


@router.post(
    "/teamnummer",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(_require_htmx)],
)
def team_phone_change(
    request: Request,
    form: dict[str, str] = Depends(_form),
    session: Session = Depends(get_db),
) -> HTMLResponse:
    """Staff, no PIN: today's number, with a check card for the owner."""
    tenant = _tenant(session)
    if tenant is None:
        raise HTTPException(status_code=503)
    try:
        card = staff.change_team_phone(session, tenant.id, form.get("nummer", ""))
    except AppError as exc:
        session.rollback()
        return _phone_sheet(
            request, session, tenant, exc.say or SWITCH_FAILED, status=409
        )
    message = PHONE_HELD if card is not None else NO_CHANGE
    return _phone_sheet(request, session, tenant, message, "ok")


def _hours_sheet(
    request: Request,
    session: Session,
    tenant: Tenant,
    day: date | None = None,
    message: str | None = None,
    tone: str = "danger",
    status: int = 200,
    typed: staff.DayHours | None = None,
) -> HTMLResponse:
    """`typed`: times the staff sent and the domain refused, shown again."""
    today = business_day(tz_name=tenant.timezone)
    day = day if day is not None and day >= today else today
    holding = staff.day_hours(session, tenant.id, day)
    times = typed if typed is not None and typed.opens is not None else holding
    choice = (
        "geschlossen"
        if holding.closed
        else "zeiten"
        if holding.opens is not None
        else "normal"
    )
    return templates.TemplateResponse(
        request,
        "fragments/tageszeiten.html",
        {
            "day": day.isoformat(),
            "today": today.isoformat(),
            "holding": holding.text(),
            "choice": choice,
            "opens": f"{times.opens:%H:%M}" if times.opens else "",
            "closes": f"{times.closes:%H:%M}" if times.closes else "",
            "message": message,
            "tone": tone,
        },
        status,
    )


def _hhmm(text: str) -> dt_time:
    """HH:MM only: a stored second or offset would undo to another time."""
    if len(text) != 5:
        raise ValueError(text)
    # "1000Z" is five characters and 10:00 with an offset: a time with a zone
    # cannot be compared with the stored ones.
    parsed = dt_time.fromisoformat(text)
    if parsed.tzinfo is not None:
        raise ValueError(text)
    return parsed


def _typed_day(text: str) -> date | None:
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


@router.get("/tageszeiten", response_class=HTMLResponse, include_in_schema=False)
def day_hours_sheet(
    request: Request, datum: str = "", session: Session = Depends(get_db)
) -> HTMLResponse:
    tenant = _tenant(session)
    if tenant is None:
        raise HTTPException(status_code=503)
    return _hours_sheet(request, session, tenant, _typed_day(datum))


@router.post(
    "/tageszeiten",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(_require_htmx)],
)
def day_hours_change(
    request: Request,
    form: dict[str, str] = Depends(_form),
    session: Session = Depends(get_db),
) -> HTMLResponse:
    """Staff, no PIN: a date's hours; longer ones for a later date wait."""
    tenant = _tenant(session)
    if tenant is None:
        raise HTTPException(status_code=503)
    day = _typed_day(form.get("datum", ""))
    choice = form.get("art", "")
    if day is None or choice not in ("geschlossen", "zeiten", "normal"):
        return _hours_sheet(request, session, tenant, day, NO_DATE, status=422)
    hours = staff.DayHours(closed=choice == "geschlossen")
    if choice == "zeiten":
        try:
            opens, closes = _hhmm(form.get("von", "")), _hhmm(form.get("bis", ""))
        except ValueError:
            return _hours_sheet(request, session, tenant, day, NO_TIME, status=422)
        hours = staff.DayHours(opens=opens, closes=closes)
    try:
        card = staff.change_day_hours(
            session, tenant.id, day, hours, overnight=bool(form.get("nachts"))
        )
    except AppError as exc:
        session.rollback()
        return _hours_sheet(
            request,
            session,
            tenant,
            day,
            exc.say or SWITCH_FAILED,
            status=409,
            typed=hours,
        )
    if card is None:
        return _hours_sheet(request, session, tenant, day, NO_CHANGE, "ok")
    message = HOURS_HELD if card.held else HOURS_WAIT
    return _hours_sheet(request, session, tenant, day, message, "ok")


# --- The owner's device list (#570 part b, D34 point 6) ---------------------------


def _admin_tenant(session: Session) -> Tenant:
    """The restaurant behind a session that `require_admin` just found: the
    session check resolves it by the same oldest tenant, so it is there."""
    tenant = _tenant(session)
    assert tenant is not None
    return tenant


def _device_rows(session: Session, tenant: Tenant, own: uuid.UUID) -> list[dict]:
    shown = "%d.%m.%Y %H:%M"
    return [
        {
            "id": row.id,
            "name": row.device or "Unbekannter Browser",
            "since": to_local(row.created_at, tenant.timezone).strftime(shown),
            "seen": to_local(row.last_seen_at, tenant.timezone).strftime(shown),
            "this": row.id == own,
        }
        for row in devices(session, tenant.id)
    ]


@router.get("/geraete", response_class=HTMLResponse, include_in_schema=False)
def devices_page(
    request: Request,
    access: Access = Depends(require_admin),
    session: Session = Depends(get_db),
) -> HTMLResponse:
    tenant = _admin_tenant(session)
    rows = _device_rows(session, tenant, access.session_id)
    return _page_response(
        request, "betrieb/geraete.html", {"tenant": tenant, "devices": rows}
    )


@router.post(
    "/geraete/{session_id}/abmelden",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(_require_htmx)],
)
def device_sign_out(
    request: Request,
    session_id: uuid.UUID,
    access: Access = Depends(require_admin),
    session: Session = Depends(get_db),
) -> HTMLResponse:
    """The lost tablet: one device signed out; it needs the password again.
    A device gone already is no error, the fresh list is the answer."""
    tenant = _admin_tenant(session)
    end_sessions(session, tenant.id, "gui:owner", "owner", session_id)
    session.commit()
    if session_id == access.session_id:
        return HTMLResponse("", headers={"HX-Redirect": LOGIN})
    return templates.TemplateResponse(
        request,
        "fragments/geraete.html",
        {"devices": _device_rows(session, tenant, access.session_id)},
    )


@router.get("/events", include_in_schema=False)
def events(
    request: Request, access: Access = Depends(require_gui_session)
) -> StreamingResponse:
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
        today_event_stream(request, tenant_id, tz_name, access.session_id),
        media_type="text/event-stream",
        # Ein puffernder Proxy haelt Ereignisse sonst zurueck, bis genug
        # Bytes zusammen sind - die Live-Ansicht waere dann nicht live.
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
