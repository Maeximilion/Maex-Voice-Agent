"""POST /v1/tools/get_service_status. Dünne Hülle um domain.status."""

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from api.core import envelope
from api.core.logging import bind_call_id
from api.db import get_db
from api.domain.status import get_service_status
from api.schemas.common import ToolRequest

router = APIRouter()


@router.post("/get_service_status")
def get_service_status_tool(
    body: ToolRequest, session: Session = Depends(get_db)
) -> JSONResponse:
    bind_call_id(str(body.call_id))
    status = get_service_status(session, body.tenant_id)
    return envelope.ok(status.model_dump(mode="json"), say=status.say)
