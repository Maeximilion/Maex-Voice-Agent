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


# The longest kitchen note on an order line.
NOTE_MAX = 200


class ChosenOption(BaseModel):
    group: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=100)


class OrderItemIn(BaseModel):
    menu_item_id: uuid.UUID
    quantity: int = Field(ge=1, le=MAX_QUANTITY)
    options: list[ChosenOption] = Field(default_factory=list, max_length=10)
    note: str | None = Field(default=None, max_length=NOTE_MAX)


class OrderAddress(BaseModel):
    """A delivery address as the guest said it."""

    street: str = Field(min_length=1, max_length=200)
    house_number: str = Field(min_length=1, max_length=20)
    # No shape here: a code not understood gets its question from the domain.
    postal_code: str = Field(max_length=40)
    city: str = Field(min_length=1, max_length=200)
    floor_note: str | None = Field(default=None, max_length=NOTE_MAX)


class OrderCustomer(BaseModel):
    # A delivery to a recognized customer may leave it out: the order then runs
    # under the stored name, which the read-back never says (L12).
    name: str | None = Field(default=None, min_length=1, max_length=200)
    phone: str = Field(min_length=3, max_length=40)
    # The default address `find_customer` gave, after the guest's yes.
    address_id: uuid.UUID | None = None
    # A new address; wins over `address_id`.
    address: OrderAddress | None = None


class DraftOrderRequest(ToolRequest):
    idempotency_key: str = Field(min_length=1, max_length=128)
    type: Literal["pickup", "delivery"]
    customer: OrderCustomer
    items: list[OrderItemIn] = Field(min_length=1, max_length=MAX_ITEMS)
    # A note for the whole order: the guest's allergy said without a dish, or
    # when it is unclear which dish it belongs to (D24).
    note: str | None = Field(default=None, max_length=NOTE_MAX)


OrderStatus = Literal["draft", "confirmed", "approved", "handed_over", "cancelled"]


class OrderDraft(BaseModel):
    order_id: uuid.UUID
    status: OrderStatus
    items_total_cents: int
    delivery_fee_cents: int
    total_cents: int
    ready_at: datetime | None
    warnings: list[str] = Field(default_factory=list)
    readback: str
