"""POST /v1/tools/find_customer. Thin shell around domain.customers.

The number is the one the call came from, never one in the request (L12,
security audit 02.11.2026), as in the core (`agent/dispatch.py`)."""

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from api.core import envelope
from api.core.logging import bind_call_id
from api.db import get_db
from api.domain.customers import recognize
from api.schemas.customers import FindCustomerRequest

router = APIRouter()


@router.post("/find_customer")
def find_customer_tool(
    body: FindCustomerRequest, session: Session = Depends(get_db)
) -> JSONResponse:
    bind_call_id(str(body.call_id))
    result = recognize(session, body.tenant_id, body.call_id, body.guest_name)
    return envelope.ok(result.model_dump(mode="json"), say=result.say)
