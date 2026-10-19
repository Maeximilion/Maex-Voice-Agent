"""POST /v1/tools/check_slot. Dünne Hülle um domain.reservations."""

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from api.core import envelope
from api.core.logging import bind_call_id
from api.db import get_db
from api.domain.reservations import check_slot
from api.schemas.reservations import CheckSlotRequest

router = APIRouter()


@router.post("/check_slot")
def check_slot_tool(
    body: CheckSlotRequest, session: Session = Depends(get_db)
) -> JSONResponse:
    bind_call_id(str(body.call_id))
    result = check_slot(session, body.tenant_id, body.reserved_for, body.party_size)
    return envelope.ok(result.model_dump(mode="json"), say=result.say)
