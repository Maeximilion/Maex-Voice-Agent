"""Vertrag des Rückruf-Tools (docs/04 §create_callback)."""

import uuid
from typing import Literal

from pydantic import BaseModel, Field

from api.schemas.common import ToolRequest

CallbackReason = Literal[
    "complaint", "not_understood", "human_requested", "out_of_scope"
]


class CreateCallbackRequest(ToolRequest):
    phone: str = Field(min_length=3, max_length=40)
    reason: CallbackReason
    summary: str = Field(min_length=1, max_length=1000)


class CallbackTask(BaseModel):
    callback_id: uuid.UUID
    status: Literal["open", "done"]
    phone: str
    reason: CallbackReason
    summary: str
    say: str | None = Field(default=None, exclude=True)
