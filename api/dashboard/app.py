"""HTTP side of the operator dashboard (#571, docs/23 §5).

    uvicorn api.dashboard.app:app --port 8002

Plain forms and no inline script (the CSP of ops.<domain> forbids it); the one
use of HTMX is the customer list, which asks for its rows again every 30
seconds. Every post must come from this site and carry the page's CSRF token,
the sign-in too (login CSRF). Outside dev and test every request but /health must
come through Caddy, which sets the proxy secret; only then is the client
address from X-Forwarded-For trusted. The checks themselves live in
`dashboard/access.py` and `dashboard/passkeys.py`.

A passkey ceremony runs in the browser (`api/gui/ts/passkey.ts`): the page
asks one of the `.../options` routes for a challenge, the device signs it, and
the answer travels in the hidden field `passkey` of an ordinary form post.

`require_step_up` is the one dependency in front of every dangerous action
(docs/23 §5): a route that needs a second factor of the last five minutes
names it and nothing else.
"""

import hmac
import logging
import secrets
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode
from zoneinfo import ZoneInfo

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from starlette.responses import Response

from api.core.logging import (
    configure_logging,
    error_text,
    get_logger,
    log,
    request_context_middleware,
)
from api.core.origin import same_origin
from api.core.tokens import LOCAL_ENVS
from api.dashboard import access, customers, passkeys, push, totp
from api.dashboard.config import settings
from api.dashboard.db import get_db

configure_logging(settings.log_level)
logger = get_logger("api.dashboard.app")

HERE = Path(__file__).resolve().parent
# The stylesheet of the operations view, so both look alike.
GUI_STATIC = HERE.parent / "gui" / "static"
LOGIN = "/login"
HOME = "/"
STEP_UP = "/step-up"
FACTORS = "/factors"
WRITES = frozenset({"POST", "PUT", "PATCH", "DELETE"})
FAILED = "Sign-in failed."
WAIT = "Too many attempts. Try again in {minutes} min."
DEVICE_DAYS = 400
# What the factors page says after a change, by the word in its address: never
# text from the request.
NOTES = {
    "passkey-added": ("ok", "Passkey added."),
    "passkey-removed": ("ok", "Passkey removed."),
    "passkey-refused": (
        "danger",
        "The passkey was not added. Start again; a device can hold one passkey.",
    ),
    "totp-set": ("ok", "The new TOTP secret is in force. The old codes are void."),
    "totp-removed": ("ok", "TOTP removed. Sign in with a passkey from now on."),
    "code-wrong": ("danger", "That code does not belong to the new secret."),
    "factor-needed": (
        "danger",
        "Not removed: the account keeps TOTP or at least two passkeys.",
    ),
}
PUSH_TITLE = "Operator sign-in changed"
JSON_ONCE = {"Cache-Control": "no-store"}
# Times are stored and sent in UTC and shown in the restaurants' time.
SHOWN_IN = ZoneInfo("Europe/Berlin")

app = FastAPI(title="Maex Operator", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=str(GUI_STATIC)), name="static")
templates = Jinja2Templates(directory=str(HERE / "templates"))


def _ago(at: datetime | None, at_now: datetime) -> str:
    """How long ago, in the one unit that says enough for a glance."""
    if at is None:
        return "never"
    minutes = max(0, int((at_now - at).total_seconds() // 60))
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes} min ago"
    if minutes < 48 * 60:
        return f"{minutes // 60} h ago"
    return f"{minutes // (24 * 60)} d ago"


def _shown(at: datetime | None) -> str:
    return at.astimezone(SHOWN_IN).strftime("%d.%m.%Y %H:%M") if at else ""


templates.env.filters["ago"] = _ago
templates.env.filters["shown"] = _shown


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


def _local(path: str) -> str:
    """A path of this site, or the start page: where a step-up leads back to
    is never another site."""
    if (
        path.startswith("/")
        and not path.startswith("//")
        and "\\" not in path
        and path.isascii()
        and path.isprintable()
    ):
        return path
    return HOME


def require_step_up(
    request: Request,
    form: dict[str, str] = Depends(_form),
    operator: access.Operator = Depends(require_operator),
) -> access.Operator:
    """In front of every dangerous action (docs/23 §5): the session showed a
    second factor in the last five minutes. Otherwise the browser goes to the
    step-up page and comes back: to the page itself for a GET, for a write to
    the page its form names in `back` (the write is dropped, never replayed)."""
    if operator.fresh(now()):
        return operator
    if request.method == "GET":
        back = request.url.path
        if request.url.query:
            back += f"?{request.url.query}"
    else:
        back = form.get("back", "")
    raise HTTPException(
        status_code=303,
        headers={"Location": f"{STEP_UP}?{urlencode({'next': _local(back)})}"},
    )


def _alert(db: Session, operator: str, what: str) -> None:
    """Tell the `ops` topic that a way into the dashboard changed. The change
    is committed and audited already; a push that fails is audited too."""
    try:
        push.notify(PUSH_TITLE, f"{operator}: {what}")
    except httpx.HTTPError as problem:
        log(logger, logging.ERROR, "Operator push failed", error=error_text(problem))
        access.audit(db, operator, "operator.push_failed", {"what": what})
        db.commit()


def _refusal(refused: access.SignInRefused) -> str:
    if refused.wait_minutes:
        return WAIT.format(minutes=refused.wait_minutes)
    return FAILED


def _json(body: str) -> Response:
    return Response(body, media_type="application/json", headers=JSON_ONCE)


def watching_operator(request: Request, db: Session = Depends(get_db)) -> None:
    """For what the page asks by itself: the session must still hold, and the
    request does not keep it alive (`access.resolve`). Once it ended, HTMX goes
    to the sign-in."""
    token = request.cookies.get(cookie("session"), "")
    if access.resolve(db, token, now(), touch=False) is None:
        raise HTTPException(
            status_code=401, detail="Signed out", headers={"HX-Redirect": LOGIN}
        )


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
            passkey=form.get("passkey", ""),
            attempt_token=request.cookies.get(cookie("attempt"), ""),
        )
    except access.SignInRefused as refused:
        return _page(request, "login.html", {"message": _refusal(refused)}, 401)
    if signed.factor == "recovery":
        _alert(db, form.get("name", "").strip().lower(), "a recovery code was used")
    response = RedirectResponse(HOME, status_code=303)
    response.delete_cookie(cookie("attempt"), path="/", secure=secure(), httponly=True)
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


@app.post(LOGIN + "/passkey/options")
def login_passkey_options(
    request: Request,
    form: dict[str, str] = Depends(_form),
    db: Session = Depends(get_db),
) -> Response:
    """A challenge for a sign-in with a passkey, for the browser that asks:
    it is kept under a cookie of this attempt and names no credential, so it
    tells nothing about any account."""
    _checked(request, form)
    attempt = request.cookies.get(cookie("attempt"), "")
    if not attempt or not attempt.isascii():
        attempt = secrets.token_urlsafe(access.TOKEN_BYTES)
    response = _json(
        passkeys.assertion_options(
            db, passkeys.for_attempt(attempt), passkeys.SIGN_IN, now()
        )
    )
    response.set_cookie(
        cookie("attempt"),
        attempt,
        max_age=int(passkeys.CHALLENGE_LIFE.total_seconds()),
        path="/",
        httponly=True,
        samesite="strict",
        secure=secure(),
    )
    return response


def _ways(db: Session, operator: access.Operator) -> dict[str, Any]:
    return {
        "operator": operator,
        "has_passkey": passkeys.count(db, operator.account_id) > 0,
        "has_totp": access.has_totp(db, operator.account_id),
    }


@app.get(STEP_UP, response_class=HTMLResponse)
def step_up_page(
    request: Request,
    next: str = HOME,
    operator: access.Operator = Depends(require_operator),
    db: Session = Depends(get_db),
) -> Response:
    return _page(request, "step_up.html", {"next": _local(next), **_ways(db, operator)})


@app.post(STEP_UP, response_class=HTMLResponse)
def step_up_post(
    request: Request,
    form: dict[str, str] = Depends(_form),
    operator: access.Operator = Depends(require_operator),
    db: Session = Depends(get_db),
) -> Response:
    _checked(request, form)
    back = _local(form.get("next", ""))
    try:
        access.step_up(
            db,
            operator,
            form.get("code", ""),
            client_address(request),
            now(),
            device_token=request.cookies.get(cookie("device"), ""),
            passkey=form.get("passkey", ""),
        )
    except access.SignInRefused as refused:
        context = {"next": back, "message": _refusal(refused), **_ways(db, operator)}
        return _page(request, "step_up.html", context, 401)
    return RedirectResponse(back, status_code=303)


@app.post(STEP_UP + "/passkey/options")
def step_up_passkey_options(
    request: Request,
    form: dict[str, str] = Depends(_form),
    operator: access.Operator = Depends(require_operator),
    db: Session = Depends(get_db),
) -> Response:
    _checked(request, form)
    return _json(
        passkeys.assertion_options(
            db,
            passkeys.for_session(operator.session_id),
            passkeys.STEP_UP,
            now(),
            account_id=operator.account_id,
        )
    )


@app.get(FACTORS, response_class=HTMLResponse)
def factors_page(
    request: Request,
    note: str = "",
    operator: access.Operator = Depends(require_operator),
    db: Session = Depends(get_db),
) -> Response:
    """The operator's ways in. Everyone signed in may look; every change, and
    the sight of a new TOTP secret, needs a fresh second factor."""
    fresh = operator.fresh(now())
    pending = access.pending_totp(db, operator) if fresh else None
    rows = passkeys.of_account(db, operator.account_id)
    has_totp = access.has_totp(db, operator.account_id)
    context = {
        "operator": operator,
        "fresh": fresh,
        "passkeys": rows,
        "has_totp": has_totp,
        "passkeys_may_go": has_totp or len(rows) > access.PASSKEYS_WITHOUT_TOTP,
        "totp_may_go": has_totp and len(rows) >= access.PASSKEYS_WITHOUT_TOTP,
        "pending": pending,
        "pending_link": totp.uri(pending, operator.name) if pending else "",
        "recovery_left": access.recovery_left(db, operator.account_id),
        "note": NOTES.get(note),
    }
    response = _page(request, "factors.html", context)
    response.headers.update(JSON_ONCE)
    return response


def _factors(note: str) -> Response:
    return RedirectResponse(f"{FACTORS}?{urlencode({'note': note})}", status_code=303)


@app.post(FACTORS + "/passkeys/options")
def passkey_options(
    request: Request,
    form: dict[str, str] = Depends(_form),
    operator: access.Operator = Depends(require_step_up),
    db: Session = Depends(get_db),
) -> Response:
    _checked(request, form)
    return _json(
        passkeys.registration_options(
            db, operator.account_id, operator.name, operator.session_id, now()
        )
    )


@app.post(FACTORS + "/passkeys")
def passkey_add(
    request: Request,
    form: dict[str, str] = Depends(_form),
    operator: access.Operator = Depends(require_step_up),
    db: Session = Depends(get_db),
) -> Response:
    _checked(request, form)
    try:
        name = access.add_passkey(
            db, operator, form.get("passkey", ""), form.get("name", ""), now()
        )
    except passkeys.Refused:
        return _factors("passkey-refused")
    _alert(db, operator.name, f"new passkey {name!r}")
    return _factors("passkey-added")


@app.post(FACTORS + "/passkeys/remove")
def passkey_remove(
    request: Request,
    form: dict[str, str] = Depends(_form),
    operator: access.Operator = Depends(require_step_up),
    db: Session = Depends(get_db),
) -> Response:
    _checked(request, form)
    try:
        name = access.remove_passkey(db, operator, form.get("id", ""))
    except access.FactorNeeded:
        return _factors("factor-needed")
    except LookupError:
        return _factors("")
    _alert(db, operator.name, f"passkey {name!r} removed")
    return _factors("passkey-removed")


@app.post(FACTORS + "/totp/new")
def totp_new(
    request: Request,
    form: dict[str, str] = Depends(_form),
    operator: access.Operator = Depends(require_step_up),
    db: Session = Depends(get_db),
) -> Response:
    _checked(request, form)
    access.propose_totp(db, operator)
    return _factors("")


@app.post(FACTORS + "/totp")
def totp_confirm(
    request: Request,
    form: dict[str, str] = Depends(_form),
    operator: access.Operator = Depends(require_step_up),
    db: Session = Depends(get_db),
) -> Response:
    _checked(request, form)
    if not access.confirm_totp(db, operator, form.get("code", ""), now()):
        return _factors("code-wrong")
    _alert(db, operator.name, "new TOTP secret")
    return _factors("totp-set")


@app.post(FACTORS + "/totp/remove")
def totp_remove(
    request: Request,
    form: dict[str, str] = Depends(_form),
    operator: access.Operator = Depends(require_step_up),
    db: Session = Depends(get_db),
) -> Response:
    _checked(request, form)
    try:
        access.remove_totp(db, operator)
    except access.FactorNeeded:
        return _factors("factor-needed")
    _alert(db, operator.name, "TOTP removed")
    return _factors("totp-removed")


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
    context = {
        "operator": operator,
        "recovery_left": access.recovery_left(db, operator.account_id),
        "no_passkey": passkeys.count(db, operator.account_id) == 0,
        **_rows(db),
    }
    return _page(request, "home.html", context)


def _rows(db: Session) -> dict[str, Any]:
    return {"rows": customers.overview(customers.stacks(db)), "now": now()}


@app.get(
    "/customers/rows",
    response_class=HTMLResponse,
    dependencies=[Depends(watching_operator)],
)
def customer_rows(request: Request, db: Session = Depends(get_db)) -> Response:
    """The list's rows alone, for its refresh. No cookie is set: `_page` gives
    the CSRF token to pages with a form."""
    return templates.TemplateResponse(request, "_customers.html", _rows(db))
