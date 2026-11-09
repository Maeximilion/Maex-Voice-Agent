"""Who may use the operations view: the device session of #570 (D34, docs/23 §6).

Every route of the view depends on `require_gui_session`, in every environment:
without a session a page goes to the sign-in, an HTMX or script request gets 401
with `HX-Redirect`. A locked session (15 minutes without a change) still reads
the board, which stays live (Maxi, 09.11.2026), and its next change goes to the
PIN page. Every change must also come from the view's own origin. The owner's
part of the admin area also needs the admin PIN on this device
(`require_admin`, #570 part b).
"""

from datetime import timedelta
from urllib.parse import quote, urlsplit

from fastapi import Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from api.config import LOCAL_ENVS, settings
from api.core import time as clock
from api.db import get_db
from api.domain.access.sessions import (
    Access,
    Signed,
    mark_active,
    resolve,
    stack_tenant_id,
)

LOGIN = "/gui/anmelden"
UNLOCK = "/gui/entsperren"
OWNER = "/gui/inhaber"
WRITES = frozenset({"POST", "PUT", "PATCH", "DELETE"})
# The server decides when a session ends (30 days without use); the cookie only
# carries the token, so it may outlive it. 400 days is the most a browser keeps.
COOKIE_DAYS = 400


def secure() -> bool:
    return settings.env not in LOCAL_ENVS


def cookie_name(kind: str = "session") -> str:
    # `__Host-` makes the browser refuse the cookie unless it is Secure, for
    # this host only and for path /; dev and test run over plain HTTP.
    return f"__Host-{kind}" if secure() else f"maex_{kind}"


def session_token(request: Request) -> str:
    return request.cookies.get(cookie_name(), "")


def device_token(request: Request) -> str:
    return request.cookies.get(cookie_name("device"), "")


def set_access_cookies(response: Response, signed: Signed) -> None:
    """The session, and the device cookie that keeps the device's own count of
    failed sign-ins (`domain/access/sessions.py`)."""
    for kind, value in (("session", signed.token), ("device", signed.device_token)):
        response.set_cookie(
            cookie_name(kind),
            value,
            max_age=int(timedelta(days=COOKIE_DAYS).total_seconds()),
            path="/",
            httponly=True,
            samesite="lax",
            secure=secure(),
        )


def client_address(request: Request) -> str:
    """The address the sign-in counts failures for. Outside dev and test only
    Caddy reaches the view (`require_gui_proxy`), and Caddy sets
    X-Forwarded-For itself, so its last entry is the client it saw."""
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded and secure():
        return forwarded.rsplit(",", 1)[-1].strip()
    return request.client.host if request.client else ""


def same_origin(request: Request) -> bool:
    """A change comes from a page of this site (docs/23 §6, T9).

    A browser names the page's origin on every cross-site write; "null" and any
    other host are refused. A client that sends neither header is no browser,
    so no cross-site request; the CSRF token still applies to it."""
    origin = request.headers.get("origin")
    if origin is not None:
        return urlsplit(origin).netloc == request.headers.get("host", "")
    return request.headers.get("sec-fetch-site") in (None, "same-origin", "none")


def require_gui_session(request: Request, db: Session = Depends(get_db)) -> Access:
    try:
        return _checked_access(request, db)
    finally:
        # The request's session lives until the response ends, and the event
        # stream runs for hours: no read of this check may keep a pooled
        # connection in an open transaction that long (see `events`).
        db.rollback()


def _checked_access(request: Request, db: Session) -> Access:
    now = clock.utcnow()
    access = resolve(db, session_token(request), stack_tenant_id(db), now)
    if access is None:
        raise _away(request, LOGIN)
    if request.method in WRITES:
        if not same_origin(request):
            raise HTTPException(status_code=403, detail="Only from this site")
        if access.locked:
            raise _away(request, UNLOCK)
        mark_active(db, access.session_id, now)
    return access


def require_admin(
    request: Request, access: Access = Depends(require_gui_session)
) -> Access:
    """The owner's part of the admin area: the device list now, zones, fees and
    weekly hours when their pages come (D34 point 3). Without the admin PIN on
    this device it goes to the PIN page, which leads back here."""
    if not access.admin:
        back = request.url.path
        if request.method != "GET":
            # A write returns to the page it came from, not to its own path.
            back = urlsplit(request.headers.get("hx-current-url", "")).path
        raise _away(request, f"{OWNER}?weiter={quote(back, safe='/')}")
    return access


def _away(request: Request, path: str) -> HTTPException:
    """A page load follows a redirect; HTMX follows `HX-Redirect`, also on a
    401, and the floor plan's script reads it (raumplan.js)."""
    page = (
        request.method == "GET"
        and request.headers.get("hx-request") != "true"
        and "text/html" in request.headers.get("accept", "")
    )
    if page:
        return HTTPException(status_code=303, headers={"Location": path})
    return HTTPException(
        status_code=401, detail="Sign in first", headers={"HX-Redirect": path}
    )
