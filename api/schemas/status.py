"""Antwortdaten von get_service_status (docs/04)."""

from datetime import datetime

from pydantic import BaseModel, Field


class SoldOutItem(BaseModel):
    number: str
    name: str


class ServiceStatus(BaseModel):
    is_open: bool
    closes_at: datetime | None
    pickup_enabled: bool
    delivery_enabled: bool
    pickup_wait_minutes: int
    delivery_wait_minutes: int
    sold_out: list[SoldOutItem] = Field(default_factory=list)
    call_mode: str
    # Vorlesesatz gehört in die Hülle, nicht in data.
    say: str | None = Field(default=None, exclude=True)
