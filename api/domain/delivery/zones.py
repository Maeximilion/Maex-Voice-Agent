"""Which zone a postal code falls in (T-6.3, docs/04 §check_delivery, D5).

Says where, not when: whether delivery is open right now stays with
`get_service_status` and `draft_order`. Polygon zones come with T-6.4.
"""

import re
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.core.errors import (
    SAY_ON_FAILURE,
    InvalidInput,
    OutOfZone,
    ServiceUnavailable,
)
from api.models import DeliveryZone
from api.schemas.delivery import DeliveryCheck

POSTAL_CODE = re.compile(r"[0-9]{5}")
SAY_POSTAL_CODE_NOT_UNDERSTOOD = (
    "Die Postleitzahl habe ich nicht verstanden. Können Sie sie noch einmal sagen?"
)
SAY_OUT_OF_ZONE = "Dorthin liefern wir leider nicht. Möchten Sie stattdessen abholen?"


def postal_digits(postal_code: str) -> str:
    """Speech recognition spaces digits out: "7 6 5 3 0"."""
    return "".join(postal_code.split())


def check_delivery(
    session: Session, tenant_id: uuid.UUID, postal_code: str
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
        raise OutOfZone(f"no zone for postal code {code}", say=SAY_OUT_OF_ZONE)
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
