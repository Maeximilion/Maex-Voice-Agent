"""Token-Prüfung als FastAPI-Dependency. Gilt für alle /v1/tools/*-Endpunkte."""

import hmac

from fastapi import Header, HTTPException

from api.config import LOCAL_ENVS, settings, valid_production_token
from api.core.errors import Unauthorized


def token_matches(authorization: str) -> bool:
    """True for the `Authorization` header of the agent token.

    An empty token opens nothing: the expected header would be "Bearer " with
    nothing behind it. Compared in constant time, like the kitchen token below.
    """
    token = settings.agent_api_token
    if not token or (
        settings.env not in LOCAL_ENVS and not valid_production_token(token)
    ):
        return False
    return hmac.compare_digest(authorization.encode(), f"Bearer {token}".encode())


def require_token(authorization: str = Header(default="")) -> None:
    if not token_matches(authorization):
        raise Unauthorized("unauthorized")


def require_kitchen_token(authorization: str = Header(default="")) -> None:
    """Token der Druckbruecke (/v1/kitchen/*).

    Das Token gilt fuer genau einen Betrieb (`KITCHEN_BRIDGE_TENANT_ID`). Fehlt
    Token oder Betrieb, ist der Eingang zu: ein Token ohne Betrieb liesse jede
    Bruecke die Bons jedes Betriebs abholen (Codex PR #143).
    """
    token = settings.kitchen_bridge_token
    if (
        not token
        or not settings.kitchen_bridge_tenant_id
        or not hmac.compare_digest(authorization.encode(), f"Bearer {token}".encode())
    ):
        raise Unauthorized("unauthorized")


def require_gui_proxy(x_maex_proxy_secret: str = Header(default="")) -> None:
    """The operations view answers only requests that came through Caddy.

    Caddy's Basic-Auth is the view's only access check, and every container on
    the Docker network reaches api:8000 without it, Asterisk among them, which
    takes calls from outside (security audit of 15.10.2026, #252 finding 6).
    Caddy sets this header from GUI_PROXY_SECRET on every request it passes on,
    replacing one a client sent. Outside dev and test a missing or weak secret,
    or one equal to the agent token, closes the view; dev has no Caddy.
    """
    if settings.env in LOCAL_ENVS:
        return
    secret = settings.gui_proxy_secret
    if (
        not valid_production_token(secret)
        or secret == settings.agent_api_token
        or not hmac.compare_digest(x_maex_proxy_secret.encode(), secret.encode())
    ):
        raise HTTPException(status_code=403, detail="only through the proxy")
