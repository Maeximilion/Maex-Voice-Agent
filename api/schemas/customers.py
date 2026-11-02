"""Contract of the customer tool (docs/04 §find_customer, legal check L12)."""

import uuid

from pydantic import BaseModel, Field

from api.schemas.common import ToolRequest


class FindCustomerRequest(ToolRequest):
    """No number: the one the call came from counts, never one a client sends
    (security audit 02.11.2026). A `caller_id` in the body is ignored."""

    guest_name: str | None = Field(default=None, max_length=200)


class CustomerMatch(BaseModel):
    """Ids and nothing else: what the model is not given it cannot say."""

    found: bool
    customer_id: uuid.UUID | None = None
    address_id: uuid.UUID | None = None
    say: str | None = Field(default=None, exclude=True)
