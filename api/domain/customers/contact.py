"""What a contact does to a customer (T-6.2, docs/03): `last_contact_at` starts
the 24-month period again, on an order, a reservation, a callback and a
complaint. Set at the yes, not on a draft.

A callback and a complaint name no person, only the number: every customer on
it counts. An order and a reservation carry a name, so only that customer does.
With an objection on the number nothing moves: recognition is off, and a contact
must not extend the period of the old record (L12).

Only a booking under the line the call came from is a contact of the customers
on that number. A number the guest only names, or a withheld line, moves nobody
(security audit 02.11.2026): a stranger could keep someone's record alive, and a
delivery would make the stranger's address that customer's default.

The default address moves at the team's mark, not at the yes (`settle_default`,
security audit 02.11.2026, P2 no. 4; Maxi 03.11.2026): a caller ID can be
spoofed, so a yes alone must not change the owner's "letztes Mal".

And only a row that is someone's record moves: one with an order counted, or a
blocked one, the rows `find_customer` recognizes. A row only a draft wrote, also
one a booking from another line wrote under this number, keeps its 90 days
(`jobs/retention.py`): the owner's callback must not keep a stranger's name and
address for 24 months (security re-audit 02.11.2026, evening).
"""

import uuid
from datetime import datetime

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from api.domain.customers.lookup import line_of, objected
from api.domain.menu.numberwords import fold
from api.models import Address, Customer, Order


def note_contact(
    session: Session,
    tenant_id: uuid.UUID,
    call_id: uuid.UUID,
    phone: str,
    at: datetime,
    name: str | None = None,
) -> None:
    if phone != line_of(session, tenant_id, call_id) or objected(
        session, tenant_id, phone
    ):
        return
    for customer in session.scalars(
        select(Customer).where(
            Customer.tenant_id == tenant_id,
            Customer.phone == phone,
            or_(Customer.order_count > 0, Customer.blocked.is_(True)),
        )
    ):
        if name is None or fold(customer.name) == fold(name):
            customer.last_contact_at = at


def note_order(session: Session, order: Order, at: datetime) -> None:
    """A confirmed order: its customer's counters and contact. The address
    waits for the team's mark (`settle_default`)."""
    if order.customer_id is None:
        note_contact(
            session,
            order.tenant_id,
            order.call_id,
            order.phone,
            at,
            order.customer_name,
        )
        return
    # Booked under a number the guest only named: the row of the draft gets no
    # order counted, so `find_customer` never offers it.
    if order.phone != line_of(session, order.tenant_id, order.call_id):
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


def settle_default(session: Session, order: Order) -> None:
    """At the mark "In Kasse eingegeben" of a delivery and at its cancel: the
    customer's default, the one and only "letztes Mal" `find_customer` may ask
    about, is the address of their newest delivery the team marked and did not
    cancel, none when no such delivery is left. The newest by creation, not by
    mark: a card marked late does not bring back an address the guest replaced
    since. The old default goes first, or the unique index stops the second."""
    if order.customer_id is None or order.address_id is None:
        return
    # A number the guest only named never makes a default (#489).
    if order.phone != line_of(session, order.tenant_id, order.call_id):
        return
    # Two marks of one customer at once go one after the other: the second
    # sees the first, and the unique index never stops it.
    session.execute(
        select(Customer.id).where(Customer.id == order.customer_id).with_for_update()
    )
    newest = session.scalar(
        select(Order.address_id)
        .where(
            Order.tenant_id == order.tenant_id,
            Order.customer_id == order.customer_id,
            Order.address_id.is_not(None),
            Order.register_entered_at.is_not(None),
            Order.status != "cancelled",
            Order.deleted_at.is_(None),
        )
        .order_by(Order.created_at.desc(), Order.id.desc())
        .limit(1)
    )
    session.execute(
        update(Address)
        .where(Address.customer_id == order.customer_id, Address.is_default.is_(True))
        .values(is_default=False)
    )
    if newest is not None:
        session.execute(
            update(Address).where(Address.id == newest).values(is_default=True)
        )
