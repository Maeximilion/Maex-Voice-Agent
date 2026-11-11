"""confirm: der einzige Übergang von draft nach confirmed (docs/04 §confirm).

Generisch über beide Vorgangsarten; die Bestellung liegt in
`domain/ordering/confirm.py` (Abholcode, Übergabe je Modus).
"""

import uuid
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.core.errors import SAY_ON_FAILURE, SAY_TO_THE_TEAM, Conflict, NotFound
from api.core.time import utcnow
from api.domain.customers.contact import note_contact
from api.domain.ordering.confirm import confirm_order
from api.domain.reservations.capacity import (
    has_room,
    lock_capacity_days,
    window_load,
)
from api.domain.reservations.manual import (
    SAY_PAST,
    SOURCE_TABLET,
    WindowFull,
    draft_lifetime,
    fits,
    say_full,
    say_taken,
    seated_on,
    seating,
    taken_tables,
)
from api.domain.reservations.slots import ASK_OTHER_TIME, SAY_RESERVATIONS_OFF
from api.domain.status.config import ACTOR_TABLET
from api.events import enqueue
from api.events.types import RESERVATION_CONFIRMED
from api.models import AuditLog, Call, Reservation, ServiceConfig, Tenant
from api.schemas.confirm import Confirmation, ConfirmRequest

ACTOR_AGENT = "agent"
SAY_TABLET_CANCELLED = "Diese Reservierung wurde bereits storniert."
SAY_CANCELLED = f"{SAY_TABLET_CANCELLED} {SAY_TO_THE_TEAM}"
SAY_SEATS_GONE = (
    f"Dieser Tisch ist in der Zwischenzeit leider vergeben worden. {ASK_OTHER_TIME}"
)

# Der Vorgang ist nach dem ersten confirm bestätigt; jeder weitere Aufruf liest
# denselben Zustand. Deshalb trägt die Zustandsmaschine die Idempotenz, nicht der
# idempotency_key: er wandert nur ins Protokoll (docs/04 §Gemeinsame Regeln).
CONFIRMED = Confirmation(status="confirmed", handover="queued", pickup_code=None)


def confirm(
    session: Session, req: ConfirmRequest, now: datetime | None = None
) -> Confirmation:
    tenant = session.get(Tenant, req.tenant_id)
    if tenant is None:
        raise NotFound("Mandant unbekannt")

    call = session.get(Call, req.call_id)
    if call is None or call.tenant_id != req.tenant_id:
        raise NotFound("Anruf unbekannt", say=SAY_ON_FAILURE)

    if req.entity == "reservation":
        return _confirm_reservation(session, req, tenant, now or utcnow())
    return confirm_order(session, req, now)


def _confirm_reservation(
    session: Session, req: ConfirmRequest, tenant: Tenant, now: datetime
) -> Confirmation:
    # Zeilensperre bis zum Ende der Transaktion: zwei gleichzeitige Aufrufe auf
    # denselben Entwurf dürfen nicht zweimal in die Outbox schreiben.
    reservation = session.execute(
        select(Reservation).where(Reservation.id == req.entity_id).with_for_update(),
        execution_options={"populate_existing": True},
    ).scalar_one_or_none()

    # Der Anruf muss derselbe sein wie beim Entwurf: das "Ja" gehört dem Gast, der
    # gerade in der Leitung ist (CLAUDE.md §2 Regel 3). Sonst bestätigt ein Anruf den
    # Tisch eines anderen Gastes, und Audit-Zeile und Outbox-Payload nennen zwei
    # verschiedene Anrufe.
    if (
        reservation is None
        or reservation.tenant_id != req.tenant_id
        or reservation.call_id != req.call_id
        or reservation.deleted_at is not None
    ):
        raise NotFound("Reservierung unbekannt", say=SAY_ON_FAILURE)

    if reservation.status == "cancelled":
        raise Conflict("Reservierung ist storniert", say=SAY_CANCELLED)

    if reservation.status == "confirmed":
        # Nichts zu schreiben; commit gibt nur die Sperre frei.
        session.commit()
        return CONFIRMED

    # A draft holds its seats only while its call can confirm it (capacity.py).
    # One that is confirmed after that, or after the capacity was lowered, may
    # find them given away: check again, under the same lock as the making of
    # a draft, so that two late confirmations cannot both see the window empty.
    # A shared lock to the commit, as `confirm_order`: a plain read does not wait
    # for a tablet that is switching reservations off this moment.
    config = session.execute(
        select(ServiceConfig)
        .where(ServiceConfig.tenant_id == req.tenant_id)
        .with_for_update(read=True),
        execution_options={"populate_existing": True},
    ).scalar_one_or_none()
    if config is None:
        raise NotFound("Mandant ohne service_config", say=SAY_ON_FAILURE)
    if not config.reservations_enabled:
        # The team switched reservations off between the read-back and the yes
        # (T-3.10). A conflict: this draft can never be confirmed.
        raise Conflict("reservations switched off", say=SAY_RESERVATIONS_OFF)
    zone = ZoneInfo(tenant.timezone)
    lock_capacity_days(
        session, req.tenant_id, reservation.reserved_for.astimezone(zone).date()
    )
    if not has_room(
        session,
        reservation,
        zone,
        now=now,
        draft_lifetime=timedelta(seconds=config.max_call_seconds),
    ):
        raise Conflict("Plätze inzwischen vergeben", say=SAY_SEATS_GONE)

    note_contact(
        session,
        req.tenant_id,
        req.call_id,
        reservation.phone,
        now,
        reservation.guest_name,
    )
    _book(
        session,
        reservation,
        ACTOR_AGENT,
        {"call_id": str(req.call_id), "idempotency_key": req.idempotency_key},
    )
    session.commit()
    return CONFIRMED


def confirm_tablet(
    session: Session,
    tenant_id: uuid.UUID,
    reservation_id: uuid.UUID,
    *,
    override: bool = False,
    actor: str = ACTOR_TABLET,
    now: datetime | None = None,
) -> Confirmation:
    """The team's confirming tap on a reservation it entered by hand (T-3.12,
    D30): the same step from draft to confirmed as the agent's, on the team's
    word instead of a call's. The guest said yes to the employee; the tap on
    the summary stands for it (CLAUDE.md §2 rule 3).

    `override` books a window that is full or a time outside every window,
    after the team was asked (`WindowFull`). A table another reservation holds
    in the same window is refused either way, and so are a time that has
    passed and a table or pair the plan no longer has.
    """
    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        raise NotFound("Mandant unbekannt")
    now = now or utcnow()
    reservation = session.execute(
        select(Reservation).where(Reservation.id == reservation_id).with_for_update(),
        execution_options={"populate_existing": True},
    ).scalar_one_or_none()
    # Only the tablet's own drafts: an agent's draft waits for its caller's yes.
    if (
        reservation is None
        or reservation.tenant_id != tenant_id
        or reservation.source != SOURCE_TABLET
        or reservation.deleted_at is not None
    ):
        raise NotFound("reservation unknown")
    if reservation.status == "cancelled":
        raise Conflict("reservation cancelled", say=SAY_TABLET_CANCELLED)
    if reservation.status == "confirmed":
        session.commit()
        return CONFIRMED
    # A draft left open past its time books nothing (Codex #544): the team
    # enters the table anew if the guest still comes.
    if reservation.reserved_for <= now:
        raise Conflict("time passed", say=SAY_PAST)

    zone = ZoneInfo(tenant.timezone)
    lock_capacity_days(
        session, tenant_id, reservation.reserved_for.astimezone(zone).date()
    )
    # The plan may have lost the table or the pair since the draft (Codex #544).
    tables = seating(
        session, tenant_id, [t.id for t in seated_on(session, reservation.id)]
    )
    window, booked = window_load(
        session,
        tenant_id,
        reservation.reserved_for,
        zone,
        now=now,
        draft_lifetime=draft_lifetime(session, tenant_id),
    )
    taken = taken_tables(
        session,
        tenant_id,
        [t.id for t in tables],
        reservation.reserved_for,
        window,
        exclude_id=reservation.id,
    )
    if taken:
        raise Conflict(
            "table taken", say=say_taken(taken, reservation.reserved_for, zone)
        )
    full = not fits(window, booked, reservation.party_size)
    if full and not override:
        raise WindowFull("window full", say=say_full(window, booked, zone))

    _book(session, reservation, actor, {"source": SOURCE_TABLET, "override": full})
    session.commit()
    return CONFIRMED


def _book(
    session: Session, reservation: Reservation, actor: str, audit: dict[str, Any]
) -> None:
    """Draft to confirmed, with its audit row and its outbox event; the caller
    holds the row lock and commits."""
    reservation.status = "confirmed"
    session.add(
        AuditLog(
            tenant_id=reservation.tenant_id,
            actor=actor,
            action="reservation.confirmed",
            entity="reservation",
            entity_id=reservation.id,
            payload=audit,
        )
    )
    enqueue(
        session,
        tenant_id=reservation.tenant_id,
        event_type=RESERVATION_CONFIRMED,
        # Vollständig, damit der Versand im kalten Pfad ohne zweite Abfrage auskommt.
        payload={
            "reservation_id": str(reservation.id),
            "call_id": str(reservation.call_id) if reservation.call_id else None,
            "source": reservation.source,
            "guest_name": reservation.guest_name,
            "phone": reservation.phone,
            "party_size": reservation.party_size,
            "reserved_for": reservation.reserved_for.isoformat(),
            "note": reservation.note,
        },
    )
