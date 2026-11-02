"""Requests of data subjects (legal check U2, T-11.8, docs/09 §2).

A guest asks what we hold about them (Art. 15), to have it erased (Art. 17), or
objects to being recognized (Art. 21). The log proves the one-month deadline.

A number is no person (L12): several guests can share one, so nothing here finds
a person by the number alone. `verify` wants a name with the date of an order or
reservation, or with the delivery address. From a row that matches, a row counts
when it carries the same name on a number of those rows, or hangs on such a row:
by its customer, or by a call nobody else's name is on. Same name on the same
number reads as one person, there is nothing to tell them apart with. A call or
a callback that carries only the number, or shares its call with another guest,
is the guest's to confirm: it stays under `unclaimed_*` until the team passes its
id in `claimed`.

Nothing here commits: the job (`api/jobs/data_requests.py`) writes the erasure
ledger first, so an erasure that commits is always in the ledger.
"""

import re
import uuid
from collections import Counter, defaultdict
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, NamedTuple

from sqlalchemy import ColumnElement, Date, cast, delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from api.config import settings
from api.core.errors import Conflict, InvalidInput, NotFound
from api.core.time import add_months, to_local
from api.domain.customers.personal import (
    CALLBACK_GONE,
    ORDER_DAY,
    ORDER_GONE,
    thin_payload,
)
from api.domain.customers.phone import DEFAULT_COUNTRY_CODE
from api.domain.menu.numberwords import fold
from api.domain.ordering.handover import SENT
from api.domain.ordering.labels import current_labels_many, labels_for_rows
from api.models import (
    Address,
    AuditLog,
    Call,
    Callback,
    ConsentLog,
    Customer,
    DataSubjectRequest,
    Order,
    OrderItem,
    OutboxEvent,
    RecognitionObjection,
    Reservation,
)

ACTOR_JOB = "job:data_requests"
REFERENCE = re.compile(r"dsr_[0-9]{4}_[0-9]{3,}")
# A complex request: two more months, told to the guest inside the first.
LONGEST_DEADLINE_MONTHS = 3
OPEN_ORDER_STATUSES = ("confirmed", "approved")
# What the guest may choose to keep; an order in the kitchen is only waited for.
KEEPABLE = (Reservation.__tablename__, Callback.__tablename__)


@dataclass(frozen=True)
class Details:
    """What the requester said, as the team typed it in."""

    name: str
    # Days of an order or a reservation. Each opens the number of its row.
    days: tuple[date, ...] = ()
    street: str | None = None
    house_number: str | None = None
    postal_code: str | None = None


class LedgerEntry(NamedTuple):
    """One line of the erasure ledger (U7): no name and no number."""

    table: str
    row_id: uuid.UUID
    action: str


class OpenRow(NamedTuple):
    table: str
    row_id: uuid.UUID


@dataclass
class Person:
    """The verified person's rows, and the ones only the guest can say are theirs."""

    phones: set[str]
    customers: list[Customer]
    orders: list[Order]
    reservations: list[Reservation]
    calls: list[Call]
    callbacks: list[Callback]
    unclaimed_calls: list[Call]
    unclaimed_callbacks: list[Callback]
    # Rows an erasure must not touch yet: they are still being served.
    open_rows: list[OpenRow]


def log_request(
    session: Session,
    tenant_id: uuid.UUID,
    *,
    kind: str,
    channel: str,
    note: str | None,
    received_at: datetime,
    zone: str,
    reference: str | None = None,
) -> DataSubjectRequest:
    """`reference` keeps the number a line of the interim file already has."""
    day = to_local(received_at, zone).date()
    if reference is None:
        reference = _next_reference(session, tenant_id, day.year)
    elif not REFERENCE.fullmatch(reference):
        raise InvalidInput(f"a reference reads dsr_2026_001, not {reference!r}")
    elif _find(session, tenant_id, reference) is not None:
        raise Conflict(f"{reference} is already in the log")
    request = DataSubjectRequest(
        tenant_id=tenant_id,
        reference=reference,
        kind=kind,
        channel=channel,
        received_at=received_at,
        deadline=add_months(day, 1),
        verification_note=note,
    )
    session.add(request)
    session.flush()
    _audit(session, request, "data_subject.request_logged", kind=kind, channel=channel)
    return request


def _next_reference(session: Session, tenant_id: uuid.UUID, year: int) -> str:
    taken = session.scalars(
        select(DataSubjectRequest.reference).where(
            DataSubjectRequest.tenant_id == tenant_id,
            DataSubjectRequest.reference.startswith(f"dsr_{year}_", autoescape=True),
        )
    )
    number = max((int(row.rsplit("_", 1)[1]) for row in taken), default=0) + 1
    return f"dsr_{year}_{number:03d}"


def _find(
    session: Session, tenant_id: uuid.UUID, reference: str
) -> DataSubjectRequest | None:
    return session.scalar(
        select(DataSubjectRequest).where(
            DataSubjectRequest.tenant_id == tenant_id,
            DataSubjectRequest.reference == reference,
        )
    )


def get_request(
    session: Session, tenant_id: uuid.UUID, reference: str
) -> DataSubjectRequest:
    request = _find(session, tenant_id, reference)
    if request is None:
        raise NotFound(f"no request {reference}")
    return request


def open_requests(
    session: Session, tenant_id: uuid.UUID
) -> Sequence[DataSubjectRequest]:
    return session.scalars(
        select(DataSubjectRequest)
        .where(
            DataSubjectRequest.tenant_id == tenant_id,
            DataSubjectRequest.answered_at.is_(None),
        )
        .order_by(DataSubjectRequest.deadline, DataSubjectRequest.reference)
    ).all()


def extend_deadline(session: Session, request: DataSubjectRequest, zone: str) -> None:
    """Art. 12(3): a complex request may take two more months, once."""
    longest = add_months(
        to_local(request.received_at, zone).date(), LONGEST_DEADLINE_MONTHS
    )
    if request.answered_at is not None or request.deadline >= longest:
        raise Conflict(f"the deadline of {request.reference} cannot move")
    request.deadline = longest
    _audit(session, request, "data_subject.deadline_extended")
    session.flush()


def answer_request(
    session: Session,
    request: DataSubjectRequest,
    outcome: str,
    answered_at: datetime,
    zone: str,
) -> None:
    """Closes the line and starts its periods (docs/03 Löschkonzept). The year is
    the restaurant's: a claim runs from the end of it (§ 199(1) BGB)."""
    if request.answered_at is not None:
        raise Conflict(f"{request.reference} is already answered")
    day = to_local(answered_at, zone).date()
    if day < to_local(request.received_at, zone).date():
        raise InvalidInput(f"{request.reference} cannot be answered before it came")
    request.answered_at = answered_at
    request.outcome = outcome
    request.delete_after = date(day.year + settings.retention_request_log_years, 12, 31)
    _audit(session, request, "data_subject.request_answered", outcome=outcome)
    session.flush()


def _audit(
    session: Session, request: DataSubjectRequest, action: str, **payload: Any
) -> None:
    """No name and no number: only `verification_note` holds those."""
    session.add(
        AuditLog(
            tenant_id=request.tenant_id,
            actor=ACTOR_JOB,
            action=action,
            entity="data_subject_request",
            entity_id=request.id,
            payload=payload,
        )
    )


def _folded(text: str) -> str:
    return fold(" ".join(text.split()))


def _local_day(column: Any, zone: str) -> ColumnElement:
    return cast(func.timezone(zone, column), Date)


def _by_day(
    session: Session, tenant_id: uuid.UUID, days: Sequence[date], zone: str, name: str
) -> list[Customer]:
    """The order of a customer loses its name after 90 days, the customer stays
    for 24 months: without this the row of a pickup guest could not be reached."""
    return [
        customer
        for customer in session.scalars(
            select(Customer).where(
                Customer.tenant_id == tenant_id,
                or_(
                    _local_day(Customer.last_order_at, zone).in_(days),
                    _local_day(Customer.last_contact_at, zone).in_(days),
                ),
            )
        )
        if _folded(customer.name) == name
    ]


def _by_address(
    session: Session, tenant_id: uuid.UUID, details: Details, name: str
) -> list[Customer]:
    rows = session.execute(
        select(Customer, Address)
        .join(Address, Address.customer_id == Customer.id)
        .where(
            Customer.tenant_id == tenant_id,
            Address.postal_code == (details.postal_code or "").strip(),
        )
    ).all()
    return [
        customer
        for customer, address in rows
        if _folded(customer.name) == name
        and _folded(address.street) == _folded(details.street or "")
        and _folded(address.house_number).replace(" ", "")
        == _folded(details.house_number or "").replace(" ", "")
    ]


def _names_on_calls(
    session: Session, tenant_id: uuid.UUID
) -> dict[uuid.UUID, set[str]]:
    """Which guests' names hang on which call. A number says nothing about that."""
    rows = session.execute(
        select(Order.call_id, Order.customer_name)
        .where(Order.tenant_id == tenant_id, Order.customer_name != "")
        .union_all(
            select(Reservation.call_id, Reservation.guest_name).where(
                Reservation.tenant_id == tenant_id, Reservation.guest_name != ""
            )
        )
    ).all()
    names: dict[uuid.UUID, set[str]] = defaultdict(set)
    for call_id, name in rows:
        names[call_id].add(_folded(name))
    return names


def _national_digits(phone: str) -> str:
    """`calls.caller_id` holds the number as the line gave it, not always E.164."""
    return phone.removeprefix("+").removeprefix(DEFAULT_COUNTRY_CODE)


def _calls_and_callbacks(
    session: Session,
    tenant_id: uuid.UUID,
    orders: list[Order],
    tables: list[Reservation],
    phones: set[str],
    claimed: Collection[uuid.UUID],
) -> tuple[list[Call], list[Callback], list[Call], list[Callback]]:
    """The person's calls and callbacks, then the ones to ask the guest about. A
    call with the name of nobody else is theirs; one with only the number, or
    with a second guest on it, is theirs once claimed; one with only another
    guest's name is not on offer at all."""
    names_on = _names_on_calls(session, tenant_id)
    own_calls = {order.call_id for order in orders}
    own_calls |= {table.call_id for table in tables}
    own_names = {_folded(order.customer_name) for order in orders} | {
        _folded(table.guest_name) for table in tables
    }
    sole = {call for call in own_calls if names_on[call] <= own_names}
    offered: set[uuid.UUID] = set()

    def sort_out(row: Any, call_id: uuid.UUID, own: list, unclaimed: list) -> None:
        if call_id in sole:
            own.append(row)
        elif call_id in own_calls or not names_on[call_id]:
            offered.add(row.id)
            (own if row.id in claimed else unclaimed).append(row)

    calls: list[Call] = []
    unclaimed_calls: list[Call] = []
    for call in session.scalars(
        select(Call)
        .where(
            Call.tenant_id == tenant_id,
            or_(
                Call.id.in_(own_calls),
                *(
                    func.regexp_replace(Call.caller_id, r"\D", "", "g").contains(
                        _national_digits(phone), autoescape=True
                    )
                    for phone in phones
                ),
            ),
        )
        .order_by(Call.started_at, Call.id)
    ):
        sort_out(call, call.id, calls, unclaimed_calls)

    callbacks: list[Callback] = []
    unclaimed_callbacks: list[Callback] = []
    for callback in session.scalars(
        select(Callback)
        .where(
            Callback.tenant_id == tenant_id,
            or_(Callback.call_id.in_(own_calls), Callback.phone.in_(phones)),
        )
        .order_by(Callback.created_at, Callback.id)
    ):
        sort_out(callback, callback.call_id, callbacks, unclaimed_callbacks)

    if unknown := set(claimed) - offered:
        raise InvalidInput(f"not on offer for this person: {sorted(map(str, unknown))}")
    return calls, callbacks, unclaimed_calls, unclaimed_callbacks


def verify(
    session: Session,
    tenant_id: uuid.UUID,
    details: Details,
    zone: str,
    now: datetime,
    claimed: Collection[uuid.UUID] = (),
) -> Person:
    """The person behind the details, or `NotFound`. A second number needs a
    detail of its own: a number the guest only names opens nothing."""
    name = _folded(details.name)
    address = (details.street, details.house_number, details.postal_code)
    if not name or not (details.days or all(address)):
        raise InvalidInput(
            "a name with the date of an order or reservation, or with the "
            "delivery address: never a number or a name alone"
        )
    found_orders = [
        order
        for order in session.scalars(
            select(Order).where(
                Order.tenant_id == tenant_id,
                _local_day(ORDER_DAY, zone).in_(details.days),
            )
        )
        if _folded(order.customer_name) == name
    ]
    found_tables = [
        table
        for table in session.scalars(
            select(Reservation).where(
                Reservation.tenant_id == tenant_id,
                _local_day(Reservation.reserved_for, zone).in_(details.days),
            )
        )
        if _folded(table.guest_name) == name
    ]
    found_customers = _by_day(session, tenant_id, details.days, zone, name)
    if all(address):
        found_customers += _by_address(session, tenant_id, details, name)
    if not (found_orders or found_tables or found_customers):
        raise NotFound("not verified: no row carries these details")

    phones = {order.phone for order in found_orders}
    phones |= {table.phone for table in found_tables}
    phones |= {customer.phone for customer in found_customers}
    linked = {c.id for c in found_customers}
    linked |= {o.customer_id for o in found_orders if o.customer_id}
    customers = [
        customer
        for customer in session.scalars(
            select(Customer).where(
                Customer.tenant_id == tenant_id,
                or_(Customer.phone.in_(phones), Customer.id.in_(linked)),
            )
        )
        if customer.id in linked or _folded(customer.name) == name
    ]
    customer_ids = {customer.id for customer in customers}
    orders = [
        order
        for order in session.scalars(
            select(Order)
            .where(
                Order.tenant_id == tenant_id,
                or_(Order.phone.in_(phones), Order.customer_id.in_(customer_ids)),
            )
            .order_by(Order.created_at, Order.id)
        )
        if order.customer_id in customer_ids or _folded(order.customer_name) == name
    ]
    tables = [
        table
        for table in session.scalars(
            select(Reservation)
            .where(Reservation.tenant_id == tenant_id, Reservation.phone.in_(phones))
            .order_by(Reservation.created_at, Reservation.id)
        )
        if _folded(table.guest_name) == name
    ]

    calls, callbacks, unclaimed_calls, unclaimed_callbacks = _calls_and_callbacks(
        session, tenant_id, orders, tables, phones, claimed
    )
    return Person(
        phones=phones,
        customers=customers,
        orders=orders,
        reservations=tables,
        calls=calls,
        callbacks=callbacks,
        unclaimed_calls=unclaimed_calls,
        unclaimed_callbacks=unclaimed_callbacks,
        open_rows=_open_rows(session, tenant_id, orders, tables, callbacks, now),
    )


def _open_rows(
    session: Session,
    tenant_id: uuid.UUID,
    orders: list[Order],
    tables: list[Reservation],
    callbacks: list[Callback],
    now: datetime,
) -> list[OpenRow]:
    """Rows still being served. An event not sent yet counts: its payload is the
    slip or the team's notification, and it would go out with the guest's name
    after the erasure (docs/09 §2). So does an order the kitchen has no slip for,
    whatever its time says: "Passt" and "Nochmal senden" build the slip from these
    rows, its allergy note included."""
    waiting = {
        value
        for payload in session.scalars(
            select(OutboxEvent.payload).where(
                OutboxEvent.tenant_id == tenant_id, OutboxEvent.status == "pending"
            )
        )
        for key, value in payload.items()
        if key in ("order_id", "reservation_id", "callback_id")
    }
    return [
        *(
            OpenRow(Order.__tablename__, order.id)
            for order in orders
            if str(order.id) in waiting
            or (
                order.status in OPEN_ORDER_STATUSES
                and order.deleted_at is None
                and (
                    order.handover_state != SENT
                    or (order.ready_at or order.created_at) > now
                )
            )
        ),
        *(
            OpenRow(Reservation.__tablename__, table.id)
            for table in tables
            if str(table.id) in waiting
            or (
                table.status == "confirmed"
                and table.deleted_at is None
                and table.reserved_for > now
            )
        ),
        *(
            OpenRow(Callback.__tablename__, callback.id)
            for callback in callbacks
            if str(callback.id) in waiting
            or (callback.status == "open" and callback.deleted_at is None)
        ),
    ]


def _fields(row: Any, *names: str) -> dict[str, Any]:
    return {name: getattr(row, name) for name in names}


def access_copy(
    session: Session, tenant_id: uuid.UUID, person: Person
) -> dict[str, Any]:
    """`copy` goes to the guest. `ask_the_guest` stays with the team: those rows
    may be someone else's until the guest confirms them (docs/09 §2 step 3)."""
    order_ids = [order.id for order in person.orders]
    table_ids = [table.id for table in person.reservations]
    callback_ids = [callback.id for callback in person.callbacks]
    items: dict[uuid.UUID, list[OrderItem]] = defaultdict(list)
    for line in session.scalars(
        select(OrderItem)
        .where(OrderItem.tenant_id == tenant_id, OrderItem.order_id.in_(order_ids))
        .order_by(OrderItem.order_id, OrderItem.created_at, OrderItem.id)
    ):
        items[line.order_id].append(line)
    # What was read back to the guest: a dish can be renamed since the call.
    stored = current_labels_many(session, order_ids)
    lines = {
        order_id: [
            {
                "number": number,
                "dish": dish,
                **_fields(line, "quantity", "options", "note"),
            }
            for line, (number, dish) in zip(
                rows,
                labels_for_rows(session, order_id, rows, stored.get(order_id)),
                strict=True,
            )
        ]
        for order_id, rows in items.items()
    }
    addresses = session.scalars(
        select(Address).where(
            Address.tenant_id == tenant_id,
            Address.customer_id.in_([customer.id for customer in person.customers]),
        )
    )
    consents = session.scalars(
        select(ConsentLog).where(
            ConsentLog.tenant_id == tenant_id,
            ConsentLog.call_id.in_([call.id for call in person.calls]),
        )
    )
    events = session.scalars(
        select(OutboxEvent).where(
            OutboxEvent.tenant_id == tenant_id,
            _event_of(order_ids, table_ids, callback_ids),
        )
    )
    objections = session.scalars(
        select(RecognitionObjection.phone).where(
            RecognitionObjection.tenant_id == tenant_id,
            RecognitionObjection.phone.in_(person.phones),
        )
    )
    return {
        "copy": {
            "customers": [
                _fields(c, "id", "name", "phone", "last_order_at", "last_contact_at")
                for c in person.customers
            ],
            "addresses": [
                _fields(
                    a, "street", "house_number", "postal_code", "city", "floor_note"
                )
                for a in addresses
            ],
            "orders": [
                {
                    **_fields(
                        order,
                        "id",
                        "type",
                        "status",
                        "customer_name",
                        "phone",
                        "note",
                        "created_at",
                        "ready_at",
                        "total_cents",
                    ),
                    "items": lines.get(order.id, []),
                }
                for order in person.orders
            ],
            "reservations": [
                _fields(
                    table,
                    "id",
                    "status",
                    "guest_name",
                    "phone",
                    "party_size",
                    "reserved_for",
                    "note",
                )
                for table in person.reservations
            ],
            "callbacks": [_callback_fields(row) for row in person.callbacks],
            "calls": [_call_fields(row) for row in person.calls],
            "consent_log": [
                _fields(c, "call_id", "question", "answer", "answered_at")
                for c in consents
            ],
            "outbox": [
                _fields(e, "event_type", "payload", "created_at") for e in events
            ],
        },
        "ask_the_guest": {
            "calls": [_call_fields(row) for row in person.unclaimed_calls],
            "callbacks": [_callback_fields(row) for row in person.unclaimed_callbacks],
            "recognition_objections": list(objections),
        },
        "open_rows": [row._asdict() for row in person.open_rows],
    }


def _call_fields(call: Call) -> dict[str, Any]:
    return _fields(call, "id", "caller_id", "started_at", "duration_seconds", "outcome")


def _callback_fields(callback: Callback) -> dict[str, Any]:
    return _fields(
        callback, "id", "call_id", "phone", "reason", "summary", "status", "created_at"
    )


def _event_of(
    order_ids: Collection[uuid.UUID],
    table_ids: Collection[uuid.UUID],
    callback_ids: Collection[uuid.UUID],
) -> ColumnElement[bool]:
    """By the row an event is about, never by its `call_id`: one call can hold
    two guests."""
    keys = (
        ("order_id", order_ids),
        ("reservation_id", table_ids),
        ("callback_id", callback_ids),
    )
    return or_(
        *(
            OutboxEvent.payload[key].astext.in_([str(row_id) for row_id in ids])
            for key, ids in keys
        )
    )


def _require(request: DataSubjectRequest, kind: str) -> None:
    if request.kind != kind or request.answered_at is not None:
        raise Conflict(f"{request.reference} is no open {kind} request")


def _write(
    session: Session, statement: Any, model: Any, action: str, ledger: list[LedgerEntry]
) -> None:
    rows = session.scalars(
        statement.returning(model.id).execution_options(synchronize_session=False)
    )
    ledger.extend(LedgerEntry(model.__tablename__, row, action) for row in rows)


def _clear_calls(
    session: Session,
    tenant_id: uuid.UUID,
    calls: list[Call],
    ledger: list[LedgerEntry],
) -> None:
    _write(
        session,
        update(Call)
        .where(
            Call.tenant_id == tenant_id,
            Call.id.in_([call.id for call in calls]),
            Call.caller_id.is_not(None),
        )
        .values(caller_id=None),
        Call,
        "cleared",
        ledger,
    )


def erase(
    session: Session,
    request: DataSubjectRequest,
    person: Person,
    keep: Collection[uuid.UUID] = (),
) -> list[LedgerEntry]:
    """Every store of U2, all at once: rows that stay stripped keep what the tax
    records need (L18, Art. 17(3)(b)), `consent_log` stays as the proof of L13
    (docs/03). Open rows stop it, so that no second run has to find a person
    whose name is already gone; `keep` names the ones the guest chose to keep."""
    _require(request, "erasure")
    keepable = {row.row_id for row in person.open_rows if row.table in KEEPABLE}
    if not keepable.issuperset(keep):
        raise InvalidInput("only an open reservation or callback can be kept")
    blocking = [row for row in person.open_rows if row.row_id not in keep]
    if blocking:
        names = ", ".join(f"{row.table} {row.row_id}" for row in blocking)
        raise Conflict(f"open rows first: {names}")

    tenant_id = request.tenant_id
    orders = [order.id for order in person.orders]
    tables = [table.id for table in person.reservations if table.id not in keep]
    callbacks = [row.id for row in person.callbacks if row.id not in keep]
    customers = [customer.id for customer in person.customers]
    ledger: list[LedgerEntry] = []
    thin = thin_payload()
    _write(
        session,
        delete(OutboxEvent).where(
            OutboxEvent.tenant_id == tenant_id, _event_of((), tables, ())
        ),
        OutboxEvent,
        "deleted",
        ledger,
    )
    _write(
        session,
        update(OutboxEvent)
        .where(
            OutboxEvent.tenant_id == tenant_id,
            _event_of(orders, (), callbacks),
            OutboxEvent.payload != thin,
        )
        .values(payload=thin),
        OutboxEvent,
        "thinned",
        ledger,
    )
    _write(
        session,
        delete(Reservation).where(
            Reservation.tenant_id == tenant_id, Reservation.id.in_(tables)
        ),
        Reservation,
        "deleted",
        ledger,
    )
    _write(
        session,
        update(OrderItem)
        .where(
            OrderItem.tenant_id == tenant_id,
            OrderItem.order_id.in_(orders),
            OrderItem.note.is_not(None),
        )
        .values(note=None),
        OrderItem,
        "cleared",
        ledger,
    )
    _write(
        session,
        update(Order)
        .where(Order.tenant_id == tenant_id, Order.id.in_(orders))
        .values(**ORDER_GONE),
        Order,
        "cleared",
        ledger,
    )
    _write(
        session,
        update(Callback)
        .where(
            Callback.tenant_id == tenant_id,
            Callback.id.in_(callbacks),
            or_(Callback.phone != "", Callback.summary != ""),
        )
        .values(**CALLBACK_GONE),
        Callback,
        "cleared",
        ledger,
    )
    _clear_calls(session, tenant_id, person.calls, ledger)
    # By hand although the key cascades: the ledger names each address.
    _write(
        session,
        delete(Address).where(
            Address.tenant_id == tenant_id, Address.customer_id.in_(customers)
        ),
        Address,
        "deleted",
        ledger,
    )
    _write(
        session,
        delete(Customer).where(
            Customer.tenant_id == tenant_id, Customer.id.in_(customers)
        ),
        Customer,
        "deleted",
        ledger,
    )
    _audit(
        session,
        request,
        "data_subject.erased",
        **Counter(entry.table for entry in ledger),
    )
    session.flush()
    session.expire_all()
    return ledger


def record_objection(
    session: Session, request: DataSubjectRequest, person: Person
) -> list[LedgerEntry]:
    """Recognition stays off for every number of the person (L12), and their
    calls lose the number. The customer stays: an objection is no erasure."""
    _require(request, "objection")
    for phone in sorted(person.phones):
        session.execute(
            insert(RecognitionObjection)
            .values(tenant_id=request.tenant_id, phone=phone)
            .on_conflict_do_nothing(constraint="uq_recognition_objections_phone")
        )
    ledger: list[LedgerEntry] = []
    _clear_calls(session, request.tenant_id, person.calls, ledger)
    _audit(session, request, "data_subject.objection_recorded")
    session.flush()
    session.expire_all()
    return ledger
