"""Alle Tool-Endpunkte unter /v1/tools, jeder nur mit Token erreichbar."""

from fastapi import APIRouter, Depends

from api.core.auth import require_token
from api.tools import service_status

router = APIRouter(prefix="/v1/tools", dependencies=[Depends(require_token)])
router.include_router(service_status.router)
