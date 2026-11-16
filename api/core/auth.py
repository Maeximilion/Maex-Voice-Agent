"""Token-Prüfung als FastAPI-Dependency. Gilt für alle /v1/tools/*-Endpunkte."""

import hmac
from dataclasses import dataclass

from fastapi import Header, HTTPException

from api.config import LOCAL_ENVS, settings, valid_production_token
from api.core import ops_reason
from api.core.errors import InvalidInput, Unauthorized


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


def require_ops_token(authorization: str = Header(default="")) -> None:
    """Token of the operator dashboard (/v1/ops/*, #571, docs/23 §8).

    Only the dashboard holds it, over the stack's internal `ops` network; the
    proxy answers 404 for the path. Empty closes the route, and outside dev and
    test a weak one does too, as for the agent token.
    """
    token = settings.ops_token
    if (
        not token
        or (settings.env not in LOCAL_ENVS and not valid_production_token(token))
        or not hmac.compare_digest(authorization.encode(), f"Bearer {token}".encode())
    ):
        raise Unauthorized("unauthorized")


@dataclass(frozen=True)
class OpsReader:
    """Who reads guest data over the ops API, and why (docs/23 §8)."""

    # `operator:<account>`, as the audit log names the reader.
    actor: str
    reason: str
    reference: str


def require_ops_reason(
    x_ops_reason: str = Header(default=""), x_ops_actor: str = Header(default="")
) -> OpsReader:
    """In front of every ops route that answers with guest data: a category of
    the fixed list with a ticket number, and the operator's account. The actor
    is believed because only the dashboard holds `OPS_TOKEN`, which the router
    has checked by now."""
    found = ops_reason.parse(x_ops_reason)
    if found is None or not ops_reason.ACTOR.fullmatch(x_ops_actor):
        raise InvalidInput(
            "guest data needs X-Ops-Reason (a listed category and a ticket "
            "number) and X-Ops-Actor",
            status=400,
        )
    return OpsReader(f"operator:{x_ops_actor}", *found)


def require_gui_proxy(x_maex_proxy_secret: str = Header(default="")) -> None:
    """The operations view answers only requests that came through Caddy.

    Caddy's Basic-Auth is the view's only access check, and every container on
    the API's Docker networks reaches api:8000 without it (security audit of
    15.10.2026, #252 finding 6; Asterisk no longer does since T-9.8, docs/13 §3).
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
