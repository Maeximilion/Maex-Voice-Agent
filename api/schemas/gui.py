"""Verträge der Betriebsansicht (docs/06 §3). Keine Tool-Verträge, nur Anzeigedaten."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class TodayReservation(BaseModel):
    """Eine Zeile der Spalte "Heute"."""

    reservation_id: uuid.UUID
    status: Literal["confirmed"]
    reserved_for: datetime
    party_size: int
    guest_name: str
    phone: str
    note: str | None = None
    # The tables of the floor plan the team seated it on, by name (T-3.12).
    tables: list[str] = Field(default_factory=list)
