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
    handover: Literal["queued"]
    pickup_code: str | None = None
