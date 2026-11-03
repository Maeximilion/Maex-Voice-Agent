"""What the floor plan editor sends and gets (T-3.11, docs/06 §3 "Raumplan").

A room or terrace travels as one document: its size, its tables, its fixtures
and which tables combine. The limits here are what a request may hold at all;
the rules between fields (a table inside its room, one live "T5") are checked in
`domain/reservations/floor_plan.py`. The counts are bounds against a runaway
request, not agreed limits of a restaurant (assumption, 03.11.2026).
"""

import uuid
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

MAX_TABLES = 200
MAX_FIXTURES = 300
MAX_PAIRS = 1000
# A fixture is named by the browser, which also keeps it across an undo.
FIXTURE_ID = r"^[A-Za-z0-9-]{1,40}$"

Rotation = Literal[0, 90, 180, 270]
Coordinate = Annotated[float, Field(ge=-10, le=5010, allow_inf_nan=False)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Table(_Strict):
    id: uuid.UUID
    name: str = Field(min_length=1, max_length=25)
    shape: Literal["rect", "round"]
    seats: int = Field(ge=1, le=40)
    width_cm: int = Field(ge=30, le=600)
    depth_cm: int = Field(ge=30, le=600)
    x_cm: int = Field(ge=0, le=5000)
    y_cm: int = Field(ge=0, le=5000)
    rotation: Rotation = 0
    end_seat: Literal["left", "right"] = "left"


class Counter(_Strict):
    kind: Literal["counter"]
    id: str = Field(pattern=FIXTURE_ID)
    name: str = Field(min_length=1, max_length=25)
    x_cm: Coordinate
    y_cm: Coordinate
    width_cm: int = Field(ge=30, le=1000)
    depth_cm: int = Field(ge=30, le=1000)
    rotation: Rotation = 0


class Door(_Strict):
    kind: Literal["door"]
    id: str = Field(pattern=FIXTURE_ID)
    name: str = Field(min_length=1, max_length=25)
    side: Literal["top", "right", "bottom", "left"]
    offset_cm: Coordinate
    width_cm: int = Field(ge=50, le=500)


class Wall(_Strict):
    kind: Literal["wall"]
    id: str = Field(pattern=FIXTURE_ID)
    name: str = Field(min_length=1, max_length=25)
    x_cm: Coordinate
    y_cm: Coordinate
    length_cm: float = Field(ge=20, le=7100, allow_inf_nan=False)
    angle: float = Field(ge=-360, le=360, allow_inf_nan=False)
    thickness_cm: int = Field(ge=5, le=60)


class Label(_Strict):
    kind: Literal["label"]
    id: str = Field(pattern=FIXTURE_ID)
    text: str = Field(min_length=1, max_length=60)
    x_cm: Coordinate
    y_cm: Coordinate
    size: Literal["small", "normal", "large", "xlarge"] = "normal"
    # Drawn so it never stands on its head (raumplan-geometry.js, readableTurn).
    rotation: Rotation = 0


Fixture = Annotated[Counter | Door | Wall | Label, Field(discriminator="kind")]


class NewArea(_Strict):
    name: str = Field(min_length=1, max_length=30)
    kind: Literal["inside", "terrace"]
    width_cm: int = Field(ge=200, le=5000)
    depth_cm: int = Field(ge=200, le=5000)


class Area(NewArea):
    shape: Literal["rectangle", "square"]
    wall_cm: int = Field(ge=5, le=60)
    tables: list[Table] = Field(max_length=MAX_TABLES)
    fixtures: list[Fixture] = Field(max_length=MAX_FIXTURES)
    combinations: list[tuple[uuid.UUID, uuid.UUID]] = Field(max_length=MAX_PAIRS)


class SaveArea(_Strict):
    """One save of the editor: the whole room as the tablet holds it, and the
    revision it started from."""

    revision: int = Field(ge=0)
    area: Area
