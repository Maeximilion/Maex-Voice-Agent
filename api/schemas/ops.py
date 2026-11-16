"""What the operator dashboard sends with a step (docs/04 §Ops API, #571).

The shapes only: which values a step takes is the domain's decision
(`domain/operator.py`). A PIN is taken as any short text and refused there, so
no validation message is ever built around one.
"""

import uuid
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator


class _Step(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PinShown(_Step):
    which: Literal["team", "admin"]


class PinSet(PinShown):
    pin: str = Field(max_length=16)


class SignOut(_Step):
    """One device, or all of them said in words: a missing field never means
    "every device"."""

    device: uuid.UUID | None = None
    all: StrictBool = False

    @model_validator(mode="after")
    def _one_or_all(self) -> Self:
        if (self.device is None) != self.all:
            raise ValueError("either a device or all")
        return self


class CallMode(_Step):
    mode: str = Field(max_length=20)


class TeamPhone(_Step):
    number: str = Field(max_length=40)
