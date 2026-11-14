"""HTTP side of the operator dashboard (#571, docs/23 §5).

    uvicorn api.dashboard.app:app --port 8002

Plain forms, no HTMX and no inline script (the CSP of ops.<domain> forbids it).
Every post must come from this site and carry the page's CSRF token, the
sign-in too (login CSRF). Outside dev and test every request but /health must
come through Caddy, which sets the proxy secret; only then is the client
address from X-Forwarded-For trusted. The checks themselves live in
`dashboard/access.py`.
"""

import hmac
import secrets
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from starlette.responses import Response

from api.core.logging import configure_logging, request_context_middleware
from api.core.origin import same_origin
from api.core.tokens import LOCAL_ENVS
from api.dashboard import access
from api.dashboard.config import settings
from api.dashboard.db import get_db

configure_logging(settings.log_level)

HERE = Path(__file__).resolve().parent
# The stylesheet of the operations view, so both look alike.
GUI_STATIC = HERE.parent / "gui" / "static"
LOGIN = "/login"
HOME = "/"
WRITES = frozenset({"POST", "PUT", "PATCH", "DELETE"})
FAILED = "Sign-in failed."
WAIT = "Too many attempts. Try again in {minutes} min."
DEVICE_DAYS = 400

app = FastAPI(title="Maex Operator", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=str(GUI_STATIC)), name="static")
templates = Jinja2Templates(directory=str(HERE / "templates"))


def now() -> datetime:
    return datetime.now(UTC)


def secure() -> bool:
    return settings.env not in LOCAL_ENVS


def cookie(kind: str) -> str:
    # `__Host-`: Secure, this host only, path /. Dev and test run over plain HTTP.
    return f"__Host-ops_{kind}" if secure() else f"ops_{kind}"


@app.middleware("http")
async def _through_the_proxy(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Every customer's api shares a network with the dashboard (docs/23 §8);
    none of them may reach the sign-in past Caddy."""
    if (
        secure()
        and request.url.path != "/health"
        and not hmac.compare_digest(
            request.headers.get("x-maex-proxy-secret", "").encode(),
            settings.operator_proxy_secret.encode(),
        )
    ):
        return PlainTextResponse("only through the proxy", status_code=403)
    return await call_next(request)


app.middleware("http")(request_context_middleware)


def client_address(request: Request) -> str:
    """Caddy sets X-Forwarded-For itself; its last entry is the client it saw.
    Trusted only outside dev and test, where only Caddy gets in."""
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded and secure():
        return forwarded.rsplit(",", 1)[-1].strip()
    return request.client.host if request.client else ""


async def _form(request: Request) -> dict[str, str]:
    """Read in the event loop, so the routes stay synchronous (the scrypt
    check runs on the thread pool, not in the loop); as gui/router.py."""
    body = (await request.body()).decode("utf-8", errors="replace")
    return {k: v[-1] for k, v in parse_qs(body, keep_blank_values=True).items()}


def _page(
    request: Request, template: str, context: dict[str, Any], status_code: int = 200
) -> HTMLResponse:
    """A page with the CSRF token its form posts back, and the cookie it is
    compared with."""
    token = request.cookies.get(cookie("csrf"), "")
    if not token or not token.isascii():
        token = secrets.token_urlsafe(32)
    response = templates.TemplateResponse(
        request, template, {**context, "csrf_token": token}, status_code=status_code
    )
    response.set_cookie(
        cookie("csrf"),
        token,
        path="/",
        httponly=True,
        samesite="strict",
        secure=secure(),
    )
    return response


def _checked(request: Request, form: dict[str, str]) -> None:
    sent, kept = form.get("csrf_token", ""), request.cookies.get(cookie("csrf"), "")
    if (
        not same_origin(request)
        or not kept
        or not sent.isascii()
        or not kept.isascii()
        or not secrets.compare_digest(sent, kept)
    ):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")


def require_operator(
    request: Request, db: Session = Depends(get_db)
) -> access.Operator:
    operator = access.resolve(db, request.cookies.get(cookie("session"), ""), now())
    if operator is None:
        if request.method == "GET":
            raise HTTPException(status_code=303, headers={"Location": LOGIN})
        raise HTTPException(status_code=401, detail="Signed out")
    if request.method in WRITES and not same_origin(request):
        raise HTTPException(status_code=403, detail="Only from this site")
    return operator


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get(LOGIN, response_class=HTMLResponse)
def login_page(request: Request, db: Session = Depends(get_db)) -> Response:
    if access.resolve(db, request.cookies.get(cookie("session"), ""), now()):
        return RedirectResponse(HOME, status_code=303)
    return _page(request, "login.html", {})


@app.post(LOGIN, response_class=HTMLResponse)
def login_post(
    request: Request,
    form: dict[str, str] = Depends(_form),
    db: Session = Depends(get_db),
) -> Response:
    _checked(request, form)
    try:
        signed = access.sign_in(
            db,
            form.get("name", ""),
            form.get("password", ""),
            form.get("code", ""),
            client_address(request),
            now(),
            device_token=request.cookies.get(cookie("device"), ""),
            previous_token=request.cookies.get(cookie("session"), ""),
        )
    except access.SignInRefused as refused:
        message = (
            WAIT.format(minutes=refused.wait_minutes)
            if refused.wait_minutes
            else FAILED
        )
        return _page(request, "login.html", {"message": message}, 401)
    response = RedirectResponse(HOME, status_code=303)
    # No max-age: the server ends the session (30 minutes idle, 12 hours).
    response.set_cookie(
        cookie("session"),
        signed.token,
        path="/",
        httponly=True,
        samesite="strict",
        secure=secure(),
    )
    response.set_cookie(
        cookie("device"),
        signed.device_token,
        max_age=int(timedelta(days=DEVICE_DAYS).total_seconds()),
        path="/",
        httponly=True,
        samesite="strict",
        secure=secure(),
    )
    return response


@app.post("/logout")
def logout(
    request: Request,
    form: dict[str, str] = Depends(_form),
    operator: access.Operator = Depends(require_operator),
    db: Session = Depends(get_db),
) -> Response:
    _checked(request, form)
    access.sign_out(db, operator)
    response = RedirectResponse(LOGIN, status_code=303)
    response.delete_cookie(cookie("session"), path="/", secure=secure(), httponly=True)
    return response


@app.get(HOME, response_class=HTMLResponse)
def home(
    request: Request,
    operator: access.Operator = Depends(require_operator),
    db: Session = Depends(get_db),
) -> Response:
    left = access.recovery_left(db, operator.account_id)
    return _page(request, "home.html", {"operator": operator, "recovery_left": left})
