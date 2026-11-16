"""The customers of the dashboard and the state of each stack (#571, docs/23 §8).

The registry (`operator_customers`) says where a stack's ops API answers and
which token it takes; the operator fills it on the host (`dashboard/cli.py`).
`overview` asks every stack for `GET /v1/ops/status` at once and turns each
answer into one row of the list. No FastAPI here.

A stack's answer is input from outside: a customer's stack that was taken over
may answer anything. So the answer is read up to a size limit, checked field by
field (types, lengths, the list of alarms), must name the customer the row was
asked for, and is shown as text only. The token goes to the registered address
and nowhere else: no redirect is followed.
"""

import re
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as NotInTime
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Annotated
from urllib.parse import urlsplit

import httpx
from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
)
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from api.core.tokens import TOKEN_MIN_LENGTH, valid_production_token
from api.dashboard.models import Customer

# api/models/tenants.py CUSTOMER_KEY.
KEY = re.compile(r"[a-z][a-z0-9]*(-[a-z0-9]+)*")
# A container name or host on the stack's `ops` network, with a port.
HOST = re.compile(r"[a-z0-9]([a-z0-9._-]*[a-z0-9])?")
STATUS_PATH = "/v1/ops/status"
TIMEOUT_SECONDS = 3.0
# For the whole list. TIMEOUT_SECONDS only ends a wait without a byte: a stack
# that sends one byte every two seconds would keep the page for hours.
DEADLINE_SECONDS = 5.0
# The answer of a busy day is under 2 kB; more than this is not a status.
MAX_ANSWER_BYTES = 64 * 1024
MAX_AT_ONCE = 8

UNREACHABLE = "not reachable"
TOO_SLOW = "answers too slowly"
REFUSED = "refuses the token"
NOT_A_STATUS = "answer is not a status"
NO_RESTAURANT = "no restaurant in the stack yet"
OTHER_CUSTOMER = "another customer answers at this address"

Short = Annotated[str, Field(max_length=200)]
EARLIEST = datetime(2000, 1, 1, tzinfo=UTC)
LATEST = datetime(2100, 1, 1, tzinfo=UTC)


def _in_our_time(at: datetime) -> datetime:
    """A time the page can show: the year 9999 passes as a time with an offset
    and then overflows when it is turned into the restaurants' time."""
    if not EARLIEST <= at < LATEST:
        raise ValueError("a time outside 2000 to 2100")
    return at


Time = Annotated[AwareDatetime, AfterValidator(_in_our_time)]


class _Answer(BaseModel):
    # Fields a newer stack adds are ignored, so the two sides deploy apart.
    model_config = ConfigDict(extra="ignore")


class _Tenant(_Answer):
    key: Short | None
    name: Short


class _Today(_Answer):
    calls: int = Field(ge=0)
    orders: int = Field(ge=0)


class _Bridge(_Answer):
    configured: bool
    last_poll_at: Time | None
    silent: bool


class _Alarm(_Answer):
    kind: Short
    last_at: Time
    count: int = Field(ge=0)


class _Menu(_Answer):
    items: int = Field(ge=0)
    price_differences: int = Field(ge=0)
    without_allergen_information: int = Field(ge=0)


class StackStatus(_Answer):
    """`GET /v1/ops/status` as docs/04 "Ops API" gives it."""

    tenant: _Tenant
    call_mode: Short | None
    last_call_at: Time | None
    business_day: date
    today: _Today
    print_bridge: _Bridge
    backup_at: Time | None
    alarms: list[_Alarm] = Field(max_length=50)
    menu: _Menu


class _Error(_Answer):
    code: Short


class _Envelope(_Answer):
    """The API's answer hull (`api/core/envelope.py`): data, or an error code."""

    ok: bool
    data: StackStatus | None = None
    error: _Error | None = None


@dataclass(frozen=True)
class Row:
    """One customer in the list: its status, or the one sentence why none."""

    key: str
    status: StackStatus | None = None
    problem: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def tone(self) -> str:
        """`danger`: the stack gave no status. `warn`: it names something to
        look at. The page shows a glyph and a word with it, never colour alone."""
        if self.status is None:
            return "danger"
        return "warn" if self.notes else "ok"


@dataclass(frozen=True)
class Stack:
    """A customer as `overview` asks it: plain values, no database row. The
    stacks are asked from several threads, and a row belongs to one session."""

    key: str
    api_url: str
    ops_token: str = field(repr=False)


class BadCustomer(ValueError):
    """What the operator typed cannot be a customer of the dashboard."""


def checked_url(api_url: str) -> str:
    """`http://host:port`, nothing else: no path, no query, no user or password
    in it. Plain HTTP is right here, the `ops` network is internal to the host
    (docs/23 §8); `https` is taken too."""
    url = api_url.strip().rstrip("/")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as problem:
        raise BadCustomer(f"not an address: {problem}") from problem
    host = parts.hostname or ""
    if (
        parts.scheme not in ("http", "https")
        or not HOST.fullmatch(host)
        or port is None
        or parts.netloc != f"{host}:{port}"
        or parts.path
        or parts.query
        or parts.fragment
    ):
        raise BadCustomer("the address is http://<host>:<port>, nothing after it")
    return url


def add_customer(db: Session, key: str, api_url: str, token: str) -> Customer:
    """Add a customer or replace its address and token (a rotated `OPS_TOKEN`)."""
    key, token = key.strip(), token.strip()
    if not KEY.fullmatch(key):
        raise BadCustomer("the key is lower case letters, digits and single hyphens")
    if not valid_production_token(token):
        raise BadCustomer(
            f"the token is the stack's OPS_TOKEN: at least {TOKEN_MIN_LENGTH} "
            "characters, no whitespace, not the placeholder"
        )
    url = checked_url(api_url)
    row = db.get(Customer, key)
    if row is None:
        row = Customer(key=key, api_url=url, ops_token=token)
        db.add(row)
    else:
        row.api_url, row.ops_token = url, token
    try:
        db.commit()
    except IntegrityError as problem:
        db.rollback()
        raise BadCustomer(
            "another customer already has this address or this token"
        ) from problem
    return row


def remove_customer(db: Session, key: str) -> bool:
    row = db.get(Customer, key.strip())
    if row is None:
        return False
    db.delete(row)
    db.commit()
    return True


def list_customers(db: Session) -> list[Customer]:
    return list(db.scalars(select(Customer).order_by(Customer.key)))


def stacks(db: Session) -> list[Stack]:
    """Every customer as plain values, with the read's transaction ended: the
    stacks take up to `TIMEOUT_SECONDS`, and nothing stays open meanwhile."""
    found = [Stack(row.key, row.api_url, row.ops_token) for row in list_customers(db)]
    db.rollback()
    return found


def _client() -> httpx.Client:
    # trust_env off: no proxy variable of the host may carry a token elsewhere.
    return httpx.Client(
        timeout=TIMEOUT_SECONDS, follow_redirects=False, trust_env=False
    )


def _ask(client: httpx.Client, customer: Stack) -> Row:
    key = customer.key
    try:
        with client.stream(
            "GET",
            customer.api_url + STATUS_PATH,
            headers={"Authorization": f"Bearer {customer.ops_token}"},
        ) as answer:
            if answer.status_code in (401, 403):
                return Row(key, problem=REFUSED)
            body = b""
            for chunk in answer.iter_bytes():
                body += chunk
                if len(body) > MAX_ANSWER_BYTES:
                    return Row(key, problem=NOT_A_STATUS)
            status_code = answer.status_code
    except httpx.HTTPError:
        return Row(key, problem=UNREACHABLE)
    try:
        envelope = _Envelope.model_validate_json(body)
    except ValidationError:
        return Row(key, problem=NOT_A_STATUS)
    if envelope.error is not None and envelope.error.code == "not_found":
        return Row(key, problem=NO_RESTAURANT)
    status = envelope.data
    if status_code != 200 or not envelope.ok or status is None:
        return Row(key, problem=NOT_A_STATUS)
    if status.tenant.key != key:
        return Row(key, problem=OTHER_CUSTOMER)
    return Row(key, status=status, notes=_notes(status))


def _notes(status: StackStatus) -> list[str]:
    """What the operator should look at, in words. The stack decides what an
    alarm is (its monitor, its own limits); the list only repeats it."""
    notes = [f"{alarm.kind} ({alarm.count})" for alarm in status.alarms]
    if status.print_bridge.configured and status.print_bridge.silent:
        notes.append("print bridge silent")
    if status.backup_at is None:
        notes.append("no backup yet")
    return notes


def overview(
    customers: list[Stack], client: Callable[[], httpx.Client] | None = None
) -> list[Row]:
    """Every customer's row, the ones to look at first. Each stack is asked at
    once, and the whole list has `DEADLINE_SECONDS`: a stack that is not done
    by then is a row of its own and delays nobody else's. Closing the client
    ends what such a stack is still sending."""
    if not customers:
        return []
    http = (client or _client)()
    pool = ThreadPoolExecutor(max_workers=min(MAX_AT_ONCE, len(customers)))
    try:
        asked = [pool.submit(_ask, http, customer) for customer in customers]
        deadline = time.monotonic() + DEADLINE_SECONDS
        rows = []
        for customer, answer in zip(customers, asked, strict=True):
            try:
                rows.append(answer.result(max(0.0, deadline - time.monotonic())))
            except NotInTime:
                rows.append(Row(customer.key, problem=TOO_SLOW))
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
        http.close()
    order = {"danger": 0, "warn": 1, "ok": 2}
    return sorted(rows, key=lambda row: (order[row.tone], row.key))
