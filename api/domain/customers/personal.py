"""What a row loses when its personal data goes.

One place for the two ways it goes, the deletion job (`jobs/retention.py`) and an
erasure on request (`requests.py`), so a store added to one reaches the other.
"""

from sqlalchemy import ColumnElement, Text, cast, func

from api.models import Order, OutboxEvent

# Required text columns become empty strings: the row stays, no column changes.
ORDER_GONE = {
    "customer_name": "",
    "phone": "",
    "note": None,
    "customer_id": None,
    "address_id": None,
}
CALLBACK_GONE = {"phone": "", "summary": ""}

# The date of an order: the promised time, or its creation for a draft.
ORDER_DAY = func.coalesce(Order.ready_at, Order.created_at)

# What an outbox payload keeps once its period is over: the ids. The kitchen
# finds every slip of an order by them (domain/ordering/handover.py).
OUTBOX_KEEP = ("order_id", "reservation_id", "callback_id", "call_id", "revision")


def thin_payload() -> ColumnElement:
    """The payload of the row reduced to `OUTBOX_KEEP`, as a SQL expression."""
    return func.jsonb_strip_nulls(
        func.jsonb_build_object(
            *(
                arg
                for key in OUTBOX_KEEP
                for arg in (cast(key, Text), OutboxEvent.payload[key])
            )
        )
    )
