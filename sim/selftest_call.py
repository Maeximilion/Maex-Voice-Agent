"""The self-test's reservation call (#574): one call through the agent, shown on
the tablet as a TEST card for a while, then removed.

The scripted stand-in model (`scripted_llm.py`) books a table for two on the
first free time from today on, the way an eval does it (`replay.py`): the
draft must be there before the yes, `confirm` alone turns it into a booking,
and the booking must reach what the tablet reads: the day's list of the
operations view (`list_today`) and the change token its event stream polls
(`today_change_token`, `gui/sse.py`).

The call is a test call (`calls.is_test`, migration 039), and so is its table:
`confirm` writes no outbox event and no customer contact for it, so nothing
reaches n8n, the guest or the team, and the figures, the call log and the day
report leave it out. The call carries its own `call_id` like every call
(CLAUDE.md §8).

The call runs inside one database transaction. The code commits as on a real
call (`start_call`, `confirm`); those commits end savepoints only
(`join_transaction_mode="create_savepoint"`), and a checker on a second
connection proves that nothing is visible yet. A call that fails a check is
rolled back and leaves no row. One that passes is committed: the tablet then
shows its table as a card marked TEST for `show` seconds (Maxi 15.11.2026,
only when the table is on today's business day, the only day the tablet
lists), and the self-test deletes the call and its rows again. A run that was
killed in between leaves its card; the next run removes it first.

While the call runs (about a second), the transaction holds the day lock of
the chosen day and a share lock on `service_config`: a real booking for that
day, or a change on the tablet's settings, waits that long. While the card is
shown, its two seats count as booked.
"""

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import Select, Text, cast, delete, func, or_, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from api.domain.reservations import list_today, today_change_token
from api.domain.reservations.capacity import capacity_windows
from api.domain.reservations.slots import check_slot
from api.models import AuditLog, Call, OutboxEvent, Reservation, ServiceConfig, Tenant
from sim.session import SimCall

PARTY = 2
# The furthest day looked at, whatever `reservation_max_days_ahead` allows.
DAYS_AHEAD = 14
GUEST = "Selbsttest"
# A number of the evals; nobody is called on it, nothing of a test call is sent.
PHONE = "0721 5551234"
CALL_PREFIX = "selftest-"
# How long the TEST card stays on the tablet before the self-test removes it.
SHOW_SECONDS = 60.0


class CallFailed(Exception):
    """The call did not end as a booking the tablet sees; the message says where."""


@dataclass(frozen=True)
class CallReport:
    call_id: uuid.UUID
    reservation_id: uuid.UUID
    reserved_for: datetime
    tools: list[str]
    # The card was on today's tablet (for `show` seconds), not on a later day's.
    shown_today: bool


def first_free_slot(session: Session, tenant: Tenant, now: datetime) -> datetime:
    """The first time from today on that `check_slot` gives a table for two."""
    config = session.get(ServiceConfig, tenant.id)
    if config is None:
        raise CallFailed("no service_config")
    if not config.reservations_enabled:
        raise CallFailed("reservations are switched off on the tablet")
    zone = ZoneInfo(tenant.timezone)
    today = now.astimezone(zone).date()
    last = min(DAYS_AHEAD, config.reservation_max_days_ahead)
    for ahead in range(last + 1):
        day = today + timedelta(days=ahead)
        for window in capacity_windows(session, tenant.id, day, zone):
            for slot in window.grid():
                # check_slot refuses a time that passed: today's grid starts at opening.
                if slot <= now:
                    continue
                if check_slot(session, tenant.id, slot, PARTY, now=now).available:
                    return slot
    raise CallFailed(
        f"no free table for {PARTY} in the next {last} days "
        "(opening hours, seats, special days)"
    )


def _lines(slot: datetime) -> list[str]:
    return [
        f"Guten Tag, ich hätte gern einen Tisch für {PARTY} Personen "
        f"am {slot:%d.%m.%Y} um {slot:%H:%M} Uhr.",
        f"Auf den Namen {GUEST}.",
        f"Meine Nummer ist {PHONE}.",
    ]


def _reservation(session: Session, call_id: uuid.UUID) -> Reservation | None:
    return session.scalars(
        select(Reservation)
        .where(Reservation.call_id == call_id)
        .order_by(Reservation.created_at.desc())
        .execution_options(populate_existing=True)
    ).first()


def _events_of(
    call_id: uuid.UUID, reservation_id: uuid.UUID | None = None
) -> Select[int]:
    """Outbox events naming the call or its table."""
    named = [OutboxEvent.payload["call_id"].astext == str(call_id)]
    if reservation_id is not None:
        named.append(
            OutboxEvent.payload["reservation_id"].astext == str(reservation_id)
        )
    return select(func.count()).where(or_(*named))


def rows_of(session: Session, call_id: uuid.UUID) -> int:
    """Rows of this call any other connection can see: the call, its
    reservations, its outbox events and its audit rows."""
    key = str(call_id)
    counts = (
        select(func.count()).select_from(Call).where(Call.id == call_id),
        select(func.count()).where(Reservation.call_id == call_id),
        _events_of(call_id),
        select(func.count()).where(AuditLog.payload["call_id"].astext == key),
    )
    return sum(session.scalar(query) or 0 for query in counts)


def remove_test_rows(session: Session, tenant_id: uuid.UUID) -> None:
    """Delete every self-test call of the tenant with its tables and their
    audit rows, and commit. A test call has no order, callback or consent row:
    one that had would fail its checks and be rolled back."""
    calls = select(Call.id).where(Call.tenant_id == tenant_id, Call.is_test)
    keys = select(cast(Call.id, Text)).where(Call.tenant_id == tenant_id, Call.is_test)
    cards = select(Reservation.id).where(
        Reservation.tenant_id == tenant_id, Reservation.call_id.in_(calls)
    )
    session.execute(
        delete(AuditLog).where(
            AuditLog.tenant_id == tenant_id,
            or_(
                AuditLog.entity_id.in_(cards),
                AuditLog.payload["call_id"].astext.in_(keys),
            ),
        )
    )
    session.execute(delete(Reservation).where(Reservation.id.in_(cards)))
    session.execute(delete(Call).where(Call.id.in_(calls)))
    session.commit()


def _converse(
    session: Session, tenant: Tenant, now: datetime, slot: datetime
) -> SimCall:
    call = SimCall(
        session,
        tenant,
        now=now,
        external_session_id=f"{CALL_PREFIX}{uuid.uuid4().hex[:8]}",
        is_test=True,
    )
    said: list[str] = []
    for line in _lines(slot):
        turn = call.say(line)
        said = turn.say
        if turn.ended:
            raise CallFailed(f"the agent ended the call: {' '.join(said)}")
    draft = _reservation(session, call.call_id)
    if draft is None or draft.status != "draft":
        state = "none" if draft is None else draft.status
        raise CallFailed(f"no draft before the yes ({state}); agent: {' '.join(said)}")
    call.say("Ja, passt so.")
    return call


def _check_booking(
    session: Session, tenant: Tenant, call: SimCall, slot: datetime, before: str
) -> tuple[uuid.UUID, list[str]]:
    booked = _reservation(session, call.call_id)
    if booked is None or booked.status != "confirmed":
        raise CallFailed("the yes did not confirm the draft")
    if not booked.is_test:
        raise CallFailed("the table of the test call is not marked as a test")
    tools = [
        str(entry.get("name"))
        for entry in session.scalar(
            select(Call.tool_calls).where(Call.id == call.call_id)
        )
        or []
    ]
    drafted = "create_reservation" in tools and "confirm" in tools
    if not drafted or tools.index("confirm") < tools.index("create_reservation"):
        raise CallFailed(f"booked without confirm after the draft: {tools}")
    audit = session.scalar(
        select(AuditLog.actor).where(
            AuditLog.entity_id == booked.id, AuditLog.action == "reservation.confirmed"
        )
    )
    if audit != "agent":
        raise CallFailed("no audit row of the agent's confirm")
    if session.scalar(_events_of(call.call_id, booked.id)):
        raise CallFailed("an outbox event for the test card: it would reach the team")
    shown = list_today(session, tenant.id, tenant.timezone, now=slot)
    if booked.id not in {row.reservation_id for row in shown if row.is_test}:
        raise CallFailed("the tablet's day list does not show the TEST card")
    if today_change_token(session, tenant.id, tenant.timezone, now=slot) == before:
        raise CallFailed("the tablet's event stream would not move")
    return booked.id, tools


def _call(engine: Engine, tenant_id: uuid.UUID, now: datetime) -> CallReport:
    """The call and its checks in one transaction: committed when every check
    passed, rolled back otherwise."""
    with engine.connect() as connection:
        outer = connection.begin()
        session = Session(
            bind=connection,
            join_transaction_mode="create_savepoint",
            autoflush=False,
            expire_on_commit=False,
        )
        try:
            tenant = session.get(Tenant, tenant_id)
            if tenant is None:
                raise CallFailed("tenant gone")
            slot = first_free_slot(session, tenant, now)
            before = today_change_token(session, tenant.id, tenant.timezone, now=slot)
            call = _converse(session, tenant, now, slot)
            reservation_id, tools = _check_booking(session, tenant, call, slot, before)
            with Session(engine) as other:
                if seen := rows_of(other, call.call_id):
                    raise CallFailed(f"{seen} row(s) visible outside the test")
            call.finish()
            today = list_today(session, tenant.id, tenant.timezone, now=now)
            shown_today = reservation_id in {row.reservation_id for row in today}
        except BaseException:
            session.close()
            outer.rollback()
            raise
        session.close()
        outer.commit()
    return CallReport(call.call_id, reservation_id, slot, tools, shown_today)


def reservation_call(
    engine: Engine,
    tenant_id: uuid.UUID,
    now: datetime,
    *,
    show: float = SHOW_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> CallReport:
    """Run the call, check it, show its TEST card, remove it, check that
    nothing is left."""
    with Session(engine) as session:
        remove_test_rows(session, tenant_id)  # a card a killed run left
    report = _call(engine, tenant_id, now)
    try:
        with Session(engine) as fresh:
            tenant = fresh.get(Tenant, tenant_id)
            zone = tenant.timezone if tenant else "Europe/Berlin"
            shown = list_today(fresh, tenant_id, zone, now=report.reserved_for)
            if report.reservation_id not in {r.reservation_id for r in shown}:
                raise CallFailed("the committed TEST card is not on the tablet")
        if report.shown_today and show > 0:
            sleep(show)
    finally:
        with Session(engine) as session:
            remove_test_rows(session, tenant_id)
    with Session(engine) as fresh:
        if left := rows_of(fresh, report.call_id):
            raise CallFailed(f"{left} row(s) left after the test card was removed")
    return report
