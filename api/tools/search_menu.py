"""POST /v1/tools/search_menu. Dünne Hülle um domain.menu.search (docs/04)."""

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from api.core import envelope
from api.core.logging import bind_call_id
from api.db import get_db
from api.domain.menu.search import search_menu
from api.schemas.menu import SearchMenuRequest

router = APIRouter()


@router.post("/search_menu")
def search_menu_tool(
    body: SearchMenuRequest, session: Session = Depends(get_db)
) -> JSONResponse:
    bind_call_id(str(body.call_id))
    result = search_menu(session, body.tenant_id, body.query, body.max_results)
    return envelope.ok(result.model_dump(mode="json"), say=result.say)
