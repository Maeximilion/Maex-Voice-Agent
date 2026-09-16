"""POST /v1/tools/create_reservation. Dünne Hülle um domain.reservations.create."""

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from api.core import envelope
from api.core.logging import bind_call_id
from api.db import get_db
from api.domain.reservations import create_reservation
from api.schemas.reservations import CreateReservationRequest

router = APIRouter()


@router.post("/create_reservation")
def create_reservation_tool(
    body: CreateReservationRequest, session: Session = Depends(get_db)
) -> JSONResponse:
    bind_call_id(str(body.call_id))
    result = create_reservation(
        session,
        body.tenant_id,
        body.call_id,
        body.idempotency_key,
        body.guest_name,
        body.phone,
        body.party_size,
        body.reserved_for,
        body.note,
    )
    return envelope.ok(result.model_dump(mode="json"))
