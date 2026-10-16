"""Vertrag des generischen confirm-Tools (docs/04 §confirm)."""

import uuid
from typing import Literal

from pydantic import BaseModel, Field

from api.schemas.common import ToolRequest


class ConfirmRequest(ToolRequest):
    entity: Literal["reservation", "order"]
    entity_id: uuid.UUID
    idempotency_key: str = Field(min_length=1, max_length=128)


class Confirmation(BaseModel):
    status: Literal["confirmed"]
    # queued: geht an die Kueche. awaiting_approval: wartet auf die Freigabe im
    # Tablet, weil der Modus nicht primary ist (docs/02 §Modus).
    handover: Literal["queued", "awaiting_approval"]
    pickup_code: str | None = None
