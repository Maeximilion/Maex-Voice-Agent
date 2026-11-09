"""GET /v1/ops/status: thin shell around domain/status/overview.py (#571, docs/23 §8).

Own token (`OPS_TOKEN`), held only by the operator dashboard, which reaches the
stack's api over the internal `ops` network; the proxy answers 404 for the path.
"""

from dataclasses import asdict

from fastapi import APIRouter, Depends
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from api.core import envelope
from api.core import time as clock
from api.core.auth import require_ops_token
from api.core.errors import NotFound
from api.db import get_db
from api.domain.access.sessions import stack_tenant_id
from api.domain.status import overview
from api.models import Tenant

router = APIRouter(prefix="/v1/ops", dependencies=[Depends(require_ops_token)])


@router.get("/status")
def status(session: Session = Depends(get_db)) -> JSONResponse:
    tenant_id = stack_tenant_id(session)
    tenant = session.get(Tenant, tenant_id) if tenant_id else None
    if tenant is None:
        raise NotFound("this stack has no restaurant yet")
    found = overview.status(session, tenant, clock.utcnow())
    today = found.today
    data = asdict(found)
    names = {"key": data.pop("tenant_key"), "name": data.pop("tenant_name")}
    data["today"] |= {
        "accuracy_percent": today.accuracy_percent,
        "cost_per_order_cents": today.cost_per_order_cents,
        "handed_over_percent": today.handed_over_percent,
    }
    return envelope.ok(jsonable_encoder({"tenant": names, **data}))
