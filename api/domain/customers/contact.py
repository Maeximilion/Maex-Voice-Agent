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

The default address moves at the team's mark, not at the yes (`default_at_mark`,
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
    waits for the team's mark (`default_at_mark`)."""
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


def default_at_mark(session: Session, order: Order) -> None:
    """The mark "In Kasse eingegeben" of a delivery: its address becomes the
    customer's default, the one and only "letztes Mal" `find_customer` may ask
    about. Unless a newer confirmed delivery of the customer, not cancelled, goes to the
    default there is: a card marked late, also while a correction holds the
    newer one's mark back, does not bring back an address the guest replaced."""
    if not _may_hold_a_default(session, order):
        return
    default = _locked_default(session, order)
    if default is not None and default != order.address_id:
        newer = select(Order.id).where(
            *_deliveries_of(order),
            Order.address_id == default,
            Order.created_at > order.created_at,
        )
        if session.scalar(newer) is not None:
            return
    _make_default(session, order, order.address_id)


def default_at_cancel(session: Session, order: Order) -> None:
    """ "Storniert" on a delivery whose address is the default: the default goes
    to the newest delivery left that the team marked, or nowhere, and the guest
    is asked. shortcut: a delivery whose mark a correction cleared counts again
    only once the team marks its new revision, which its card asks for."""
    if not _may_hold_a_default(session, order):
        return
    if _locked_default(session, order) != order.address_id:
        return
    newest = session.scalar(
        select(Order.address_id)
        .where(*_deliveries_of(order), Order.register_entered_at.is_not(None))
        .order_by(Order.created_at.desc(), Order.id.desc())
        .limit(1)
    )
    _make_default(session, order, newest)


def _may_hold_a_default(session: Session, order: Order) -> bool:
    """A delivery with its customer row, booked under the line the call came
    from: a number the guest only named never makes a default (#489), and an
    erased customer has none."""
    return (
        order.customer_id is not None
        and order.address_id is not None
        and order.phone == line_of(session, order.tenant_id, order.call_id)
    )


def _locked_default(session: Session, order: Order) -> uuid.UUID | None:
    """The customer's default address, read under a lock on the customer row:
    two marks or cancels of one customer at once go one after the other, the
    second sees the first, and the unique index never stops it."""
    session.execute(
        select(Customer.id).where(Customer.id == order.customer_id).with_for_update()
    )
    return session.scalar(
        select(Address.id).where(
            Address.customer_id == order.customer_id, Address.is_default.is_(True)
        )
    )


def _deliveries_of(order: Order) -> tuple:
    """The customer's other deliveries the guest said yes to, not cancelled."""
    return (
        Order.tenant_id == order.tenant_id,
        Order.customer_id == order.customer_id,
        Order.id != order.id,
        Order.address_id.is_not(None),
        Order.status.not_in(("draft", "cancelled")),
        Order.deleted_at.is_(None),
    )


def _make_default(session: Session, order: Order, address_id: uuid.UUID | None) -> None:
    """The old default goes first, or the unique index stops the second."""
    session.execute(
        update(Address)
        .where(Address.customer_id == order.customer_id, Address.is_default.is_(True))
        .values(is_default=False)
    )
    if address_id is not None:
        session.execute(
            update(Address).where(Address.id == address_id).values(is_default=True)
        )
