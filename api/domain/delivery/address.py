"""Who a delivery runs under, where it goes, what the zone asks (T-6.5, docs/04
§draft_order, legal check L12).

`place` only reads and raises, so a refused delivery leaves no row behind;
`keep` writes the customer and the address once the draft stands. A name that
does not match means a new customer, and with an objection on the number so
does one that matches. A stored address counts only as the default of the
customer `find_customer` recognizes on this very number, which holds an
objection and a blocked customer as well, also in the middle of a call.

Only on the line the call came from (security audit 02.11.2026). A number the
guest only names, or a withheld line, is the number the team calls back on and
no key to a record: no stored name or address, no row reused, a new row that
`confirm` leaves without default and counters, so `find_customer` never offers
it. A blocked customer on the booking number or on the line goes to the team.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.core.errors import SAY_ON_FAILURE, InvalidInput, ServiceUnavailable
from api.domain.customers.lookup import find_customer, objected
from api.domain.delivery.zones import check_delivery, postal_digits
from api.domain.menu.numberwords import fold
from api.models import Address, Customer, Order
from api.schemas.customers import CustomerMatch
from api.schemas.delivery import DeliveryCheck
from api.schemas.orders import OrderAddress, OrderCustomer

SAY_WHICH_ADDRESS = (
    "An welche Adresse dürfen wir liefern? Bitte nennen Sie mir Postleitzahl, "
    "Straße und Hausnummer."
)
SAY_WHICH_NAME = "Auf welchen Namen darf ich die Bestellung notieren?"


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
    # The booking number is the line the call came from.
    own_line: bool


def place(
    session: Session,
    tenant_id: uuid.UUID,
    wanted: OrderCustomer,
    phone: str,
    line: str | None,
) -> Place:
    """`line`: the number the call came from, None when withheld."""
    name = (wanted.name or "").strip()
    if session.scalar(
        select(Customer.id).where(
            Customer.tenant_id == tenant_id,
            # The booking number and the line: a prank names another number.
            Customer.phone.in_([phone, line or phone]),
            Customer.blocked.is_(True),
        )
    ):
        # Spam or a prank (docs/03): the team decides, nobody drives out on it.
        # The message names no state of the number: it reaches an HTTP client.
        raise ServiceUnavailable("delivery left to the team", say=SAY_ON_FAILURE)
    if wanted.address is None and wanted.address_id is None:
        raise InvalidInput("delivery without an address", say=SAY_WHICH_ADDRESS)

    own_line = phone == line
    match = (
        find_customer(session, tenant_id, phone, name or None)
        if own_line
        else CustomerMatch(found=False)
    )
    if wanted.address is None and (
        not match.found or match.address_id != wanted.address_id
    ):
        # Says nothing of what is stored: to the guest it is a question.
        raise InvalidInput("not the customer's default address", say=SAY_WHICH_ADDRESS)
    known = session.get(Customer, match.customer_id) if match.customer_id else None
    runs_under = name or (known.name if known else "")
    if not runs_under:
        raise InvalidInput("delivery without a name", say=SAY_WHICH_NAME)

    stored = None
    if wanted.address is not None:
        postal_code = wanted.address.postal_code
    else:
        stored = session.get_one(Address, wanted.address_id)
        postal_code = stored.postal_code
    return Place(
        name=runs_under,
        name_heard=bool(name),
        zone=check_delivery(session, tenant_id, postal_code),
        spoken=wanted.address,
        stored=stored,
        customer_id=known.id if known else None,
        own_line=own_line,
    )


def keep(
    session: Session,
    tenant_id: uuid.UUID,
    call_id: uuid.UUID,
    phone: str,
    where: Place,
) -> tuple[Customer, Address]:
    """The rows the draft points at, with the zone on the address. Flushed:
    the order needs their ids. Counters wait for `confirm`, the default for the
    team's mark (`board.settle_default`): a draft nobody said yes to is no
    "letztes Mal"."""
    customer = _customer(session, tenant_id, call_id, phone, where)
    address = where.stored or address_row(session, tenant_id, customer.id, where.spoken)
    address.zone_id = where.zone.zone_id
    session.flush()
    return customer, address


def _customer(
    session: Session,
    tenant_id: uuid.UUID,
    call_id: uuid.UUID,
    phone: str,
    where: Place,
) -> Customer:
    if where.customer_id:
        return session.get_one(Customer, where.customer_id)
    # Besides the one `find_customer` recognized, only the row an earlier
    # draft of this call wrote: any other row on the number may be one a
    # booking under a number someone only named wrote. A named number and an
    # objection (L12) reuse none, the name does not lead to an old record.
    if where.own_line and not objected(session, tenant_id, phone):
        this_call = session.scalars(
            select(Customer)
            .join(Order, Order.customer_id == Customer.id)
            .where(Order.call_id == call_id, Customer.phone == phone)
            .order_by(Customer.created_at)
        ).all()
        for row in this_call:
            if fold(row.name) == fold(where.name):
                return row
    customer = Customer(tenant_id=tenant_id, phone=phone, name=where.name)
    session.add(customer)
    session.flush()
    return customer


def address_row(
    session: Session,
    tenant_id: uuid.UUID,
    customer_id: uuid.UUID,
    said: OrderAddress | None,
) -> Address:
    """The same address said again, a second draft of the call, a later order
    or the team's correction on the tablet (#525), is the same row."""
    assert said is not None  # `place`: a stored address or a spoken one
    wanted = _wanted(tenant_id, customer_id, said)
    for row in session.scalars(
        select(Address).where(Address.customer_id == customer_id)
    ):
        if _same(row) == _same(wanted):
            return row
    session.add(wanted)
    return wanted


def same_address(row: Address, said: OrderAddress) -> bool:
    """Whether `said` is `row` again, as `address_row` would find it."""
    return _same(row) == _same(_wanted(row.tenant_id, row.customer_id, said))


def _wanted(
    tenant_id: uuid.UUID, customer_id: uuid.UUID, said: OrderAddress
) -> Address:
    return Address(
        tenant_id=tenant_id,
        customer_id=customer_id,
        street=said.street.strip(),
        house_number=said.house_number.strip(),
        postal_code=postal_digits(said.postal_code),
        city=said.city.strip(),
        floor_note=(said.floor_note or "").strip() or None,
    )


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
