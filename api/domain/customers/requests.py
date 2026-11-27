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
ledger first, so an erasure that commits is always in the ledger. The one
exception is `look`, the owner's read on the tab "Daten" (#571): its audit row
is committed before a row is read. That tab logs, extends, answers and reads;
`erase` and `record_objection` stay with the job, which has the ledger file.
"""

import csv
import re
import uuid
from collections import Counter, defaultdict
from collections.abc import Callable, Collection, Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, NamedTuple

from sqlalchemy import (
    ColumnElement,
    Date,
    case,
    cast,
    delete,
    func,
    or_,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from api.config import settings
from api.core.errors import Ambiguous, Conflict, FieldErrors, InvalidInput, NotFound
from api.core.time import add_months, to_local, tz
from api.domain.customers.personal import (
    CALLBACK_GONE,
    ORDER_DAY,
    ORDER_GONE,
    thin_payload,
)
from api.domain.customers.phone import (
    DEFAULT_COUNTRY_CODE,
    normalize_phone,
    number_or_none,
)
from api.domain.menu.numberwords import fold
from api.domain.ordering.handover import SENT
from api.domain.ordering.labels import current_labels_many, labels_for_rows
from api.domain.status.staff_changes import _locked
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
    Recording,
    Reservation,
    ServiceConfig,
)
from api.models.customers import REQUEST_CHANNELS, REQUEST_KINDS, REQUEST_OUTCOMES

ACTOR_JOB = "job:data_requests"
REFERENCE = re.compile(r"dsr_[0-9]{4}_[0-9]{3,}")
# The verification details as the guest gave them: a few lines, never a file.
NOTE_MAX = 2000
# What `erase` and `record_objection` leave in the audit log, by request kind.
RUN_ACTIONS = {
    "erasure": "data_subject.erased",
    "objection": "data_subject.objection_recorded",
}
ROWS_READ = "data_subject.rows_read"
SAY_LOG_CHANGED = (
    "Die Liste hat sich gerade geändert. Bitte prüfen, ob die Anfrage schon "
    "eingetragen ist."
)
SAY_NOTE_LONG = f"Die Notiz ist zu lang (höchstens {NOTE_MAX} Zeichen)."
SAY_NOTE_SIGN = "Die Notiz enthält ein Zeichen, das sich nicht speichern lässt."
# The database takes no NUL in a text; a typed one is refused, not a 500.
NUL = chr(0)
SAY_FUTURE = "Der Eingang kann nicht in der Zukunft liegen."
# On the tab a request needs the guest's details: without them nobody can find
# the guest's rows, so no request is logged and no deadline runs (#829-11,
# Maxi 2026-11-17). The job's interim file may still log a line without them.
SAY_NO_DETAILS = (
    "Bitte die Angaben des Gastes zu seiner Person eintragen. Ohne sie wird "
    "die Anfrage nicht eingetragen."
)
SAY_DETAILS = (
    "Bitte einen Namen mit dem Tag einer Bestellung oder Reservierung oder mit "
    "der vollständigen Lieferadresse angeben. Eine Nummer oder ein Name allein "
    "reicht nie."
)
SAY_NOBODY = (
    "Nicht bestätigt: zu diesen Angaben gibt es keine Zeile. Bitte die "
    "Schreibweise des Namens und den Tag prüfen."
)
SAY_NUMBERS = (
    "Nicht bestätigt: die Angaben passen zu mehreren Rufnummern. Bitte den Gast "
    "nach jeder Nummer fragen, von der er angerufen hat, und alle eintragen."
)
SAY_CLAIM = (
    "Eine angekreuzte Zeile gehört nicht mehr zu dieser Person. Bitte ohne "
    "Kreuze neu prüfen."
)
SAY_NOT_RUN = (
    "Für diese Anfrage ist auf dem Server noch nichts gelaufen. „Erledigt“ "
    "geht erst danach; konnte die Person nicht bestätigt werden, ist das "
    "Ergebnis „nicht bestätigt“."
)
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
    # Numbers the guest says they called from: they narrow what a day or the
    # address opens to one person, and never open anything themselves.
    phones: tuple[str, ...] = ()


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


def _note_fault(note: str | None) -> str | None:
    """What is wrong with a stripped note, or None."""
    if note is not None and len(note) > NOTE_MAX:
        return SAY_NOTE_LONG
    if note is not None and NUL in note:
        return SAY_NOTE_SIGN
    return None


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
    actor: str = ACTOR_JOB,
) -> DataSubjectRequest:
    """`reference` keeps the number a line of the interim file already has."""
    note = (note or "").strip() or None
    if fault := _note_fault(note):
        raise FieldErrors("the note is refused", {"note": fault})
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
    _audit(
        session,
        request,
        "data_subject.request_logged",
        actor,
        kind=kind,
        channel=channel,
    )
    return request


def log_token(session: Session, tenant_id: uuid.UUID) -> str:
    """The request log as a form showed it (`log_request_once`): how many lines
    it has and when the newest was written. Every new line changes it, whatever
    year its reference counts in."""
    count, newest = session.execute(
        select(func.count(), func.max(DataSubjectRequest.created_at)).where(
            DataSubjectRequest.tenant_id == tenant_id
        )
    ).one()
    return f"{count}-{newest.timestamp():.6f}" if newest else "0"


def log_request_once(
    session: Session,
    tenant_id: uuid.UUID,
    seen: str,
    *,
    kind: str,
    channel: str,
    note: str | None,
    received_on: date | None,
    now: datetime,
    actor: str,
    other_faults: dict[str, str] | None = None,
) -> DataSubjectRequest:
    """A request typed into the tab "Daten". `seen` is `log_token` as the
    form showed it: a second tap on "Eintragen", or a request another device
    logged in between, writes no second line. One request after the other, under
    the lock of the settings row. A day of receipt that has not come is refused:
    its deadline would be later than the law's. Every fault comes in one answer
    (#862); `other_faults` are those the caller found in the form itself, such as
    a day that could not be read."""
    _, zone = _locked(session, tenant_id)
    if seen != log_token(session, tenant_id):
        raise Conflict("the request log changed", say=SAY_LOG_CHANGED)
    if kind not in REQUEST_KINDS or channel not in REQUEST_CHANNELS:
        raise InvalidInput("no such kind or channel")
    today = to_local(now, zone).date()
    faults = dict(other_faults or {})
    stripped = (note or "").strip()
    if not stripped:
        faults["note"] = SAY_NO_DETAILS
    elif fault := _note_fault(stripped):
        faults["note"] = fault
    if received_on is not None and received_on > today:
        faults["received"] = SAY_FUTURE
    if faults:
        raise FieldErrors(f"request refused: {sorted(faults)}", faults)
    received_at = now
    if received_on is not None and received_on != today:
        received_at = datetime(
            received_on.year, received_on.month, received_on.day, tzinfo=tz(zone)
        )
    return log_request(
        session,
        tenant_id,
        kind=kind,
        channel=channel,
        note=note,
        received_at=received_at,
        zone=zone,
        actor=actor,
    )


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
    session: Session, tenant_id: uuid.UUID, reference: str, lock: bool = False
) -> DataSubjectRequest | None:
    query = select(DataSubjectRequest).where(
        DataSubjectRequest.tenant_id == tenant_id,
        DataSubjectRequest.reference == reference,
    )
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    return session.scalar(query)


def get_request(
    session: Session, tenant_id: uuid.UUID, reference: str, *, lock: bool = False
) -> DataSubjectRequest:
    """`lock` for a write from the tab "Daten": two devices answer or extend one
    request one after the other, and the second sees what the first wrote."""
    request = _find(session, tenant_id, reference, lock)
    if request is None:
        raise NotFound(f"no request {reference}")
    return request


def answered_requests(
    session: Session, tenant_id: uuid.UUID, limit: int = 10
) -> Sequence[DataSubjectRequest]:
    """The newest answers, for the tab "Daten": the proof stays visible."""
    return session.scalars(
        select(DataSubjectRequest)
        .where(
            DataSubjectRequest.tenant_id == tenant_id,
            DataSubjectRequest.answered_at.is_not(None),
        )
        .order_by(
            DataSubjectRequest.answered_at.desc(), DataSubjectRequest.reference.desc()
        )
        .limit(limit)
    ).all()


def has_run(session: Session, request: DataSubjectRequest) -> bool:
    """Whether the erasure or the objection of this request ran on the server
    (`erase`, `record_objection`). An access request has no run."""
    action = RUN_ACTIONS.get(request.kind)
    if action is None:
        return False
    return (
        session.scalar(
            select(AuditLog.id)
            .where(
                AuditLog.tenant_id == request.tenant_id,
                AuditLog.entity == "data_subject_request",
                AuditLog.entity_id == request.id,
                AuditLog.action == action,
            )
            .limit(1)
        )
        is not None
    )


def look(
    session: Session,
    request: DataSubjectRequest,
    details: Details,
    zone: str,
    now: datetime,
    *,
    claimed: Collection[uuid.UUID] = (),
    actor: str,
) -> dict[str, Any]:
    """What `access_copy` holds for the person behind the details, for the tab
    "Daten". The look is in the audit log, committed, before a row is read:
    which request and who, nothing of the guest. Only for a request still open:
    without one nobody's rows are looked at."""
    if request.answered_at is not None:
        raise Conflict(f"{request.reference} is already answered")
    tenant_id = request.tenant_id
    _audit(session, request, ROWS_READ, actor)
    session.commit()
    person = verify(session, tenant_id, details, zone, now, claimed=claimed)
    return access_copy(session, tenant_id, person)


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


def can_extend(
    request: DataSubjectRequest, zone: str, now: datetime | None = None
) -> bool:
    """`now`, for the tab "Daten": the guest has to learn of the extension inside
    the first month (Art. 12(3)), so after it the page moves nothing, or an
    overdue request would read as in time. The job passes none: there the
    operator enters late what was told in time."""
    received = to_local(request.received_at, zone).date()
    if now is not None and to_local(now, zone).date() > add_months(received, 1):
        return False
    return request.answered_at is None and request.deadline < add_months(
        received, LONGEST_DEADLINE_MONTHS
    )


def extend_deadline(
    session: Session,
    request: DataSubjectRequest,
    zone: str,
    actor: str = ACTOR_JOB,
    now: datetime | None = None,
) -> None:
    """Art. 12(3): a complex request may take two more months, once."""
    if not can_extend(request, zone, now):
        raise Conflict(f"the deadline of {request.reference} cannot move")
    request.deadline = add_months(
        to_local(request.received_at, zone).date(), LONGEST_DEADLINE_MONTHS
    )
    _audit(session, request, "data_subject.deadline_extended", actor)
    session.flush()


def answer_request(
    session: Session,
    request: DataSubjectRequest,
    outcome: str,
    answered_at: datetime,
    zone: str,
    actor: str = ACTOR_JOB,
    *,
    needs_run: bool = False,
) -> None:
    """Closes the line and starts its periods (docs/03 Löschkonzept). The year is
    the restaurant's: a claim runs from the end of it (§ 199(1) BGB).
    `needs_run`, for the tab "Daten": an erasure or objection is not `done`
    before its run on the server, or the log would say erased while every row
    is still there."""
    if request.answered_at is not None:
        raise Conflict(f"{request.reference} is already answered")
    if outcome not in REQUEST_OUTCOMES:
        raise InvalidInput(f"no outcome {outcome!r}")
    if (
        needs_run
        and outcome == "done"
        and request.kind in RUN_ACTIONS
        and not has_run(session, request)
    ):
        raise Conflict(f"{request.reference} has not run", say=SAY_NOT_RUN)
    day = to_local(answered_at, zone).date()
    if day < to_local(request.received_at, zone).date():
        raise InvalidInput(f"{request.reference} cannot be answered before it came")
    request.answered_at = answered_at
    request.outcome = outcome
    request.delete_after = date(day.year + settings.retention_request_log_years, 12, 31)
    _audit(session, request, "data_subject.request_answered", actor, outcome=outcome)
    session.flush()


def _audit(
    session: Session,
    request: DataSubjectRequest,
    action: str,
    actor: str = ACTOR_JOB,
    **payload: Any,
) -> None:
    """No name and no number: only `verification_note` holds those."""
    session.add(
        AuditLog(
            tenant_id=request.tenant_id,
            actor=actor,
            action=action,
            entity="data_subject_request",
            entity_id=request.id,
            payload=payload,
        )
    )


def _folded(text: str) -> str:
    return fold(" ".join(text.split()))


def _local_day(column: Any, zone: str) -> ColumnElement[date]:
    return cast(func.timezone(zone, column), Date)


def _on_day(
    session: Session, tenant_id: uuid.UUID, day: date, zone: str, name: str
) -> list[Order | Reservation | Customer]:
    """What one day opens under the name: orders and reservations booked under
    the line their call came from and confirmed (security audit 02.11.2026: a
    caller can book a draft under any name and number, no yes needed), and
    customers."""
    orders = _own_line(
        session, Order, _local_day(ORDER_DAY, zone) == day, Order.tenant_id == tenant_id
    )
    tables = _own_line(
        session,
        Reservation,
        _local_day(Reservation.reserved_for, zone) == day,
        Reservation.tenant_id == tenant_id,
    )
    return [
        *(order for order in orders if _folded(order.customer_name) == name),
        *(table for table in tables if _folded(table.guest_name) == name),
        *_by_day(session, tenant_id, day, zone, name),
    ]


def _own_line(session: Session, model: Any, *where: Any) -> list[Any]:
    """Rows of `model` whose number is the one their call came from, as the
    draft read it: the caller ID goes with its period, an objection or an
    erasure, the booking keeps its name longer (#523). A withheld one proved
    nothing then and proves nothing now. Only a booking the guest said yes to:
    a presented number can be spoofed, and a draft needs nothing but a hang-up
    (#888 S4). The board cancels only a confirmed order (an erasure cancels a
    draft too, but takes its name); the tablet cancels a table draft (#862)."""
    yes = model.status != "draft" if model is Order else model.status == "confirmed"
    return list(
        session.scalars(select(model).where(model.booked_on_own_line, yes, *where))
    )


def _by_day(
    session: Session, tenant_id: uuid.UUID, day: date, zone: str, name: str
) -> list[Customer]:
    """The order of a customer loses its name after 90 days, the customer stays
    for 24 months: without this the row of a pickup guest could not be reached.
    Only a customer with a confirmed delivery from their own line: a delivery
    draft under a named number writes a row dated by its own insert, and only
    the own line moves the day (`customers.contact`). Never `last_contact_at`:
    a callback or a complaint moves it on the presented number alone, no yes
    (#888 S4)."""
    return [
        customer
        for customer in session.scalars(
            select(Customer).where(
                Customer.tenant_id == tenant_id,
                Customer.order_count > 0,
                _local_day(Customer.last_order_at, zone) == day,
            )
        )
        if _folded(customer.name) == name
    ]


def _by_address(
    session: Session, tenant_id: uuid.UUID, details: Details, name: str
) -> list[Customer]:
    """As in `_by_day`, only a customer with a confirmed delivery from their own
    line: the address of a draft under a named number is the planter's."""
    rows = session.execute(
        select(Customer, Address)
        .join(Address, Address.customer_id == Customer.id)
        .where(
            Customer.tenant_id == tenant_id,
            Customer.order_count > 0,
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
                Reservation.tenant_id == tenant_id,
                Reservation.guest_name != "",
                Reservation.call_id.is_not(None),
            )
        )
    ).all()
    names: dict[uuid.UUID, set[str]] = defaultdict(set)
    for call_id, name in rows:
        names[call_id].add(_folded(name))
    return names


def _national_digits(phone: str) -> str:
    """`calls.caller_id` holds the number as the line gave it, not always E.164:
    its digits only narrow the search, `_calls_and_callbacks` compares."""
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
    # A table the team entered by hand has no call (T-3.12).
    own_calls |= {table.call_id for table in tables if table.call_id is not None}
    own_names = {_folded(order.customer_name) for order in orders} | {
        _folded(table.guest_name) for table in tables
    }
    sole = {call for call in own_calls if names_on[call] <= own_names}
    offered: set[uuid.UUID] = set()

    def sort_out(
        row: Any, call_id: uuid.UUID, own: list[Any], unclaimed: list[Any]
    ) -> None:
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
        # The digits of a longer number hold the person's too: a switchboard's
        # extension, Berlin's 1234567 in 12345678 (#888 S3). Exact, as `line_of`.
        if call.id in own_calls or number_or_none(call.caller_id) in phones:
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
        raise InvalidInput(
            f"not on offer for this person: {sorted(map(str, unknown))}",
            say=SAY_CLAIM,
        )
    return calls, callbacks, unclaimed_calls, unclaimed_callbacks


def _one_person(
    rows: Sequence[Order | Reservation | Customer], said: set[str]
) -> list[Order | Reservation | Customer]:
    """What one detail opens, on the numbers the guest said if they said any.
    The name on two numbers is two people (L12): the union would hand one of
    them the other's data (security audit 02.11.2026)."""
    rows = [row for row in rows if not said or row.phone in said]
    if len({row.phone for row in rows}) > 1:
        raise Ambiguous(
            "not verified: one detail opens the name on several numbers; ask the "
            "guest for the number they called from (--phone)",
            say=SAY_NUMBERS,
        )
    return rows


def verify(
    session: Session,
    tenant_id: uuid.UUID,
    details: Details,
    zone: str,
    now: datetime,
    claimed: Collection[uuid.UUID] = (),
) -> Person:
    """The person behind the details, or `NotFound`. A second number needs a
    detail of its own and the guest's word (`phones`): a number the guest only
    names opens nothing."""
    name = _folded(details.name)
    address = (details.street, details.house_number, details.postal_code)
    if not name or not (details.days or all(address)):
        raise InvalidInput(
            "a name with the date of an order or reservation, or with the "
            "delivery address: never a number or a name alone",
            say=SAY_DETAILS,
        )
    said = {normalize_phone(phone) for phone in details.phones}
    found: list[Order | Reservation | Customer] = []
    for day in details.days:
        found += _one_person(_on_day(session, tenant_id, day, zone, name), said)
    if all(address):
        found += _one_person(_by_address(session, tenant_id, details, name), said)
    if not found:
        raise NotFound("not verified: no row carries these details", say=SAY_NOBODY)

    phones = {row.phone for row in found}
    # Two details on two numbers can be two namesakes as well (security re-audit
    # 02.11.2026, evening): only numbers the guest names join.
    if len(phones) > 1 and not said:
        raise Ambiguous(
            "not verified: the details open the name on several numbers; ask the "
            "guest for every number they called from and pass each with --phone",
            say=SAY_NUMBERS,
        )
    linked = {row.id for row in found if isinstance(row, Customer)}
    linked |= {
        row.customer_id for row in found if isinstance(row, Order) and row.customer_id
    }
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
    rows, its allergy note included. So does a draft its call can still confirm:
    the call has not ended and the draft is younger than the longest a call may
    last (docs/04 §confirm, the rule of `reservations/capacity.py`). No config, no
    `confirm`, so no draft is live. `erase` closes every other draft."""
    lifetime = timedelta(
        seconds=session.scalar(
            select(ServiceConfig.max_call_seconds).where(
                ServiceConfig.tenant_id == tenant_id
            )
        )
        or 0
    )

    def live(model: Any, rows: Sequence[Any]) -> set[uuid.UUID]:
        return set(
            session.scalars(
                select(model.id)
                .join(Call, Call.id == model.call_id)
                .where(
                    model.tenant_id == tenant_id,
                    model.id.in_([row.id for row in rows]),
                    model.status == "draft",
                    model.deleted_at.is_(None),
                    model.created_at > now - lifetime,
                    Call.ended_at.is_(None),
                )
            )
        )

    drafts = live(Order, orders) | live(Reservation, tables)
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
            or order.id in drafts
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
            or table.id in drafts
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
    recorded = session.scalars(
        select(Recording).where(
            Recording.tenant_id == tenant_id,
            Recording.call_id.in_([call.id for call in person.calls]),
        )
    )
    # A number-only call may hold a recording the copy cannot name before the
    # guest claims it: the team has to know to ask (T-11.3 security review).
    recorded_unclaimed = set(
        session.scalars(
            select(Recording.call_id).where(
                Recording.tenant_id == tenant_id,
                Recording.call_id.in_([call.id for call in person.unclaimed_calls]),
            )
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
            # The team hands the audio over from the folder (docs/09 §2).
            "recordings": [
                _fields(r, "call_id", "file_name", "started_at", "withdrawn_at")
                for r in recorded
            ],
        },
        "ask_the_guest": {
            "calls": [
                {**_call_fields(row), "recorded": row.id in recorded_unclaimed}
                for row in person.unclaimed_calls
            ],
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
    (docs/03). A recording is withdrawn here, and the next pass of the deletion
    job takes its file, the one process that holds the folder. Open rows stop it, so that no second run has to find a person
    whose name is already gone; `keep` names the ones the guest chose to keep. An
    order draft becomes `cancelled`: `confirm` takes any draft of its call, also
    of one that ended, and must not send the kitchen one without its notes. A
    reservation draft is deleted like any finished table."""
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
        .values(
            **ORDER_GONE,
            status=case((Order.status == "draft", "cancelled"), else_=Order.status),
        ),
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
    _write(
        session,
        update(Recording)
        .where(
            Recording.tenant_id == tenant_id,
            Recording.call_id.in_([call.id for call in person.calls]),
            Recording.withdrawn_at.is_(None),
        )
        .values(withdrawn_at=func.now()),
        Recording,
        "withdrawn",
        ledger,
    )
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
        ACTOR_JOB,
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
    # The rows that hold each number name it in the ledger, never the number:
    # a replay after a restore reads it from them again (#1085).
    holders: list[Customer | Order | Reservation] = [
        *person.customers,
        *person.orders,
        *person.reservations,
    ]
    ledger = [
        LedgerEntry(row.__tablename__, row.id, "objected")
        for row in holders
        if row.phone in person.phones
    ]
    for phone in sorted(person.phones):
        session.execute(
            insert(RecognitionObjection)
            .values(tenant_id=request.tenant_id, phone=phone)
            .on_conflict_do_nothing(constraint="uq_recognition_objections_phone")
        )
    _clear_calls(session, request.tenant_id, person.calls, ledger)
    _audit(session, request, "data_subject.objection_recorded")
    session.flush()
    session.expire_all()
    return ledger


def _phone_holder(model: Any) -> Callable[[list[uuid.UUID]], Any]:
    """An objection again, from a row that held its number at the time."""
    return lambda ids: (
        insert(RecognitionObjection)
        .from_select(
            ["tenant_id", "phone"],
            select(model.tenant_id, model.phone).where(
                model.id.in_(ids), model.phone != ""
            ),
        )
        .on_conflict_do_nothing(constraint="uq_recognition_objections_phone")
    )


# What a ledger line does again after a restore (U7, #1085), by (table, action),
# in this order: the objections first, while their rows still hold the number,
# then the steps of `erase`. A store that `erase` adds gets its line here too.
REPLAY: dict[tuple[str, str], Callable[[list[uuid.UUID]], Any]] = {
    (Customer.__tablename__, "objected"): _phone_holder(Customer),
    (Order.__tablename__, "objected"): _phone_holder(Order),
    (Reservation.__tablename__, "objected"): _phone_holder(Reservation),
    (OutboxEvent.__tablename__, "deleted"): lambda ids: delete(OutboxEvent).where(
        OutboxEvent.id.in_(ids)
    ),
    (OutboxEvent.__tablename__, "thinned"): lambda ids: (
        update(OutboxEvent)
        .where(OutboxEvent.id.in_(ids), OutboxEvent.payload != thin_payload())
        .values(payload=thin_payload())
    ),
    (Reservation.__tablename__, "deleted"): lambda ids: delete(Reservation).where(
        Reservation.id.in_(ids)
    ),
    (OrderItem.__tablename__, "cleared"): lambda ids: (
        update(OrderItem)
        .where(OrderItem.id.in_(ids), OrderItem.note.is_not(None))
        .values(note=None)
    ),
    (Order.__tablename__, "cleared"): lambda ids: (
        update(Order)
        .where(Order.id.in_(ids))
        .values(
            **ORDER_GONE,
            status=case((Order.status == "draft", "cancelled"), else_=Order.status),
        )
    ),
    (Callback.__tablename__, "cleared"): lambda ids: (
        update(Callback)
        .where(Callback.id.in_(ids), or_(Callback.phone != "", Callback.summary != ""))
        .values(**CALLBACK_GONE)
    ),
    (Call.__tablename__, "cleared"): lambda ids: (
        update(Call)
        .where(Call.id.in_(ids), Call.caller_id.is_not(None))
        .values(caller_id=None)
    ),
    (Recording.__tablename__, "withdrawn"): lambda ids: (
        update(Recording)
        .where(Recording.id.in_(ids), Recording.withdrawn_at.is_(None))
        .values(withdrawn_at=func.now())
    ),
    (Address.__tablename__, "deleted"): lambda ids: delete(Address).where(
        Address.id.in_(ids)
    ),
    (Customer.__tablename__, "deleted"): lambda ids: delete(Customer).where(
        Customer.id.in_(ids)
    ),
}


def read_ledger(lines: Iterable[str], since: date) -> list[LedgerEntry]:
    """The lines of the ledger file from the day of the dump on. One line this
    cannot read stops the whole replay: a line skipped would bring a row back."""
    entries = []
    for number, fields in enumerate(csv.reader(lines), start=1):
        if not fields:
            continue
        try:
            day, table, row_id, action = fields
            entry = LedgerEntry(table, uuid.UUID(row_id), action)
            if (table, action) not in REPLAY:
                raise ValueError(f"no replay for {table} {action}")
            if date.fromisoformat(day) >= since:
                entries.append(entry)
        except ValueError as exc:
            raise InvalidInput(
                f"ledger line {number}: {exc}; nothing replayed"
            ) from exc
    return entries


def replay_ledger(
    session: Session, tenant_id: uuid.UUID, entries: Sequence[LedgerEntry], since: date
) -> Counter[str]:
    """Every line again after a restore, so nothing erased comes back. By row id
    only: the ids are unique, and a line of another stack's ledger finds nothing.
    Idempotent, a line already done changes nothing. Nothing here commits."""
    ids: defaultdict[tuple[str, str], list[uuid.UUID]] = defaultdict(list)
    for entry in entries:
        ids[(entry.table, entry.action)].append(entry.row_id)
    changed: Counter[str] = Counter()
    for key, statement in REPLAY.items():
        if ids[key]:
            done = statement(ids[key])
            rows = session.scalars(
                done.returning(done.table.c.id).execution_options(
                    synchronize_session=False
                )
            )
            changed[" ".join(key)] += len(rows.all())
    session.add(
        AuditLog(
            tenant_id=tenant_id,
            actor=ACTOR_JOB,
            action="data_subject.ledger_replayed",
            entity="erasure_ledger",
            payload={"since": since.isoformat(), "lines": len(entries), **changed},
        )
    )
    session.flush()
    session.expire_all()
    return changed
