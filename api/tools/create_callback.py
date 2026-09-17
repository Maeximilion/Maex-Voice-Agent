"""POST /v1/tools/create_callback. Dünne Hülle um domain.callbacks."""

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from api.core import envelope
from api.core.logging import bind_call_id
from api.db import get_db
from api.domain.callbacks import create_callback
from api.schemas.callbacks import CreateCallbackRequest

router = APIRouter()


@router.post("/create_callback")
def create_callback_tool(
    body: CreateCallbackRequest, session: Session = Depends(get_db)
) -> JSONResponse:
    bind_call_id(str(body.call_id))
    task = create_callback(session, body)
    return envelope.ok(task.model_dump(mode="json"), say=task.say)
