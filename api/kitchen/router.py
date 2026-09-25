"""POST /v1/kitchen/claim und /v1/kitchen/ack. Duenne Huelle um domain/ordering/handover.

Eigenes Token (`KITCHEN_BRIDGE_TOKEN`): die Bruecke steht auf einem Rechner im
Lokal und soll nur Bons abholen koennen, keine Tools des Agenten rufen.
"""

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from api.config import settings
from api.core import envelope
from api.core.auth import require_kitchen_token
from api.core.errors import Unauthorized
from api.db import get_db
from api.domain.ordering.handover import ack_ticket, claim_tickets
from api.schemas.kitchen import AckRequest, ClaimRequest

router = APIRouter(prefix="/v1/kitchen", dependencies=[Depends(require_kitchen_token)])


def _own_tenant(tenant_id: uuid.UUID) -> None:
    bound = settings.kitchen_bridge_tenant_id
    if bound and str(tenant_id) != bound:
        raise Unauthorized("unauthorized")


@router.post("/claim")
def claim(body: ClaimRequest, session: Session = Depends(get_db)) -> JSONResponse:
    _own_tenant(body.tenant_id)
    tickets = claim_tickets(session, body.tenant_id, datetime.now(UTC), body.limit)
    return envelope.ok({"tickets": tickets})


@router.post("/ack")
def ack(body: AckRequest, session: Session = Depends(get_db)) -> JSONResponse:
    _own_tenant(body.tenant_id)
    status = ack_ticket(
        session, body.tenant_id, body.event_id, body.ok, datetime.now(UTC), body.error
    )
    return envelope.ok({"status": status})
