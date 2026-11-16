"""The customers of the dashboard and the state of each stack (#571, docs/23 §8).

The registry (`operator_customers`) says where a stack's ops API answers and
which token it takes; the operator fills it on the host (`dashboard/cli.py`).
`overview` asks every stack for `GET /v1/ops/status` at once and turns each
answer into one row of the list; `detail` asks one stack for its status and its
week's figures, `calls` for the calls of one business day. `step` reads a form
of the manage page as one request and `manage` sends it: the access read and
the six steps that show a secret or change the restaurant. No FastAPI here.

A stack's answer is input from outside: a customer's stack that was taken over
may answer anything. So the answer is read up to a size limit, checked field by
field (types, lengths, the list of alarms), must name the customer the row was
asked for, and is shown as text only. The token goes to the registered address
and nowhere else: no redirect is followed. Every question goes through `_fetch`
and `_at_once`, which hold these rules; there is no second way to a stack.
"""

import logging
import re
import time
import traceback
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as NotInTime
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from functools import partial
from typing import Annotated, Any
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

from api.core import ops_reason
from api.core.tokens import TOKEN_MIN_LENGTH, valid_production_token
from api.dashboard.models import Customer
from api.domain.access.hashing import PIN

logger = logging.getLogger(__name__)

# api/models/tenants.py CUSTOMER_KEY.
KEY = re.compile(r"[a-z][a-z0-9]*(-[a-z0-9]+)*")
# `ck_tenants_customer_key`: a longer key is one no stack's tenant can have.
KEY_MAX_LENGTH = 32
# A container name or host on the stack's `ops` network, with a port.
HOST = re.compile(r"[a-z0-9]([a-z0-9._-]*[a-z0-9])?")
STATUS_PATH = "/v1/ops/status"
FIGURES_PATH = "/v1/ops/figures"
CALLS_PATH = "/v1/ops/calls"
ACCESS_PATH = "/v1/ops/access"
PIN_SHOW_PATH = "/v1/ops/access/pin/show"
PIN_SET_PATH = "/v1/ops/access/pin"
SIGN_OUT_PATH = "/v1/ops/access/sign-out"
SETUP_LINK_PATH = "/v1/ops/access/setup-link"
CALL_MODE_PATH = "/v1/ops/call-mode"
TEAM_PHONE_PATH = "/v1/ops/team-phone"
# `/customers/rows` is the list's refresh, so no customer's page can be there.
RESERVED_KEYS = frozenset({"rows"})
TIMEOUT_SECONDS = 3.0
# For the whole list. TIMEOUT_SECONDS only ends a wait without a byte: a stack
# that sends one byte every two seconds would keep the page for hours.
DEADLINE_SECONDS = 5.0
# The answer of a busy day is under 2 kB; more than this is not a status.
MAX_ANSWER_BYTES = 64 * 1024
# A call is under 250 bytes, and a restaurant does not take 2000 calls a day.
MAX_CALLS = 2000
MAX_CALLS_BYTES = 512 * 1024
# As `domain/operator.py` MAX_DEVICES: a stack sends its newest devices.
MAX_DEVICES = 200
# One call's tool calls; the stack sends no more (`domain/calls/log.py`).
MAX_TOOL_CALLS = 200
# And as its MAX_NUMBERS: the numbers a team number can be chosen from.
MAX_NUMBERS = 20
# What a form of the manage page may send and a stack may answer, each in
# full; a PIN by the stack's own rule (`domain/access/hashing.py` PIN).
PINS = ("team", "admin")
# A mode of the switch as a code; which codes there are is the stack's word.
MODE = re.compile(r"[a-z_]{1,20}")
# `domain/customers/phone.py` E164.
PHONE = re.compile(r"\+[1-9][0-9]{6,14}")
# A number as a stack's settings write it, which is not always E.164
# ("0721 555-1234"): digits and what stands between them, nothing to read.
SHOWN_PHONE = re.compile(r"[0-9+(][0-9 ()/.+-]{2,39}")
# The setup link as `domain/access/setup.py` builds it: the customer's own
# https host (as `AGENT_DOMAIN` writes it) or none, the one path, and the
# token as the fragment.
SETUP_LINK = re.compile(
    r"(https://[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?(:[0-9]{1,5})?)?"
    r"/gui/einrichten#[A-Za-z0-9_-]{20,100}"
)
ALL_DEVICES = "all"

UNREACHABLE = "not reachable"
TOO_SLOW = "answers too slowly"
NOT_ASKED = "could not be asked"
REFUSED = "refuses the token"
NOT_A_STATUS = "answer is not a status"
NOT_AN_ANSWER = "answer cannot be read"
NOT_TAKEN = "does not take this request"
NOT_KNOWN = "does not know this question (an older version?)"
NO_RESTAURANT = "no restaurant in the stack yet"
OTHER_CUSTOMER = "another customer answers at this address"
NO_KEY = "its restaurant has no customer key yet"
WRONG_STACK = "is not this customer's stack (another key, or none): nothing was done"
REFUSED_BY_RULE = "refuses this by a rule of its own"
NO_LINK = "answered without a link"
NO_SUCH_CALL = (
    "knows no call of this id (or the call is older than the call list reaches)"
)
# After these the stack did nothing; after any other problem of a step that
# changes something, nobody knows.
NOTHING_DONE = frozenset(
    {REFUSED, NOT_KNOWN, NOT_TAKEN, NO_RESTAURANT, WRONG_STACK, REFUSED_BY_RULE}
)

Short = Annotated[str, Field(max_length=200)]
EARLIEST = datetime(2000, 1, 1, tzinfo=UTC)
LATEST = datetime(2100, 1, 1, tzinfo=UTC)


def _in_our_time(at: datetime) -> datetime:
    """A time the page can show: the year 9999 passes as a time with an offset
    and then overflows when it is turned into the restaurants' time."""
    if not EARLIEST <= at < LATEST:
        raise ValueError("a time outside 2000 to 2100")
    return at


def _in_our_days(day: date) -> date:
    """The same for a day: the page counts back from it, and the year 1 has no
    day before it."""
    if not EARLIEST.date() <= day < LATEST.date():
        raise ValueError("a day outside 2000 to 2100")
    return day


Time = Annotated[AwareDatetime, AfterValidator(_in_our_time)]
Day = Annotated[date, AfterValidator(_in_our_days)]
# What a count or an amount in cents can be: a number of a thousand digits is
# valid JSON too, and the page would show it.
Count = Annotated[int, Field(ge=0, le=10**9)]
Percent = Annotated[int, Field(ge=0, le=100)]


class _Answer(BaseModel):
    # Fields a newer stack adds are ignored, so the two sides deploy apart.
    model_config = ConfigDict(extra="ignore")


class _Tenant(_Answer):
    key: Short | None
    name: Short


class _Today(_Answer):
    calls: Count
    orders: Count


class _Bridge(_Answer):
    configured: bool
    last_poll_at: Time | None
    silent: bool


class _Alarm(_Answer):
    kind: Short
    last_at: Time
    count: Count


class _Menu(_Answer):
    items: Count
    price_differences: Count
    without_allergen_information: Count


class _Named(_Answer):
    """Every answer names its restaurant, and `_fetch` compares it."""

    tenant: _Tenant


class StackStatus(_Named):
    """`GET /v1/ops/status` as docs/04 "Ops API" gives it."""

    call_mode: Short | None
    last_call_at: Time | None
    business_day: Day
    today: _Today
    print_bridge: _Bridge
    backup_at: Time | None
    alarms: list[_Alarm] = Field(max_length=50)
    menu: _Menu


class _Figures(_Answer):
    """The four figures of docs/06 §4 with what they are counted from. None:
    nothing to divide by."""

    calls: Count
    orders: Count
    missed: Count
    unknown_cost_calls: Count
    accuracy_percent: Percent | None
    cost_per_order_cents: Count | None
    handed_over_percent: Percent | None


class _DayFigures(_Figures):
    day: Day


class StackFigures(_Named):
    """`GET /v1/ops/figures`: the last seven business days in one, and each."""

    business_day: Day
    week: _Figures
    days: list[_DayFigures] = Field(max_length=31)


class _Call(_Answer):
    started_at: Time
    # The day's callers counted, never a number (`domain/calls/log.py`).
    caller: Annotated[int, Field(ge=1, le=MAX_CALLS)] | None
    intent: Short | None
    outcome: Short | None
    duration_seconds: Annotated[int, Field(ge=0, le=7 * 24 * 3600)] | None
    cost_cents: Count | None
    tool_calls: Count
    line_note: Short | None


class StackCalls(_Named):
    """`GET /v1/ops/calls`: the calls of one business day."""

    business_day: Day
    calls: list[_Call] = Field(max_length=MAX_CALLS)


class _ToolCall(_Answer):
    name: Short
    duration_ms: Annotated[float, Field(ge=0, le=3_600_000, allow_inf_nan=False)] | None
    ok: bool
    error_code: Short | None


class _CallLog(_Answer):
    started_at: Time
    ended_at: Time | None
    duration_seconds: Annotated[int, Field(ge=0, le=7 * 24 * 3600)] | None
    intent: Short | None
    outcome: Short | None
    transfer_reason: Short | None
    line_note: Short | None
    cost_cents: Count | None
    model: Short | None
    is_test: bool
    tool_calls: list[_ToolCall] = Field(max_length=MAX_TOOL_CALLS)
    tool_calls_total: Count


class StackCall(_Named):
    """`GET /v1/ops/calls/<call_id>`: one call with its tool calls, or none
    of that id."""

    call_id: uuid.UUID
    call: _CallLog | None


def _full(pattern: re.Pattern[str]) -> AfterValidator:
    """The whole text is the pattern, by Python's rule: no line break after it."""

    def check(text: str) -> str:
        if not pattern.fullmatch(text):
            raise ValueError("not of the expected form")
        return text

    return AfterValidator(check)


class _Device(_Answer):
    id: uuid.UUID
    # What the browser called itself, cut by the stack (`DEVICE_CHARS`).
    device: Short
    created_at: Time
    last_seen_at: Time


class StackAccess(_Named):
    """`GET /v1/ops/access` and the answer of every step: the restaurant's
    login, devices, call mode and team number after it. `pin`, `link` and
    `signed_out` are what one step shows besides; no other field is a secret,
    and a field this page does not know is dropped."""

    login: bool
    team_pin_set: bool
    admin_pin_set: bool
    devices: list[_Device] = Field(max_length=MAX_DEVICES)
    call_mode: Annotated[str, _full(MODE)] | None
    call_modes: list[Annotated[str, _full(MODE)]] = Field(max_length=10)
    team_phone: Annotated[str, _full(SHOWN_PHONE)] | None
    team_phone_standard: Annotated[str, _full(SHOWN_PHONE)] | None
    # When a number set for today ends; None: the standard number holds.
    team_phone_until: Time | None
    team_phones: list[Annotated[str, _full(PHONE)]] = Field(max_length=MAX_NUMBERS)
    pin: Annotated[str, _full(PIN)] | None = None
    link: Annotated[str, _full(SETUP_LINK)] | None = None
    link_expires_at: Time | None = None
    signed_out: Count | None = None


class _Error(_Answer):
    code: Short


class _Envelope[Asked: _Named](_Answer):
    """The API's answer hull (`api/core/envelope.py`): data, or an error code."""

    ok: bool
    data: Asked | None = None
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
class Detail:
    """One customer's page: its row of the list, and the week's figures or the
    one sentence why there are none."""

    row: Row
    figures: StackFigures | None = None
    figures_problem: str = ""


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
    (docs/23 §8), and `https` is not taken: its handshake is one more wait
    that the deadline of the list cannot end."""
    url = api_url.strip().rstrip("/")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as problem:
        raise BadCustomer(f"not an address: {problem}") from problem
    host = parts.hostname or ""
    # What is stored is built from the parts, and the input must be that and
    # nothing more: `http://api:8000?` has an empty query, which is no query
    # to `urlsplit`, and the question's path would be glued on behind the `?`.
    built = f"{parts.scheme}://{host}:{port}"
    if (
        parts.scheme != "http"
        or not HOST.fullmatch(host)
        or port is None
        or url != built
    ):
        raise BadCustomer("the address is http://<host>:<port>, nothing after it")
    return built


def add_customer(db: Session, key: str, api_url: str, token: str) -> Customer:
    """Add a customer or replace its address and token (a rotated `OPS_TOKEN`)."""
    key, token = key.strip(), token.strip()
    if not KEY.fullmatch(key) or len(key) > KEY_MAX_LENGTH or key in RESERVED_KEYS:
        raise BadCustomer(
            "the key is lower case letters, digits and single hyphens, at most "
            f"{KEY_MAX_LENGTH} characters, and not " + ", ".join(sorted(RESERVED_KEYS))
        )
    # ASCII: the token travels in a header, which takes nothing else.
    if not (valid_production_token(token) and token.isascii() and token.isprintable()):
        raise BadCustomer(
            f"the token is the stack's OPS_TOKEN: at least {TOKEN_MIN_LENGTH} "
            "characters, plain ASCII, no whitespace, not the placeholder"
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


def stack(db: Session, key: str) -> Stack | None:
    """One customer as plain values, or None for a key the registry does not
    hold. The key is part of an address the browser asked for."""
    row = db.get(Customer, key) if KEY.fullmatch(key) else None
    found = Stack(row.key, row.api_url, row.ops_token) if row else None
    db.rollback()
    return found


def _client() -> httpx.Client:
    # trust_env off: no proxy variable of the host may carry a token elsewhere.
    return httpx.Client(
        timeout=TIMEOUT_SECONDS, follow_redirects=False, trust_env=False
    )


def _fetch[Asked: _Named](
    client: httpx.Client,
    customer: Stack,
    path: str,
    hull: type[_Envelope[Asked]],
    *,
    headers: dict[str, str] | None = None,
    limit: int = MAX_ANSWER_BYTES,
    unreadable: str = NOT_AN_ANSWER,
    method: str = "GET",
    sent: dict[str, Any] | None = None,
    by_rule: str = NOT_TAKEN,
) -> Asked | str:
    """One answer of a stack, checked, or the one sentence why there is none.
    Asked once: a step that got no answer is never sent again by itself."""
    try:
        with client.stream(
            method,
            customer.api_url + path,
            json=sent,
            headers={
                **(headers or {}),
                "Authorization": f"Bearer {customer.ops_token}",
                "Accept-Encoding": "identity",
            },
        ) as answer:
            if answer.status_code in (401, 403):
                return REFUSED
            # A path the stack does not have, not `not_found` in a 200: the
            # two sides deploy apart, and that is no missing restaurant.
            if answer.status_code == 404:
                return NOT_KNOWN
            # The limit below counts what arrives. A packed answer would be
            # unpacked first, 32 kB into 32 MB, so none is asked for or taken:
            # without this header HTTPX passes the bytes on as they are.
            if answer.headers.get("content-encoding", "identity") != "identity":
                return unreadable
            body = b""
            for chunk in answer.iter_bytes():
                body += chunk
                if len(body) > limit:
                    return unreadable
            status_code = answer.status_code
    except httpx.HTTPError:
        return UNREACHABLE
    try:
        envelope = hull.model_validate_json(body)
    except ValidationError:
        return unreadable
    code = envelope.error.code if envelope.error is not None else ""
    # The stack's own `NotFound` comes with 200 (`api/core/errors.py`); the
    # same code under another status is something else's answer.
    if code == "not_found" and status_code == 200:
        return NO_RESTAURANT
    # A step marks what one of the stack's rules refused with a status of its
    # own (`domain/operator.py` `_by_rule`); with any other the request itself
    # was not taken: a header, the body's form. Only the first is explained as
    # a rule.
    if code == "invalid_input":
        return by_rule if status_code == ops_reason.RULE_STATUS else NOT_TAKEN
    # `_customer` of the stack's router: its restaurant is not the customer
    # this request names. With any other status the code is something else's.
    if code == "conflict" and status_code == 409:
        return WRONG_STACK
    data = envelope.data
    if status_code != 200 or not envelope.ok or data is None:
        return unreadable
    # A tenant without a key (the seed's, or one older than migration 035) is
    # not another customer, and still nothing ties its answer to this one.
    if data.tenant.key is None:
        return NO_KEY
    if data.tenant.key != customer.key:
        return OTHER_CUSTOMER
    return data


def _ask(client: httpx.Client, customer: Stack) -> Row:
    found = _fetch(
        client, customer, STATUS_PATH, _Envelope[StackStatus], unreadable=NOT_A_STATUS
    )
    if isinstance(found, str):
        return Row(customer.key, problem=found)
    return Row(customer.key, status=found, notes=_notes(found))


def _notes(status: StackStatus) -> list[str]:
    """What the operator should look at, in words. The stack decides what an
    alarm is (its monitor, its own limits); the list only repeats it."""
    notes = [f"{alarm.kind} ({alarm.count})" for alarm in status.alarms]
    if status.print_bridge.configured and status.print_bridge.silent:
        notes.append("print bridge silent")
    if status.backup_at is None:
        notes.append("no backup yet")
    return notes


def _at_once[Found](
    asks: list[tuple[str, Callable[[httpx.Client], Found]]],
    client: Callable[[], httpx.Client] | None = None,
) -> list[Found | str]:
    """Every question (a customer's key and what to ask) at once: each one's
    answer, or the sentence why there is none. All of them together have
    `DEADLINE_SECONDS`: a stack that is not done by then delays nobody else.
    Closing the client ends what such a stack is still sending."""
    http = (client or _client)()
    # A worker for every question: in a smaller pool the slow stacks keep the
    # others waiting in the queue, and those are then "too slow" unasked.
    # shortcut: a thread per customer and refresh, right for a registry the
    # operator types in by hand; ask in batches once it holds hundreds.
    pool = ThreadPoolExecutor(max_workers=len(asks))
    found: list[Found | str] = []
    try:
        asked = [pool.submit(ask, http) for _, ask in asks]
        deadline = time.monotonic() + DEADLINE_SECONDS
        for (key, _), answer in zip(asks, asked, strict=True):
            try:
                found.append(answer.result(max(0.0, deadline - time.monotonic())))
            except NotInTime:
                found.append(TOO_SLOW)
            except Exception as problem:  # noqa: BLE001  one row, not the list
                # Whatever one customer's question trips over while it is
                # asked, the others are still shown. (What a stack answers is
                # kept from breaking the drawn page by the checks above.)
                # The kind of failure and where, not its message: that can
                # quote the header it failed on, and the token is in it.
                logger.error(
                    "customer %s could not be asked: %s\n%s",
                    key,
                    type(problem).__name__,
                    "".join(traceback.format_tb(problem.__traceback__)),
                )
                found.append(NOT_ASKED)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
        http.close()
    return found


def _row(key: str, found: object) -> Row:
    if isinstance(found, Row):
        return found
    return Row(key, problem=found if isinstance(found, str) else NOT_ASKED)


def overview(
    customers: list[Stack], client: Callable[[], httpx.Client] | None = None
) -> list[Row]:
    """Every customer's row, the ones to look at first; a stack that is not
    done in time is a row of its own."""
    if not customers:
        return []
    found = _at_once(
        [(customer.key, partial(_ask, customer=customer)) for customer in customers],
        client,
    )
    rows = [_row(c.key, one) for c, one in zip(customers, found, strict=True)]
    order = {"danger": 0, "warn": 1, "ok": 2}
    return sorted(rows, key=lambda row: (order[row.tone], row.key))


def detail(customer: Stack, client: Callable[[], httpx.Client] | None = None) -> Detail:
    """One customer's status and week, asked at once. Neither holds guest data,
    so neither needs a reason (docs/23 §8)."""
    asks: list[tuple[str, Callable[[httpx.Client], Row | StackFigures | str]]] = [
        (customer.key, partial(_ask, customer=customer)),
        (
            customer.key,
            partial(
                _fetch,
                customer=customer,
                path=FIGURES_PATH,
                hull=_Envelope[StackFigures],
            ),
        ),
    ]
    status, week = _at_once(asks, client)
    row = _row(customer.key, status)
    if isinstance(week, StackFigures):
        return Detail(row, figures=week)
    return Detail(row, figures_problem=week if isinstance(week, str) else NOT_ASKED)


def calls(
    customer: Stack,
    day: date,
    actor: str,
    reason: str,
    client: Callable[[], httpx.Client] | None = None,
) -> StackCalls | str:
    """The calls of one business day: guest data, so the stack is told who
    asks and why (`api/core/ops_reason.py`) and writes that down before it
    answers. `actor` is the session's account, never something a form sent."""
    if not ops_reason.ACTOR.fullmatch(actor) or ops_reason.parse(reason) is None:
        raise ValueError("no account or no reason: nothing is asked")
    ask = partial(
        _fetch,
        customer=customer,
        path=f"{CALLS_PATH}?day={day.isoformat()}",
        hull=_Envelope[StackCalls],
        headers={ops_reason.REASON_HEADER: reason, ops_reason.ACTOR_HEADER: actor},
        limit=MAX_CALLS_BYTES,
    )
    found = _at_once([(customer.key, ask)], client)[0]
    if isinstance(found, StackCalls) and found.business_day != day:
        return NOT_AN_ANSWER
    return found


def call_log(
    customer: Stack,
    call_id: uuid.UUID,
    actor: str,
    reason: str,
    client: Callable[[], httpx.Client] | None = None,
) -> StackCall | str:
    """One call with its tool calls, for the dev tools (docs/23 §5): what a
    guest did, so the stack is told who asks and why, as for `calls`. A read:
    the question is a GET, and the stack writes its audit row and nothing
    else. An answer about another call than the one asked is no answer."""
    if not ops_reason.ACTOR.fullmatch(actor) or ops_reason.parse(reason) is None:
        raise ValueError("no account or no reason: nothing is asked")
    ask = partial(
        _fetch,
        customer=customer,
        path=f"{CALLS_PATH}/{call_id}",
        hull=_Envelope[StackCall],
        headers={ops_reason.REASON_HEADER: reason, ops_reason.ACTOR_HEADER: actor},
    )
    found = _at_once([(customer.key, ask)], client)[0]
    if isinstance(found, StackCall) and found.call_id != call_id:
        return NOT_AN_ANSWER
    return found


@dataclass(frozen=True)
class Step:
    """One request of the manage page: where it goes, what it carries, and
    what both audits may say of it. `body` can hold a new PIN and is in no
    printed form; `detail` never holds a PIN, a link or a telephone number."""

    name: str
    path: str
    body: dict[str, Any] | None = field(default=None, repr=False)
    detail: dict[str, str] = field(default_factory=dict)

    @property
    def method(self) -> str:
        return "GET" if self.path == ACCESS_PATH else "POST"

    @property
    def changes(self) -> bool:
        """Whether the stack may be another after it: everything but the read
        and the look at a PIN."""
        return self.path not in (ACCESS_PATH, PIN_SHOW_PATH)


def _device(text: str) -> str | None:
    """A device's id as the stack wrote it, or None."""
    try:
        found = str(uuid.UUID(text))
    except ValueError:
        return None
    return found if found == text else None


def _open(form: dict[str, str]) -> Step | None:
    return Step("access", ACCESS_PATH)


def _pin_show(form: dict[str, str]) -> Step | None:
    which = form.get("which", "")
    if which not in PINS:
        return None
    return Step("pin_show", PIN_SHOW_PATH, {"which": which}, {"which": which})


def _pin_set(form: dict[str, str]) -> Step | None:
    which, pin = form.get("which", ""), form.get("pin", "")
    if which not in PINS or not PIN.fullmatch(pin):
        return None
    return Step("pin_set", PIN_SET_PATH, {"which": which, "pin": pin}, {"which": which})


def _sign_out(form: dict[str, str]) -> Step | None:
    device = form.get("device", "")
    if device == ALL_DEVICES:
        return Step("sign_out", SIGN_OUT_PATH, {"all": True}, {"device": device})
    if _device(device) is None:
        return None
    return Step("sign_out", SIGN_OUT_PATH, {"device": device}, {"device": device})


def _setup_link(form: dict[str, str]) -> Step | None:
    return Step("setup_link", SETUP_LINK_PATH)


def _call_mode(form: dict[str, str]) -> Step | None:
    mode = form.get("mode", "")
    if not MODE.fullmatch(mode):
        return None
    return Step("call_mode", CALL_MODE_PATH, {"mode": mode}, {"mode": mode})


def _team_phone(form: dict[str, str]) -> Step | None:
    number = form.get("number", "")
    if not PHONE.fullmatch(number):
        return None
    return Step("team_phone", TEAM_PHONE_PATH, {"number": number})


# The last part of the manage page's addresses, and how each reads its form.
STEPS: dict[str, Callable[[dict[str, str]], Step | None]] = {
    "open": _open,
    "pin-show": _pin_show,
    "pin-set": _pin_set,
    "sign-out": _sign_out,
    "setup-link": _setup_link,
    "call-mode": _call_mode,
    "team-phone": _team_phone,
}


def step(action: str, form: dict[str, str]) -> Step | None:
    """What a form of the manage page asks for, or None when it is no step:
    the shape is checked here, so nothing odd is sent; whether a PIN, a mode
    or a number is taken is the stack's decision."""
    read = STEPS.get(action)
    return read(form) if read else None


def manage(
    customer: Stack,
    asked: Step,
    actor: str,
    reason: str,
    client: Callable[[], httpx.Client] | None = None,
) -> StackAccess | str:
    """One step on a customer's access or call routing, and the state after
    it. The stack is told who asks and why, as for guest data, and which
    customer is meant: it compares that with its own restaurant before it
    shows or changes anything. `actor` is the session's account, never
    something a form sent."""
    if not ops_reason.ACTOR.fullmatch(actor) or ops_reason.parse(reason) is None:
        raise ValueError("no account or no reason: nothing is asked")
    ask = partial(
        _fetch,
        customer=customer,
        path=asked.path,
        hull=_Envelope[StackAccess],
        headers={
            ops_reason.REASON_HEADER: reason,
            ops_reason.ACTOR_HEADER: actor,
            ops_reason.CUSTOMER_HEADER: customer.key,
        },
        method=asked.method,
        sent=asked.body,
        by_rule=REFUSED_BY_RULE,
    )
    found = _at_once([(customer.key, ask)], client)[0]
    # The step that exists for the link: an answer without one is no answer,
    # though the stack may have made a link and voided the one before.
    if asked.path == SETUP_LINK_PATH and isinstance(found, StackAccess):
        return found if found.link else NO_LINK
    return found
