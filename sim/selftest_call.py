"""The self-test's reservation call (#574): one call through the agent, never kept.

The scripted stand-in model (`scripted_llm.py`) books a table for two on the
first free time in the coming days, the way an eval does it (`replay.py`): the
draft must be there before the yes, `confirm` alone turns it into a booking,
and the booking must reach what the tablet reads: the day's list of the
operations view (`list_today`), the change token its event stream polls
(`today_change_token`, `gui/sse.py`) and the outbox event for the team
(`reservation.confirmed`).

Everything happens inside one database transaction that is rolled back at the
end. The code commits as on a real call (`start_call`, `confirm`); those commits
end savepoints only (`join_transaction_mode="create_savepoint"`). Nothing is
ever visible to another connection: the dispatcher never sees the event, so no
push and no n8n call reaches the team, no tablet shows a card, and no row is
left behind. The call carries its own `call_id` like every call (CLAUDE.md §8).
A checker on a second connection proves the first, a fresh read after the
rollback the second.

While it runs (about a second), the transaction holds the day lock of the chosen
day and a share lock on `service_config`: a real booking for that day, or a
change on the tablet's settings, waits that long.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from api.domain.reservations import list_today, today_change_token
from api.domain.reservations.capacity import capacity_windows
from api.domain.reservations.slots import check_slot
from api.events.types import RESERVATION_CONFIRMED
from api.models import AuditLog, Call, OutboxEvent, Reservation, ServiceConfig, Tenant
from sim.session import SimCall

PARTY = 2
# The furthest day looked at, whatever `reservation_max_days_ahead` allows.
DAYS_AHEAD = 14
GUEST = "Selbsttest"
# A number of the evals; nobody is called on it, nothing leaves the transaction.
PHONE = "0721 5551234"
CALL_PREFIX = "selftest-"


class CallFailed(Exception):
    """The call did not end as a booking the tablet sees; the message says where."""


@dataclass(frozen=True)
class CallReport:
    call_id: uuid.UUID
    reserved_for: datetime
    tools: list[str]


def first_free_slot(session: Session, tenant: Tenant, now: datetime) -> datetime:
    """The first time from tomorrow on that `check_slot` gives a table for two."""
    config = session.get(ServiceConfig, tenant.id)
    if config is None:
        raise CallFailed("no service_config")
    if not config.reservations_enabled:
        raise CallFailed("reservations are switched off on the tablet")
    zone = ZoneInfo(tenant.timezone)
    today = now.astimezone(zone).date()
    last = min(DAYS_AHEAD, config.reservation_max_days_ahead)
    for ahead in range(1, last + 1):
        day = today + timedelta(days=ahead)
        for window in capacity_windows(session, tenant.id, day, zone):
            for slot in window.grid():
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


def rows_of(session: Session, call_id: uuid.UUID) -> int:
    """Rows of this call any other connection can see: the call, its
    reservations, its outbox events and its audit rows."""
    key = str(call_id)
    counts = (
        select(func.count()).select_from(Call).where(Call.id == call_id),
        select(func.count()).where(Reservation.call_id == call_id),
        select(func.count()).where(OutboxEvent.payload["call_id"].astext == key),
        select(func.count()).where(AuditLog.payload["call_id"].astext == key),
    )
    return sum(session.scalar(query) or 0 for query in counts)


def _converse(
    session: Session, tenant: Tenant, now: datetime, slot: datetime
) -> SimCall:
    call = SimCall(
        session,
        tenant,
        now=now,
        external_session_id=f"{CALL_PREFIX}{uuid.uuid4().hex[:8]}",
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
) -> CallReport:
    booked = _reservation(session, call.call_id)
    if booked is None or booked.status != "confirmed":
        raise CallFailed("the yes did not confirm the draft")
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
    event = session.scalar(
        select(OutboxEvent).where(
            OutboxEvent.event_type == RESERVATION_CONFIRMED,
            OutboxEvent.payload["reservation_id"].astext == str(booked.id),
        )
    )
    if event is None or event.payload.get("call_id") != str(call.call_id):
        raise CallFailed("no outbox event for the team with the call's call_id")
    shown = list_today(session, tenant.id, tenant.timezone, now=slot)
    if booked.id not in {row.reservation_id for row in shown}:
        raise CallFailed("the tablet's day list does not show the booking")
    if today_change_token(session, tenant.id, tenant.timezone, now=slot) == before:
        raise CallFailed("the tablet's event stream would not move")
    return CallReport(call.call_id, booked.reserved_for, tools)


def reservation_call(engine: Engine, tenant_id: uuid.UUID, now: datetime) -> CallReport:
    """Run the call, check it, roll it back, check that nothing is left."""
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
            report = _check_booking(session, tenant, call, slot, before)
            with Session(engine) as other:
                if seen := rows_of(other, call.call_id):
                    raise CallFailed(f"{seen} row(s) visible outside the test")
            call.finish()
        finally:
            session.close()
            outer.rollback()
    with Session(engine) as fresh:
        if left := rows_of(fresh, report.call_id):
            raise CallFailed(f"{left} row(s) left after the rollback")
    return report
