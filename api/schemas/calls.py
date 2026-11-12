"""Vertrag des Anruf-Logs (docs/03 §calls, docs/04 §Endpunkte)."""

import uuid
from typing import Literal

from pydantic import BaseModel, Field

from api.schemas.common import ToolRequest

Intent = Literal["reservation", "pickup", "delivery", "info", "complaint", "unknown"]
Outcome = Literal["completed", "transferred", "callback", "abandoned", "error"]
# What was wrong with the line of a call (`calls.line_note`, migration 036).
# `no_caller_audio`: answered, and no audio of the caller arrived (#651).
LineNote = Literal["no_caller_audio"]
# Longer than any number with its formatting; the phone line drops a longer
# caller id instead of refusing the call (#674).
CALLER_ID_MAX_LENGTH = 40


class StartCallRequest(BaseModel):
    tenant_id: uuid.UUID
    external_session_id: str = Field(min_length=1, max_length=200)
    caller_id: str | None = Field(default=None, max_length=CALLER_ID_MAX_LENGTH)


class CallStarted(BaseModel):
    call_id: uuid.UUID


class EndCallRequest(ToolRequest):
    outcome: Outcome
    intent: Intent | None = None
    cost_cents: int | None = Field(default=None, ge=0)
    model: str | None = None
    line_note: LineNote | None = None


class CallEnded(BaseModel):
    call_id: uuid.UUID
    duration_seconds: int
    outcome: Outcome
