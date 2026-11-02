"""Who a delivery runs under, where it goes, what the zone asks (T-6.5, docs/04
§draft_order, legal check L12).

`place` only reads and raises, so a refused delivery leaves no row behind;
`keep` writes the customer and the address once the draft stands. A name that
does not match means a new customer. A stored address counts only as the
default of the customer `find_customer` recognizes on this very number, which
holds an objection and a blocked customer as well, also in the middle of a call.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.core.errors import SAY_ON_FAILURE, BelowMinimum, InvalidInput, ServiceUnavailable
from api.domain.customers.lookup import find_customer
from api.domain.delivery.zones import check_delivery, postal_digits
from api.domain.menu.numberwords import fold
from api.domain.ordering.readback import spoken_euro
from api.models import Address, Customer
from api.schemas.delivery import DeliveryCheck
from api.schemas.orders import OrderAddress, OrderCustomer

SAY_WHICH_ADDRESS = (
    "An welche Adresse dürfen wir liefern? Bitte nennen Sie mir Postleitzahl, "
    "Straße und Hausnummer."
)
SAY_WHICH_NAME = "Auf welchen Namen darf ich die Bestellung notieren?"
SAY_BELOW_MINIMUM = (
    "Bis zum Mindestbestellwert fehlen noch {missing}. "
    "Möchten Sie noch etwas dazunehmen?"
)


@dataclass(frozen=True)
class Place:
    name: str
    # False: the stored name, which the read-back must not say (L12).
    name_heard: bool
    zone: DeliveryCheck
    spoken: OrderAddress | None
    # The stored address, and its customer.
    stored: Address | None
    customer_id: uuid.UUID | None


def place(
    session: Session, tenant_id: uuid.UUID, wanted: OrderCustomer, phone: str
) -> Place:
    name = (wanted.name or "").strip()
    if session.scalar(
        select(Customer.id).where(
            Customer.tenant_id == tenant_id,
            Customer.phone == phone,
            Customer.blocked.is_(True),
        )
    ):
        # Spam or a prank (docs/03): the team decides, nobody drives out on it.
        raise ServiceUnavailable("blocked number on a delivery", say=SAY_ON_FAILURE)
    if wanted.address is None and wanted.address_id is None:
        raise InvalidInput("delivery without an address", say=SAY_WHICH_ADDRESS)

    match = find_customer(session, tenant_id, phone, name or None)
    if wanted.address is None and (
        not match.found or match.address_id != wanted.address_id
    ):
        # Says nothing of what is stored: to the guest it is a question.
        raise InvalidInput("not the customer's default address", say=SAY_WHICH_ADDRESS)
    if not name and not match.found:
        raise InvalidInput("delivery without a name", say=SAY_WHICH_NAME)

    known = session.get(Customer, match.customer_id) if match.customer_id else None
    stored = None if wanted.address else session.get(Address, wanted.address_id)
    postal_code = wanted.address.postal_code if wanted.address else stored.postal_code
    return Place(
        name=name or known.name,
        name_heard=bool(name),
        zone=check_delivery(session, tenant_id, postal_code),
        spoken=wanted.address,
        stored=stored,
        customer_id=known.id if known else None,
    )


def require_minimum(items_cents: int, zone: DeliveryCheck) -> None:
    """The dishes count, the fee does not."""
    missing = zone.min_order_cents - items_cents
    if missing > 0:
        raise BelowMinimum(
            f"{missing} cents below the minimum order value",
            say=SAY_BELOW_MINIMUM.format(missing=spoken_euro(missing)),
        )


def keep(
    session: Session, tenant_id: uuid.UUID, phone: str, where: Place
) -> tuple[Customer, Address]:
    """The rows the draft points at, with the zone on the address. Flushed:
    the order needs their ids. Default and counters wait for `confirm`: a draft
    nobody said yes to is no "letztes Mal"."""
    customer = _customer(session, tenant_id, phone, where)
    address = where.stored or _address(session, tenant_id, customer, where.spoken)
    address.zone_id = where.zone.zone_id
    session.flush()
    return customer, address


def _customer(
    session: Session, tenant_id: uuid.UUID, phone: str, where: Place
) -> Customer:
    if where.customer_id:
        return session.get_one(Customer, where.customer_id)
    same_number = session.scalars(
        select(Customer)
        .where(Customer.tenant_id == tenant_id, Customer.phone == phone)
        .order_by(Customer.created_at)
    ).all()
    for row in same_number:
        if fold(row.name) == fold(where.name):
            return row
    customer = Customer(tenant_id=tenant_id, phone=phone, name=where.name)
    session.add(customer)
    session.flush()
    return customer


def _address(
    session: Session,
    tenant_id: uuid.UUID,
    customer: Customer,
    said: OrderAddress | None,
) -> Address:
    """The same address said again, a second draft of the call or a later
    order, is the same row."""
    assert said is not None  # `place`: a stored address or a spoken one
    wanted = Address(
        tenant_id=tenant_id,
        customer_id=customer.id,
        street=said.street.strip(),
        house_number=said.house_number.strip(),
        postal_code=postal_digits(said.postal_code),
        city=said.city.strip(),
        floor_note=(said.floor_note or "").strip() or None,
    )
    for row in session.scalars(
        select(Address).where(Address.customer_id == customer.id)
    ):
        if _same(row) == _same(wanted):
            return row
    session.add(wanted)
    return wanted


def _same(address: Address) -> tuple[str, ...]:
    return tuple(
        fold(value or "")
        for value in (
            address.street,
            address.house_number,
            address.postal_code,
            address.city,
            address.floor_note,
        )
    )
