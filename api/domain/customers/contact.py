"""What a contact does to a customer (T-6.2, docs/03): `last_contact_at` starts
the 24-month period again, on an order, a reservation, a callback and a
complaint. Set at the yes, not on a draft.

A callback and a complaint name no person, only the number: every customer on
it counts. An order and a reservation carry a name, so only that customer does.
With an objection on the number nothing moves: recognition is off, and a contact
must not extend the period of the old record (L12).
"""

import uuid
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from api.domain.customers.lookup import objected
from api.domain.menu.numberwords import fold
from api.models import Address, Customer, Order


def note_contact(
    session: Session,
    tenant_id: uuid.UUID,
    phone: str,
    at: datetime,
    name: str | None = None,
) -> None:
    if objected(session, tenant_id, phone):
        return
    for customer in session.scalars(
        select(Customer).where(Customer.tenant_id == tenant_id, Customer.phone == phone)
    ):
        if name is None or fold(customer.name) == fold(name):
            customer.last_contact_at = at


def note_order(session: Session, order: Order, at: datetime) -> None:
    """A confirmed order. Its address becomes the default, the one and only
    "letztes Mal" `find_customer` may ask about: the old one goes first, or the
    unique index stops the second."""
    if order.customer_id is None:
        note_contact(session, order.tenant_id, order.phone, at, order.customer_name)
        return
    # In SQL: the session of a call may hold a count from before another call.
    session.execute(
        update(Customer)
        .where(Customer.id == order.customer_id)
        .values(
            last_contact_at=at,
            last_order_at=at,
            order_count=Customer.order_count + 1,
        )
    )
    if order.address_id is None:
        return
    session.execute(
        update(Address)
        .where(Address.customer_id == order.customer_id, Address.is_default.is_(True))
        .values(is_default=False)
    )
    session.execute(
        update(Address).where(Address.id == order.address_id).values(is_default=True)
    )
