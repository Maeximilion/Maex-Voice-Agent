"""Sign-in, setup link, PIN unlock and admin PIN pages of the operations view
(#570, D34).

Plain forms, no HTMX: each answer is a redirect or the page again with one
sentence. Every post must come from this site and carry the page's CSRF token
in a hidden field, the sign-in too (login CSRF, docs/23 §6). The checks
themselves live in `domain/access/`.
"""

import re
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from api.core import time as clock
from api.core.auth import require_gui_proxy
from api.core.errors import InvalidInput
from api.db import get_db
from api.domain.access.admin import AdminRefused, close_admin, unlock_admin
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
    device_token,
    same_origin,
    session_token,
    set_access_cookies,
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
WRONG_ADMIN_PIN = "Die Inhaber-PIN stimmt nicht."
NO_ADMIN_PIN = "Noch keine Inhaber-PIN. Der Betreiber setzt sie."
# Where the admin PIN page may lead back to: a path of the view, nothing else,
# and not the owner pages themselves (review of #587).
BACK = re.compile(r"/gui/((?!inhaber)(?!.*\.\.)[A-Za-z0-9/._-]*)")


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


@router.get("/anmelden", response_class=HTMLResponse, include_in_schema=False)
def sign_in_page(request: Request, db: Session = Depends(get_db)) -> Response:
    if resolve(db, session_token(request), stack_tenant_id(db), clock.utcnow()):
        return RedirectResponse(BOARD, status_code=303)
    return _page_response(request, "zugang/anmelden.html", {})


@router.post("/anmelden", response_class=HTMLResponse, include_in_schema=False)
def sign_in_post(
    request: Request,
    form: dict[str, str] = Depends(_form),
    db: Session = Depends(get_db),
) -> Response:
    _checked(request, form)
    tenant_id = stack_tenant_id(db)
    if tenant_id is None:
        return _page_response(
            request, "zugang/anmelden.html", {"message": NO_TENANT}, 503
        )
    try:
        signed = sign_in(
            db,
            tenant_id,
            form.get("passwort", ""),
            client_address(request),
            _device(request),
            clock.utcnow(),
            device_token=device_token(request),
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
    response = RedirectResponse(BOARD, status_code=303)
    set_access_cookies(response, signed)
    return response


@router.get("/einrichten", response_class=HTMLResponse, include_in_schema=False)
def setup_page(request: Request) -> Response:
    """The code is in the link's fragment (`#...`), which the browser never
    sends: no access log and no proxy sees it. base.js puts it in the form."""
    return _page_response(request, "zugang/einrichten.html", {"code": ""})


@router.post("/einrichten", response_class=HTMLResponse, include_in_schema=False)
def setup_post(
    request: Request,
    form: dict[str, str] = Depends(_form),
    db: Session = Depends(get_db),
) -> Response:
    _checked(request, form)
    code = form.get("code", "")
    again = {"code": code}
    password, pin = form.get("passwort", ""), form.get("pin", "")
    admin_pin = form.get("admin_pin", "")
    if (
        password != form.get("passwort2")
        or pin != form.get("pin2")
        or admin_pin != form.get("admin_pin2")
    ):
        return _page_response(
            request, "zugang/einrichten.html", {**again, "message": DIFFERENT}, 422
        )
    tenant_id = stack_tenant_id(db)
    try:
        if tenant_id is None:
            raise InvalidInput("no tenant", say=NO_TENANT)
        signed = use_link(
            db,
            tenant_id,
            code,
            password,
            pin,
            _device(request),
            clock.utcnow(),
            device_token=device_token(request),
            admin_pin=admin_pin,
        )
    except InvalidInput as refused:
        return _page_response(
            request, "zugang/einrichten.html", {**again, "message": refused.say}, 422
        )
    response = RedirectResponse(BOARD, status_code=303)
    set_access_cookies(response, signed)
    return response


@router.get("/entsperren", response_class=HTMLResponse, include_in_schema=False)
def unlock_page(request: Request, db: Session = Depends(get_db)) -> Response:
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
) -> Response:
    _checked(request, form)
    now = clock.utcnow()
    access = resolve(db, session_token(request), stack_tenant_id(db), now)
    if access is None:
        return RedirectResponse(LOGIN, status_code=303)
    try:
        left = unlock(db, access.session_id, form.get("pin", ""), now)
    except InvalidInput as refused:
        return _page_response(
            request, "zugang/entsperren.html", {"message": refused.say}, 409
        )
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


# --- The owner's admin PIN (#570 part b, D34 point 3) ---------------------------


def _back(value: str) -> str:
    # Rebuilt behind a constant prefix: the allowlist alone is not visible to CodeQL.
    match = BACK.fullmatch(value)
    return "/gui/" + match.group(1) if match else BOARD


@router.get("/inhaber", response_class=HTMLResponse, include_in_schema=False)
def owner_page(
    request: Request, weiter: str = "", db: Session = Depends(get_db)
) -> Response:
    access = resolve(db, session_token(request), stack_tenant_id(db), clock.utcnow())
    if access is None:
        return RedirectResponse(LOGIN, status_code=303)
    if access.admin:
        return RedirectResponse(_back(weiter), status_code=303)
    return _page_response(request, "zugang/inhaber.html", {"back": _back(weiter)})


@router.post("/inhaber", response_class=HTMLResponse, include_in_schema=False)
def owner_post(
    request: Request,
    form: dict[str, str] = Depends(_form),
    db: Session = Depends(get_db),
) -> Response:
    _checked(request, form)
    now = clock.utcnow()
    access = resolve(db, session_token(request), stack_tenant_id(db), now)
    if access is None:
        return RedirectResponse(LOGIN, status_code=303)
    back = _back(form.get("weiter", ""))
    try:
        unlock_admin(db, access.session_id, form.get("pin", ""), now)
    except AdminRefused as refused:
        if refused.ended:
            return RedirectResponse(LOGIN, status_code=303)
        message = (
            NO_ADMIN_PIN
            if refused.missing
            else WAIT.format(minutes=refused.wait_minutes)
            if refused.wait_minutes
            else WRONG_ADMIN_PIN
        )
        return _page_response(
            request, "zugang/inhaber.html", {"back": back, "message": message}, 401
        )
    return RedirectResponse(back, status_code=303)


@router.post("/inhaber/schliessen", include_in_schema=False)
def owner_close(
    request: Request,
    form: dict[str, str] = Depends(_form),
    db: Session = Depends(get_db),
) -> Response:
    """The owner is done before the hour ends: staff rights on this device."""
    _checked(request, form)
    access = resolve(db, session_token(request), stack_tenant_id(db), clock.utcnow())
    if access is not None:
        close_admin(db, access.session_id)
    return RedirectResponse(BOARD, status_code=303)
