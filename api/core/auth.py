"""Token-Prüfung als FastAPI-Dependency. Gilt für alle /v1/tools/*-Endpunkte."""

from fastapi import Header

from api.config import settings
from api.core.errors import Unauthorized


def require_token(authorization: str = Header(default="")) -> None:
    if authorization != f"Bearer {settings.agent_api_token}":
        raise Unauthorized("unauthorized")
