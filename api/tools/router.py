"""Alle Tool-Endpunkte unter /v1/tools, jeder nur mit Token erreichbar."""

from fastapi import APIRouter, Depends

from api.core.auth import require_token
from api.tools import (
    check_slot,
    confirm,
    create_callback,
    create_reservation,
    get_item_details,
    search_menu,
    service_status,
    transfer_to_team,
)

router = APIRouter(prefix="/v1/tools", dependencies=[Depends(require_token)])
router.include_router(service_status.router)
router.include_router(check_slot.router)
router.include_router(create_reservation.router)
router.include_router(confirm.router)
router.include_router(create_callback.router)
router.include_router(transfer_to_team.router)
router.include_router(search_menu.router)
router.include_router(get_item_details.router)
