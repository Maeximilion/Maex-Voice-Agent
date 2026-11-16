"""The ops API of a stack: thin shells around the domain (#571, docs/23 §8).

Own token (`OPS_TOKEN`), held only by the operator dashboard, which reaches the
stack's api over the internal `ops` network; the proxy answers 404 for the path.

`/status` and `/figures` answer with counts, times and codes. `/calls` answers
with what guests did, so it needs a reason and the operator's account
(`require_ops_reason`) and is in the audit log before it answers.
"""

from dataclasses import asdict
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from api.core import envelope
from api.core import time as clock
from api.core.auth import OpsReader, require_ops_reason, require_ops_token
from api.core.errors import NotFound
from api.db import get_db
from api.domain.access.sessions import stack_tenant_id
from api.domain.calls.figures import Figures
from api.domain.calls.log import calls_for_support
from api.domain.status import overview
from api.models import Tenant

router = APIRouter(prefix="/v1/ops", dependencies=[Depends(require_ops_token)])


def _tenant(session: Session) -> Tenant:
    tenant_id = stack_tenant_id(session)
    tenant = session.get(Tenant, tenant_id) if tenant_id else None
    if tenant is None:
        raise NotFound("this stack has no restaurant yet")
    return tenant


def _names(tenant: Tenant) -> dict[str, Any]:
    """Every answer names its restaurant: the dashboard checks that the stack
    it asked is the one that answers."""
    return {"key": tenant.customer_key, "name": tenant.name}


def _figures(figures: Figures) -> dict[str, Any]:
    return asdict(figures) | {
        "accuracy_percent": figures.accuracy_percent,
        "cost_per_order_cents": figures.cost_per_order_cents,
        "handed_over_percent": figures.handed_over_percent,
    }


@router.get("/status")
def status(session: Session = Depends(get_db)) -> JSONResponse:
    tenant = _tenant(session)
    found = overview.status(session, tenant, clock.utcnow())
    data = asdict(found) | {"today": _figures(found.today)}
    del data["tenant_key"], data["tenant_name"]
    return envelope.ok(jsonable_encoder({"tenant": _names(tenant), **data}))


@router.get("/figures")
def figures(session: Session = Depends(get_db)) -> JSONResponse:
    tenant = _tenant(session)
    found = overview.week(session, tenant, clock.utcnow())
    return envelope.ok(
        jsonable_encoder(
            {
                "tenant": _names(tenant),
                "business_day": found.business_day,
                "week": _figures(found.week),
                "days": [{"day": day, **_figures(one)} for day, one in found.days],
            }
        )
    )


@router.get("/calls")
def calls(
    day: date,
    reader: OpsReader = Depends(require_ops_reason),
    session: Session = Depends(get_db),
) -> JSONResponse:
    """The calls of one business day, never a span: no bulk export over this
    API (docs/23 §8)."""
    tenant = _tenant(session)
    names = _names(tenant)
    rows = calls_for_support(
        session,
        tenant,
        day,
        clock.utcnow(),
        actor=reader.actor,
        reason=reader.reason,
        reference=reader.reference,
    )
    return envelope.ok(
        jsonable_encoder(
            {"tenant": names, "business_day": day, "calls": [asdict(r) for r in rows]}
        )
    )
