"""Which zone a postal code falls in (T-6.3, docs/04 §check_delivery, D5).

Says where, not when: whether delivery is open right now stays with
`get_service_status` and `draft_order`. Polygon zones come with T-6.4.
"""

import re
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.core.errors import (
    SAY_ON_FAILURE,
    InvalidInput,
    OutOfZone,
    ServiceUnavailable,
)
from api.core.time import utcnow
from api.domain.status.service import pickup_open
from api.models import Call, DeliveryZone, ServiceConfig, Tenant
from api.schemas.delivery import DeliveryCheck

POSTAL_CODE = re.compile(r"[0-9]{5}")
SAY_POSTAL_CODE_NOT_UNDERSTOOD = (
    "Die Postleitzahl habe ich nicht verstanden. Können Sie sie noch einmal sagen?"
)
SAY_OUT_OF_ZONE_NO_PICKUP = "Dorthin liefern wir leider nicht."
SAY_OUT_OF_ZONE = f"{SAY_OUT_OF_ZONE_NO_PICKUP} Möchten Sie stattdessen abholen?"


def postal_digits(postal_code: str) -> str:
    """Speech recognition spaces digits out: "7 6 5 3 0"."""
    return "".join(postal_code.split())


def check_delivery(
    session: Session,
    tenant_id: uuid.UUID,
    postal_code: str,
    now: datetime | None = None,
) -> DeliveryCheck:
    code = postal_digits(postal_code)
    if not POSTAL_CODE.fullmatch(code):
        raise InvalidInput(
            f"postal code invalid: {postal_code!r}",
            say=SAY_POSTAL_CODE_NOT_UNDERSTOOD,
        )
    zones = session.scalars(
        select(DeliveryZone).where(
            DeliveryZone.tenant_id == tenant_id,
            DeliveryZone.active.is_(True),
            DeliveryZone.match_type == "postal_code",
            DeliveryZone.postal_codes.contains([code]),
        )
    ).all()
    if not zones:
        raise OutOfZone(
            f"no zone for postal code {code}",
            say=_out_of_zone_say(session, tenant_id, now or utcnow()),
        )
    if len(zones) > 1:
        # The fee would be whichever row came first (hard rule 2).
        raise ServiceUnavailable(
            f"{len(zones)} zones for postal code {code}", say=SAY_ON_FAILURE
        )
    zone = zones[0]
    return DeliveryCheck(
        zone_id=zone.id,
        zone_name=zone.name,
        fee_cents=zone.fee_cents,
        min_order_cents=zone.min_order_cents,
        eta_minutes=zone.eta_minutes,
    )


def _out_of_zone_say(session: Session, tenant_id: uuid.UUID, now: datetime) -> str:
    """No offer of a pickup the order path would refuse (T-3.10, #524)."""
    tenant = session.get(Tenant, tenant_id)
    config = session.get(ServiceConfig, tenant_id, populate_existing=True)
    if tenant is None or config is None:
        return SAY_OUT_OF_ZONE_NO_PICKUP
    if pickup_open(session, tenant_id, config, now, ZoneInfo(tenant.timezone)):
        return SAY_OUT_OF_ZONE
    return SAY_OUT_OF_ZONE_NO_PICKUP


def told_out_of_zone(
    session: Session, tenant_id: uuid.UUID, call_id: uuid.UUID
) -> bool:
    """The last zone answer of this call was `out_of_zone`, from `check_delivery`
    or a delivery `draft_order` (#524). Read from `calls.tool_calls`, which the
    agent and the tool route both write before they answer; a zone that
    `check_delivery` found since takes it back."""
    log = session.scalar(
        select(Call.tool_calls).where(Call.id == call_id, Call.tenant_id == tenant_id)
    )
    for entry in reversed(log or []):
        if entry.get("error_code") == OutOfZone.code:
            return True
        if entry.get("name") == "check_delivery" and entry.get("ok"):
            return False
    return False
