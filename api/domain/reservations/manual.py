"""A reservation the team enters on the tablet (T-3.12, #497, D30).

A friend asks an employee on WhatsApp for a table, and the team enters it by
hand. There is no call: `source` is `tablet`, `call_id` stays empty, and
`audit_log` names the tablet as the actor. As with the agent, the entry is a
draft first, and only `confirm_tablet` (`domain/confirm.py`) makes it a
booking: the team's second tap, on the summary of what it entered, stands for
the guest's yes (CLAUDE.md §2 rule 3, Maxi 05.11.2026).

It counts against the same seats the agent books from (D28). A window that is
full, or a time outside every window, is booked only on the team's word
(`override`); a table that another reservation holds in the same window is
refused, whatever the team says. A tablet draft holds no seats and no table:
`confirm_tablet` checks both again under the day lock.

The agent's limits are the agent's: the party sizes, how far ahead, the notice,
the opening hours and the reservation switch (T-3.10) do not stop the team,
which decides by hand what the agent may not (assumption, docs/01).
"""

import uuid
from datetime import datetime, timedelta
from typing import cast
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from api.core.errors import Conflict, InvalidInput, NotFound
from api.core.time import utcnow
from api.domain.customers.phone import normalize_phone
from api.domain.reservations.capacity import CapacityWindow, window_load
from api.domain.status.config import ACTOR_TABLET
from api.models import (
    AuditLog,
    DiningTable,
    Reservation,
    ReservationTable,
    ServiceConfig,
    TableCombination,
    Tenant,
)
from api.schemas.reservations import (
    ReservationStatus,
    TabletDraft,
    TabletReservationRequest,
)

SOURCE_TABLET = "tablet"
ACTION_DRAFT_CREATED = "reservation.draft_created"
SAY_PAST = "Dieser Zeitpunkt ist schon vorbei."
SAY_NO_NAME = "Bitte einen Namen eintragen."
SAY_TABLE_GONE = "Diesen Tisch gibt es im Raumplan nicht mehr."
SAY_NOT_COMBINED = "Diese beiden Tische sind im Raumplan nicht kombiniert."
SAY_SAME_TABLE = "Ein Tisch kann nur einmal gewählt werden."


class WindowFull(Conflict):
    """The window holds no room for the party, or the time lies outside every
    window: booked only when the team says so (`override`)."""


def create_tablet_reservation(
    session: Session,
    req: TabletReservationRequest,
    now: datetime | None = None,
    actor: str = ACTOR_TABLET,
) -> TabletDraft:
    now = now or utcnow()
    tenant = session.get(Tenant, req.tenant_id)
    if tenant is None:
        raise NotFound("Mandant unbekannt")
    zone = ZoneInfo(tenant.timezone)

    # Same key, same answer: a second tap on "Weiter" makes no second draft.
    existing = _by_key(session, req.idempotency_key)
    if existing is not None:
        return _replay(session, existing, req.tenant_id, zone, now)

    guest_name = req.guest_name.strip()
    if not guest_name:
        raise InvalidInput("guest_name: darf nicht leer sein", say=SAY_NO_NAME)
    raw_phone = (req.phone or "").strip()
    # No number is an empty text, as after the deletion period (jobs/retention.py).
    phone = normalize_phone(raw_phone) if raw_phone else ""
    note = (req.note or "").strip() or None
    if req.reserved_for <= now:
        raise InvalidInput("Zeitpunkt liegt in der Vergangenheit", say=SAY_PAST)
    tables = _seating(session, req.tenant_id, req.table_ids)
    # Told now, so the team picks another table before it reads the summary;
    # `confirm_tablet` asks again under the lock.
    window, _ = window_load(
        session,
        req.tenant_id,
        req.reserved_for,
        zone,
        now=now,
        draft_lifetime=draft_lifetime(session, req.tenant_id),
    )
    taken = taken_tables(
        session, req.tenant_id, [t.id for t in tables], req.reserved_for, window
    )
    if taken:
        raise Conflict("table taken", say=say_taken(taken, req.reserved_for, zone))

    reservation = Reservation(
        tenant_id=req.tenant_id,
        call_id=None,
        source=SOURCE_TABLET,
        status="draft",
        guest_name=guest_name,
        phone=phone,
        party_size=req.party_size,
        reserved_for=req.reserved_for,
        note=note,
        idempotency_key=req.idempotency_key,
        created_at=now,
    )
    session.add(reservation)
    try:
        session.flush()
    except IntegrityError:
        # Two taps at once with the same key: the second reads the first.
        session.rollback()
        existing = _by_key(session, req.idempotency_key)
        if existing is None:
            raise
        return _replay(session, existing, req.tenant_id, zone, now)

    session.add_all(
        ReservationTable(
            tenant_id=req.tenant_id, reservation_id=reservation.id, table_id=table.id
        )
        for table in tables
    )
    session.add(
        AuditLog(
            tenant_id=req.tenant_id,
            actor=actor,
            action=ACTION_DRAFT_CREATED,
            entity="reservation",
            entity_id=reservation.id,
            payload={
                "source": SOURCE_TABLET,
                "party_size": req.party_size,
                "reserved_for": req.reserved_for.isoformat(),
                "tables": sorted(t.name for t in tables),
            },
        )
    )
    session.flush()
    draft = describe(session, reservation, zone, now)
    session.commit()
    return draft


def describe(
    session: Session, reservation: Reservation, zone: ZoneInfo, now: datetime
) -> TabletDraft:
    """The draft with its window, the guests the window holds and its tables."""
    window, booked = window_load(
        session,
        reservation.tenant_id,
        reservation.reserved_for,
        zone,
        now=now,
        draft_lifetime=draft_lifetime(session, reservation.tenant_id),
    )
    return TabletDraft(
        reservation_id=reservation.id,
        # ck_reservations_status keeps the text to these values, TabletDraft again.
        status=cast(ReservationStatus, reservation.status),
        reserved_for=reservation.reserved_for,
        party_size=reservation.party_size,
        guest_name=reservation.guest_name,
        phone=reservation.phone,
        note=reservation.note,
        tables=[t.name for t in seated_on(session, reservation.id)],
        window_start=window.start if window else None,
        window_end=window.end if window else None,
        booked_guests=booked,
        max_guests=window.max_guests if window else None,
        needs_override=not fits(window, booked, reservation.party_size),
    )


def fits(window: CapacityWindow | None, booked: int, party_size: int) -> bool:
    return window is not None and booked + party_size <= window.max_guests


def say_full(window: CapacityWindow | None, booked: int, zone: ZoneInfo) -> str:
    if window is None:
        return "Zu dieser Zeit gibt es kein Reservierungsfenster."
    start, end = window.start.astimezone(zone), window.end.astimezone(zone)
    return (
        f"Das Fenster {start:%H:%M} bis {end:%H:%M} Uhr hat {booked} von "
        f"{window.max_guests} Plätzen belegt."
    )


def seated_on(session: Session, reservation_id: uuid.UUID) -> list[DiningTable]:
    """The tables of a reservation, removed ones included, by name."""
    return list(
        session.scalars(
            select(DiningTable)
            .join(ReservationTable, ReservationTable.table_id == DiningTable.id)
            .where(ReservationTable.reservation_id == reservation_id)
            .order_by(DiningTable.name)
        )
    )


def taken_tables(
    session: Session,
    tenant_id: uuid.UUID,
    table_ids: list[uuid.UUID],
    at: datetime,
    window: CapacityWindow | None,
    exclude_id: uuid.UUID | None = None,
) -> list[str]:
    """Names of the tables another confirmed reservation holds in the same
    capacity window, the turn of a table (docs/01). Outside every window only
    the same start time collides."""
    if not table_ids:
        return []
    when = (
        (Reservation.reserved_for >= window.start)
        & (Reservation.reserved_for < window.end)
        if window is not None
        else Reservation.reserved_for == at
    )
    return list(
        session.scalars(
            select(DiningTable.name)
            .join(ReservationTable, ReservationTable.table_id == DiningTable.id)
            .join(Reservation, Reservation.id == ReservationTable.reservation_id)
            .where(
                ReservationTable.table_id.in_(table_ids),
                Reservation.tenant_id == tenant_id,
                Reservation.id.is_distinct_from(exclude_id),
                Reservation.status == "confirmed",
                Reservation.deleted_at.is_(None),
                when,
            )
            .distinct()
            .order_by(DiningTable.name)
        )
    )


def say_taken(names: list[str], at: datetime, zone: ZoneInfo) -> str:
    word, verb = ("Tisch", "ist") if len(names) == 1 else ("Die Tische", "sind")
    return (
        f"{word} {' und '.join(names)} {verb} um {at.astimezone(zone):%H:%M} Uhr "
        "schon vergeben."
    )


def draft_lifetime(session: Session, tenant_id: uuid.UUID) -> timedelta:
    """How long an agent's draft holds its seats (`capacity.booked_guests`)."""
    seconds = session.scalar(
        select(ServiceConfig.max_call_seconds).where(
            ServiceConfig.tenant_id == tenant_id
        )
    )
    if seconds is None:
        raise NotFound("Mandant ohne service_config")
    return timedelta(seconds=seconds)


def _seating(
    session: Session, tenant_id: uuid.UUID, table_ids: list[uuid.UUID]
) -> list[DiningTable]:
    """One live table of the plan, or two that the plan combines."""
    if len(set(table_ids)) != len(table_ids):
        raise InvalidInput("same table twice", say=SAY_SAME_TABLE)
    if not table_ids:
        return []
    tables = list(
        session.scalars(
            select(DiningTable).where(
                DiningTable.id.in_(table_ids),
                DiningTable.tenant_id == tenant_id,
                DiningTable.deleted_at.is_(None),
            )
        )
    )
    if len(tables) != len(table_ids):
        raise NotFound("table unknown or removed", say=SAY_TABLE_GONE)
    if len(table_ids) == 2:
        # The pair is stored with the smaller id first (ck_table_combinations_order).
        low, high = sorted(table_ids)
        pair = session.scalar(
            select(TableCombination.id).where(
                TableCombination.tenant_id == tenant_id,
                TableCombination.table_a_id == low,
                TableCombination.table_b_id == high,
            )
        )
        if pair is None:
            raise InvalidInput("tables not combined", say=SAY_NOT_COMBINED)
    return tables


def _by_key(session: Session, key: str) -> Reservation | None:
    return session.scalar(select(Reservation).where(Reservation.idempotency_key == key))


def _replay(
    session: Session,
    existing: Reservation,
    tenant_id: uuid.UUID,
    zone: ZoneInfo,
    now: datetime,
) -> TabletDraft:
    if existing.tenant_id != tenant_id or existing.source != SOURCE_TABLET:
        raise Conflict("Idempotenz-Schlüssel gehört zu einem anderen Vorgang")
    return describe(session, existing, zone, now)
