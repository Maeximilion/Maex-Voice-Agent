"""POST /v1/tools/check_delivery. Thin shell around domain.delivery."""

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from api.core import envelope
from api.core.logging import bind_call_id
from api.db import get_db
from api.domain.delivery import check_delivery
from api.schemas.delivery import CheckDeliveryRequest

router = APIRouter()


@router.post("/check_delivery")
def check_delivery_tool(
    body: CheckDeliveryRequest, session: Session = Depends(get_db)
) -> JSONResponse:
    bind_call_id(str(body.call_id))
    result = check_delivery(session, body.tenant_id, body.postal_code)
    return envelope.ok(result.model_dump(mode="json"))
