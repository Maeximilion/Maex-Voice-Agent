"""Contract of the delivery tool (docs/04 §check_delivery)."""

import uuid

from pydantic import BaseModel, Field

from api.schemas.common import ToolRequest


class CheckDeliveryRequest(ToolRequest):
    # No shape here: a code not understood gets its question from the domain.
    postal_code: str = Field(max_length=40)


class DeliveryCheck(BaseModel):
    deliverable: bool = True
    zone_id: uuid.UUID
    zone_name: str
    fee_cents: int
    min_order_cents: int
    eta_minutes: int
