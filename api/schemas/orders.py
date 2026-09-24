"""Vertrag von draft_order (docs/04 §draft_order)."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from api.schemas.common import ToolRequest

# Obergrenze je Position: gegen Hörfehler ("zwanzig" statt "zwei"), nicht gegen
# Großbestellungen. Mehr nimmt das Team entgegen.
MAX_QUANTITY = 30
MAX_ITEMS = 30


class ChosenOption(BaseModel):
    group: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=100)


class OrderItemIn(BaseModel):
    menu_item_id: uuid.UUID
    quantity: int = Field(ge=1, le=MAX_QUANTITY)
    options: list[ChosenOption] = Field(default_factory=list, max_length=10)
    note: str | None = Field(default=None, max_length=200)


class OrderCustomer(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    phone: str = Field(min_length=3, max_length=40)
    # Erst mit Lieferung (T-6.5) ausgewertet.
    address_id: uuid.UUID | None = None


class DraftOrderRequest(ToolRequest):
    idempotency_key: str = Field(min_length=1, max_length=128)
    type: Literal["pickup", "delivery"]
    customer: OrderCustomer
    items: list[OrderItemIn] = Field(min_length=1, max_length=MAX_ITEMS)


class OrderDraft(BaseModel):
    order_id: uuid.UUID
    status: Literal["draft", "confirmed", "approved", "handed_over", "cancelled"]
    items_total_cents: int
    delivery_fee_cents: int
    total_cents: int
    ready_at: datetime | None
    warnings: list[str] = Field(default_factory=list)
    readback: str
