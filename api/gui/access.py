"""Sign-in, setup link and PIN unlock pages of the operations view (#570, D34).

Plain forms, no HTMX: each answer is a redirect or the page again with one
sentence. Every post must come from this site and carry the page's CSRF token
in a hidden field, the sign-in too (login CSRF, docs/23 §6). The checks
themselves live in `domain/access/`.
"""

import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from api.core import time as clock
from api.core.auth import require_gui_proxy
from api.core.errors import InvalidInput
from api.db import get_db
from api.domain.access.sessions import (
    SignInRefused,
    resolve,
    sign_in,
    stack_tenant_id,
    unlock,
)
from api.domain.access.setup import use_link
from api.gui.router import NO_TENANT, _form, _page_response
from api.gui.session import (
    LOGIN,
    client_address,
    same_origin,
    session_token,
    set_session_cookie,
)

router = APIRouter(
    prefix="/gui", tags=["gui"], dependencies=[Depends(require_gui_proxy)]
)

BOARD = "/gui/"
WRONG_PASSWORD = "Das Passwort stimmt nicht."
WAIT = "Zu viele Versuche. Bitte in {minutes} Minuten nochmal."
DIFFERENT = "Die beiden Eingaben sind nicht gleich."
WRONG_PIN = "Die PIN stimmt nicht. Noch {left} Versuche."
PIN_ENDED = "Fünfmal die falsche PIN: das Gerät ist abgemeldet. Bitte mit dem Passwort anmelden."


def _checked(request: Request, form: dict[str, str]) -> None:
    cookie = request.cookies.get("csrf_token", "")
    sent = form.get("csrf_token", "")
    if (
        not same_origin(request)
        or not cookie
        or not sent.isascii()
        or not cookie.isascii()
        or not secrets.compare_digest(cookie, sent)
    ):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")


def _device(request: Request) -> str:
    return request.headers.get("user-agent", "")


def _signed_in(response: RedirectResponse, token: str) -> RedirectResponse:
    set_session_cookie(response, token)
    return response


@router.get("/anmelden", response_class=HTMLResponse, include_in_schema=False)
def sign_in_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    if resolve(db, session_token(request), stack_tenant_id(db), clock.utcnow()):
        return RedirectResponse(BOARD, status_code=303)
    return _page_response(request, "zugang/anmelden.html", {})


@router.post("/anmelden", response_class=HTMLResponse, include_in_schema=False)
def sign_in_post(
    request: Request,
    form: dict[str, str] = Depends(_form),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    _checked(request, form)
    tenant_id = stack_tenant_id(db)
    if tenant_id is None:
        return _page_response(
            request, "zugang/anmelden.html", {"message": NO_TENANT}, 503
        )
    try:
        token = sign_in(
            db,
            tenant_id,
            form.get("passwort", ""),
            client_address(request),
            _device(request),
            clock.utcnow(),
        )
    except SignInRefused as refused:
        message = (
            WAIT.format(minutes=refused.wait_minutes)
            if refused.wait_minutes
            else WRONG_PASSWORD
        )
        return _page_response(
            request, "zugang/anmelden.html", {"message": message}, 401
        )
    return _signed_in(RedirectResponse(BOARD, status_code=303), token)


@router.get("/einrichten", response_class=HTMLResponse, include_in_schema=False)
def setup_page(request: Request) -> HTMLResponse:
    """The code is in the link's fragment (`#...`), which the browser never
    sends: no access log and no proxy sees it. base.js puts it in the form."""
    return _page_response(request, "zugang/einrichten.html", {"code": ""})


@router.post("/einrichten", response_class=HTMLResponse, include_in_schema=False)
def setup_post(
    request: Request,
    form: dict[str, str] = Depends(_form),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    _checked(request, form)
    code = form.get("code", "")
    again = {"code": code}
    password, pin = form.get("passwort", ""), form.get("pin", "")
    if password != form.get("passwort2") or pin != form.get("pin2"):
        return _page_response(
            request, "zugang/einrichten.html", {**again, "message": DIFFERENT}, 422
        )
    tenant_id = stack_tenant_id(db)
    try:
        if tenant_id is None:
            raise InvalidInput("no tenant", say=NO_TENANT)
        token = use_link(
            db, tenant_id, code, password, pin, _device(request), clock.utcnow()
        )
    except InvalidInput as refused:
        return _page_response(
            request, "zugang/einrichten.html", {**again, "message": refused.say}, 422
        )
    return _signed_in(RedirectResponse(BOARD, status_code=303), token)


@router.get("/entsperren", response_class=HTMLResponse, include_in_schema=False)
def unlock_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    access = resolve(db, session_token(request), stack_tenant_id(db), clock.utcnow())
    if access is None:
        return RedirectResponse(LOGIN, status_code=303)
    if not access.locked:
        return RedirectResponse(BOARD, status_code=303)
    return _page_response(request, "zugang/entsperren.html", {})


@router.post("/entsperren", response_class=HTMLResponse, include_in_schema=False)
def unlock_post(
    request: Request,
    form: dict[str, str] = Depends(_form),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    _checked(request, form)
    now = clock.utcnow()
    access = resolve(db, session_token(request), stack_tenant_id(db), now)
    if access is None:
        return RedirectResponse(LOGIN, status_code=303)
    left = unlock(db, access.session_id, form.get("pin", ""), now)
    if left is None:
        return RedirectResponse(BOARD, status_code=303)
    if left == 0:
        return _page_response(
            request, "zugang/anmelden.html", {"message": PIN_ENDED}, 401
        )
    return _page_response(
        request,
        "zugang/entsperren.html",
        {"message": WRONG_PIN.format(left=left)},
        401,
    )
