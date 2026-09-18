"""POST /v1/tools/get_item_details. Dünne Hülle um domain.menu."""

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from api.core import envelope
from api.core.logging import bind_call_id
from api.db import get_db
from api.domain.menu import get_item_details
from api.schemas.menu import ItemDetailsRequest

router = APIRouter()


@router.post("/get_item_details")
def get_item_details_tool(
    body: ItemDetailsRequest, session: Session = Depends(get_db)
) -> JSONResponse:
    bind_call_id(str(body.call_id))
    result = get_item_details(session, body.tenant_id, body.menu_item_id)
    return envelope.ok(result.model_dump(mode="json"), say=result.say)
