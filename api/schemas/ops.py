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


class _Change(_Step):
    """A step that changes the restaurant: it carries the version of the page
    it came from (`domain/operator.py` `Access.version`, #811). Required: a
    step without it could act on any page."""

    seen: str = Field(min_length=1, max_length=64)


class PinShown(_Step):
    which: Literal["team", "admin"]


class PinSet(_Change):
    which: Literal["team", "admin"]
    pin: str = Field(max_length=16)


class SetupLink(_Change):
    pass


class SignOut(_Change):
    """One device, or all of them said in words: a missing field never means
    "every device"."""

    device: uuid.UUID | None = None
    all: StrictBool = False

    @model_validator(mode="after")
    def _one_or_all(self) -> Self:
        if (self.device is None) != self.all:
            raise ValueError("either a device or all")
        return self


class CallMode(_Change):
    mode: str = Field(max_length=20)


class TeamPhone(_Change):
    """Today's number, or the standard number (#811): the same shape."""

    number: str = Field(max_length=40)
