"""Token-Prüfung als FastAPI-Dependency. Gilt für alle /v1/tools/*-Endpunkte."""

import hmac

from fastapi import Header

from api.config import settings
from api.core.errors import Unauthorized


def require_token(authorization: str = Header(default="")) -> None:
    if authorization != f"Bearer {settings.agent_api_token}":
        raise Unauthorized("unauthorized")


def require_kitchen_token(authorization: str = Header(default="")) -> None:
    """Token der Druckbruecke (/v1/kitchen/*). Ohne gesetztes Token ist der Eingang zu."""
    token = settings.kitchen_bridge_token
    if not token or not hmac.compare_digest(
        authorization.encode(), f"Bearer {token}".encode()
    ):
        raise Unauthorized("unauthorized")
