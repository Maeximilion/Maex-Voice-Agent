"""POST /v1/tools/draft_order. Dünne Hülle um domain.ordering (docs/04)."""

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from api.core import envelope
from api.core.logging import bind_call_id
from api.db import get_db
from api.domain.ordering import draft_order
from api.schemas.orders import DraftOrderRequest

router = APIRouter()


@router.post("/draft_order")
def draft_order_tool(
    body: DraftOrderRequest, session: Session = Depends(get_db)
) -> JSONResponse:
    bind_call_id(str(body.call_id))
    draft = draft_order(session, body)
    return envelope.ok(draft.model_dump(mode="json"))
