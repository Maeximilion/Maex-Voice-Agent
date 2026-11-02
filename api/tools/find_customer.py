"""POST /v1/tools/find_customer. Thin shell around domain.customers."""

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from api.core import envelope
from api.core.logging import bind_call_id
from api.db import get_db
from api.domain.customers import find_customer
from api.schemas.customers import FindCustomerRequest

router = APIRouter()


@router.post("/find_customer")
def find_customer_tool(
    body: FindCustomerRequest, session: Session = Depends(get_db)
) -> JSONResponse:
    bind_call_id(str(body.call_id))
    result = find_customer(session, body.tenant_id, body.caller_id, body.guest_name)
    return envelope.ok(result.model_dump(mode="json"), say=result.say)
