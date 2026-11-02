"""Who a number is (T-6.2, docs/04 §find_customer, legal check L12).

A number is no person: several customers can share one, so without a name only
a number with one customer behind it counts, and a name that does not match
opens nobody's record (docs/04: never guess). An objection switches recognition
off for the number, a blocked customer is not recognized.

Recognition goes by the line the call came from, never by a number a guest,
a model or a client names: it could read out who stands behind any number
(security audit 02.11.2026). Only a row a confirmed delivery from its own line
counted an order on is someone to recognize; a draft nobody said yes to writes
a row as well, and so does a booking under a number the guest only named.
"""

import uuid

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from api.domain.customers.phone import normalize_phone, number_or_none
from api.domain.menu.numberwords import fold
from api.models import Address, Call, Customer, RecognitionObjection
from api.schemas.customers import CustomerMatch

# L12: the agent asks and never says what is stored.
SAY_SAME_ADDRESS = "An dieselbe Adresse wie beim letzten Mal?"


def objected(session: Session, tenant_id: uuid.UUID, phone: str) -> bool:
    """An objection to recognition stands on this number (L12)."""
    return (
        session.scalar(
            select(RecognitionObjection.id).where(
                RecognitionObjection.tenant_id == tenant_id,
                RecognitionObjection.phone == phone,
            )
        )
        is not None
    )


def line_of(session: Session, tenant_id: uuid.UUID, call_id: uuid.UUID) -> str | None:
    """The number the call came from, in E.164. None when it was withheld, is
    no number, or the call is not one of this tenant."""
    call = session.get(Call, call_id)
    if call is None or call.tenant_id != tenant_id:
        return None
    return number_or_none(call.caller_id)


def recognize(
    session: Session,
    tenant_id: uuid.UUID,
    call_id: uuid.UUID,
    guest_name: str | None = None,
) -> CustomerMatch:
    """`find_customer` on the line of the call: the tool of the core and the
    HTTP tool both, so neither takes a number from its caller."""
    line = line_of(session, tenant_id, call_id)
    if line is None:
        return CustomerMatch(found=False)
    return find_customer(session, tenant_id, line, guest_name)


def find_customer(
    session: Session,
    tenant_id: uuid.UUID,
    caller_id: str,
    guest_name: str | None = None,
) -> CustomerMatch:
    phone = normalize_phone(caller_id)
    if objected(session, tenant_id, phone):
        return CustomerMatch(found=False)
    customers = session.scalars(
        select(Customer).where(
            Customer.tenant_id == tenant_id,
            Customer.phone == phone,
            # A blocked row counts with or without an order, see below.
            or_(Customer.order_count > 0, Customer.blocked.is_(True)),
        )
    ).all()
    if guest_name is not None:
        customers = [c for c in customers if fold(c.name) == fold(guest_name.strip())]
    # A blocked customer counts: without them the other would look like the
    # only one on the number (#207).
    if len(customers) != 1 or customers[0].blocked:
        return CustomerMatch(found=False)
    customer = customers[0]
    address_id = session.scalar(
        select(Address.id).where(
            Address.customer_id == customer.id, Address.is_default.is_(True)
        )
    )
    return CustomerMatch(
        found=True,
        customer_id=customer.id,
        address_id=address_id,
        say=SAY_SAME_ADDRESS if address_id else None,
    )
