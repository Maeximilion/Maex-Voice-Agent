"""Anfragen der Druckbruecke an /v1/kitchen (T-4.6, docs/04 §Kuechenbon)."""

import uuid

from pydantic import BaseModel, Field


class ClaimRequest(BaseModel):
    tenant_id: uuid.UUID
    limit: int = Field(default=5, ge=1, le=10)


class AckRequest(BaseModel):
    tenant_id: uuid.UUID
    event_id: uuid.UUID
    ok: bool
    error: str | None = Field(default=None, max_length=500)
