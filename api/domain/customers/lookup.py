"""Who a number is (T-6.2, docs/04 §find_customer, legal check L12).

A number is no person: several customers can share one, so without a name only
a number with one customer behind it counts, and a name that does not match
opens nobody's record (docs/04: never guess). An objection switches recognition
off for the number, a blocked customer is not recognized.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.domain.customers.phone import normalize_phone
from api.domain.menu.numberwords import fold
from api.models import Address, Customer, RecognitionObjection
from api.schemas.customers import CustomerMatch

# L12: the agent asks and never says what is stored.
SAY_SAME_ADDRESS = "An dieselbe Adresse wie beim letzten Mal?"


def find_customer(
    session: Session,
    tenant_id: uuid.UUID,
    caller_id: str,
    guest_name: str | None = None,
) -> CustomerMatch:
    phone = normalize_phone(caller_id)
    objected = session.scalar(
        select(RecognitionObjection.id).where(
            RecognitionObjection.tenant_id == tenant_id,
            RecognitionObjection.phone == phone,
        )
    )
    if objected is not None:
        return CustomerMatch(found=False)
    customers = session.scalars(
        select(Customer).where(
            Customer.tenant_id == tenant_id,
            Customer.phone == phone,
            Customer.blocked.is_(False),
        )
    ).all()
    if guest_name is not None:
        customers = [c for c in customers if fold(c.name) == fold(guest_name.strip())]
    if len(customers) != 1:
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
