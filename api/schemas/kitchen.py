"""Anfragen der Druckbruecke an /v1/kitchen (T-4.6, docs/04 §Kuechenbon)."""

import uuid
from typing import Annotated

from pydantic import BaseModel, Field


class ClaimRequest(BaseModel):
    tenant_id: uuid.UUID
    limit: int = Field(default=5, ge=1, le=10)
    # The slip layouts the bridge prints beyond orders ("reservation", #863).
    # A bridge that names none gets orders only.
    layouts: list[Annotated[str, Field(max_length=32)]] = Field(
        default_factory=list, max_length=8
    )


class AckRequest(BaseModel):
    tenant_id: uuid.UUID
    event_id: uuid.UUID
    ok: bool
    error: str | None = Field(default=None, max_length=500)
