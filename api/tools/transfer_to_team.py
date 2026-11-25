"""POST /v1/tools/transfer_to_team. Dünne Hülle um domain.callbacks.transfer."""

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from api.core import envelope
from api.core.logging import bind_call_id
from api.db import get_db
from api.domain.callbacks import transfer_to_team
from api.schemas.transfer import TransferToTeamRequest

router = APIRouter()


@router.post("/transfer_to_team")
def transfer_to_team_tool(
    body: TransferToTeamRequest, session: Session = Depends(get_db)
) -> JSONResponse:
    bind_call_id(str(body.call_id))
    result = transfer_to_team(session, body)
    return envelope.ok(result.model_dump(mode="json"), say=result.say)
