"""The ops API of a stack: thin shells around the domain (#571, docs/23 §8).

Own token (`OPS_TOKEN`), held only by the operator dashboard, which reaches the
stack's api over the internal `ops` network; the proxy answers 404 for the path.

`/status` and `/figures` answer with counts, times and codes. `/calls` answers
with what guests did, so it needs a reason and the operator's account
(`require_ops_reason`) and is in the audit log before it answers.

`/access` and the steps behind it (both PINs, devices, a setup link) and
`/call-mode` and `/team-phone` show a secret or change the restaurant. Each
needs the reason and the account too, and the key of the customer the dashboard
means (`_customer`): a stack that is not that customer's refuses before
anything is read or written. Each answers with the state after the step
(`domain/operator.py` `access`), which holds no secret; a shown PIN and a new
setup link are the only secrets an answer carries, once.
"""

from dataclasses import asdict
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, Header
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from api.core import envelope
from api.core import time as clock
from api.core.auth import require_ops_reason, require_ops_token
from api.core.errors import Conflict, NotFound
from api.core.ops_reason import OpsReader
from api.db import get_db
from api.domain import operator
from api.domain.access.sessions import stack_tenant_id
from api.domain.calls.figures import Figures
from api.domain.calls.log import calls_for_support
from api.domain.status import overview
from api.models import Tenant
from api.schemas.ops import CallMode, PinSet, PinShown, SignOut, TeamPhone

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


def _customer(
    x_ops_customer: str = Header(default=""), session: Session = Depends(get_db)
) -> Tenant:
    """The stack's restaurant, when it is the customer the dashboard means.
    A restaurant without a key is nobody's: nothing ties it to that customer."""
    tenant = _tenant(session)
    if not tenant.customer_key or tenant.customer_key != x_ops_customer:
        raise Conflict("this stack is not that customer's", status=409)
    return tenant


def _access(session: Session, tenant: Tenant, **shown: Any) -> JSONResponse:
    """The state after a step, and what the step alone shows."""
    found = operator.access(session, tenant, clock.utcnow())
    return envelope.ok(
        jsonable_encoder({"tenant": _names(tenant), **asdict(found), **shown})
    )


@router.get("/access")
def access(
    by: OpsReader = Depends(require_ops_reason),
    tenant: Tenant = Depends(_customer),
    session: Session = Depends(get_db),
) -> JSONResponse:
    found = operator.read_access(session, tenant, clock.utcnow(), by)
    return envelope.ok(jsonable_encoder({"tenant": _names(tenant), **asdict(found)}))


@router.post("/access/pin/show")
def pin_show(
    body: PinShown,
    by: OpsReader = Depends(require_ops_reason),
    tenant: Tenant = Depends(_customer),
    session: Session = Depends(get_db),
) -> JSONResponse:
    """A post: the look is written down, and nothing may repeat or keep it."""
    pin = operator.show_pin(session, tenant, body.which, by)
    return _access(session, tenant, pin=pin)


@router.post("/access/pin")
def pin_set(
    body: PinSet,
    by: OpsReader = Depends(require_ops_reason),
    tenant: Tenant = Depends(_customer),
    session: Session = Depends(get_db),
) -> JSONResponse:
    operator.set_pin(session, tenant, body.which, body.pin, by)
    return _access(session, tenant)


@router.post("/access/sign-out")
def sign_out(
    body: SignOut,
    by: OpsReader = Depends(require_ops_reason),
    tenant: Tenant = Depends(_customer),
    session: Session = Depends(get_db),
) -> JSONResponse:
    ended = operator.sign_out(session, tenant, body.device, by)
    return _access(session, tenant, signed_out=ended)


@router.post("/access/setup-link")
def setup_link(
    by: OpsReader = Depends(require_ops_reason),
    tenant: Tenant = Depends(_customer),
    session: Session = Depends(get_db),
) -> JSONResponse:
    link, expires_at = operator.setup_link(session, tenant, clock.utcnow(), by)
    return _access(session, tenant, link=link, link_expires_at=expires_at)


@router.post("/call-mode")
def call_mode(
    body: CallMode,
    by: OpsReader = Depends(require_ops_reason),
    tenant: Tenant = Depends(_customer),
    session: Session = Depends(get_db),
) -> JSONResponse:
    operator.set_call_mode(session, tenant, body.mode, clock.utcnow(), by)
    return _access(session, tenant)


@router.post("/team-phone")
def team_phone(
    body: TeamPhone,
    by: OpsReader = Depends(require_ops_reason),
    tenant: Tenant = Depends(_customer),
    session: Session = Depends(get_db),
) -> JSONResponse:
    operator.set_team_phone(session, tenant, body.number, clock.utcnow(), by)
    return _access(session, tenant)
