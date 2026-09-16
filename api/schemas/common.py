"""Gemeinsame Request-Bausteine der Tool-Verträge (docs/04 §Gemeinsame Regeln)."""

import uuid

from pydantic import BaseModel


class ToolRequest(BaseModel):
    call_id: uuid.UUID
    tenant_id: uuid.UUID
