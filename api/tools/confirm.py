"""POST /v1/tools/confirm. Dünne Hülle um domain.confirm."""

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from api.core import envelope
from api.core.logging import bind_call_id
from api.db import get_db
from api.domain.confirm import confirm
from api.schemas.confirm import ConfirmRequest

router = APIRouter()


@router.post("/confirm")
def confirm_tool(
    body: ConfirmRequest, session: Session = Depends(get_db)
) -> JSONResponse:
    bind_call_id(str(body.call_id))
    result = confirm(session, body)
    return envelope.ok(result.model_dump(mode="json"))
