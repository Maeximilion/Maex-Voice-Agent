"""Token-Prüfung als FastAPI-Dependency. Gilt für alle /v1/tools/*-Endpunkte."""

import hmac

from fastapi import Header

from api.config import settings, token_usable
from api.core.errors import Unauthorized


def require_token(authorization: str = Header(default="")) -> None:
    """A token that is blank, or outside a workbench still the placeholder of
    `.env.example`, opens nothing: `Bearer ` would match an empty one, and the
    placeholder is public. The settings refuse to load with such a token on a
    server (`api/config.py`); the check is repeated here so the door does not
    depend on how the settings were built (audit 12.10.2026)."""
    token = settings.agent_api_token
    if not token_usable(token, settings.env) or not hmac.compare_digest(
        authorization.encode(), f"Bearer {token}".encode()
    ):
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
