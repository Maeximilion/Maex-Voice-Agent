"""create_reservation: Entwurf anlegen, Satz zum Vorlesen zurückgeben. Erst confirm macht ihn gültig."""

import uuid
from datetime import date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from api.core.errors import Conflict, InvalidInput, NotFound
from api.core.time import utcnow
from api.domain.customers.phone import normalize_phone
from api.domain.reservations.slots import check_slot
from api.domain.reservations.spoken import spoken_date, spoken_party_size, spoken_time
from api.models import AuditLog, Call, Reservation, Tenant
from api.schemas.reservations import CreateReservationRequest, ReservationDraft

ACTOR_AGENT = "agent"
ACTION_DRAFT_CREATED = "reservation.draft_created"
SAY_CALL_UNKNOWN = "Bei mir gibt es gerade eine technische Störung. Ich verbinde Sie mit dem Restaurant."


def create_reservation(
    session: Session, req: CreateReservationRequest, now: datetime | None = None
) -> ReservationDraft:
    now = now or utcnow()
    tenant = session.get(Tenant, req.tenant_id)
    if tenant is None:
        raise NotFound("Mandant unbekannt")
    zone = ZoneInfo(tenant.timezone)

    # Gleicher Schlüssel → gleiche Antwort, kein zweiter Vorgang (docs/04 §1).
    existing = _by_key(session, req.idempotency_key)
    if existing is not None:
        return _replay(existing, req.tenant_id, zone)

    call = session.get(Call, req.call_id)
    if call is None or call.tenant_id != req.tenant_id:
        raise NotFound("Anruf unbekannt", say=SAY_CALL_UNKNOWN)

    guest_name = req.guest_name.strip()
    if not guest_name:
        raise InvalidInput("guest_name: darf nicht leer sein")
    phone = normalize_phone(req.phone)
    note = (req.note or "").strip() or None

    _lock_business_day(session, req.tenant_id, req.reserved_for.astimezone(zone).date())
    slot = check_slot(session, req.tenant_id, req.reserved_for, req.party_size, now=now)
    if not slot.available:
        raise Conflict("Zeitpunkt nicht mehr verfügbar", say=slot.say)

    reservation = Reservation(
        tenant_id=req.tenant_id,
        call_id=req.call_id,
        status="draft",
        guest_name=guest_name,
        phone=phone,
        party_size=req.party_size,
        reserved_for=req.reserved_for,
        note=note,
        idempotency_key=req.idempotency_key,
        # Fester Bezugspunkt für den readback, damit ein Replay über Mitternacht
        # nicht plötzlich "heute" statt "morgen" sagt.
        created_at=now,
    )
    session.add(reservation)
    try:
        session.flush()
    except IntegrityError:
        # Zwei gleichzeitige Aufrufe mit demselben Schlüssel: der zweite liest den ersten.
        session.rollback()
        existing = _by_key(session, req.idempotency_key)
        if existing is None:
            raise
        return _replay(existing, req.tenant_id, zone)

    session.add(
        AuditLog(
            tenant_id=req.tenant_id,
            actor=ACTOR_AGENT,
            action=ACTION_DRAFT_CREATED,
            entity="reservation",
            entity_id=reservation.id,
            payload={
                "call_id": str(req.call_id),
                "party_size": req.party_size,
                "reserved_for": req.reserved_for.isoformat(),
            },
        )
    )
    session.commit()
    return _draft(reservation, zone)


def _lock_business_day(session: Session, tenant_id: uuid.UUID, day: date) -> None:
    """Prüfen und Anlegen laufen je Betrieb und Tag nacheinander, sonst überbuchen
    zwei gleichzeitige Anrufe dasselbe Fenster: beide lesen die Kapazität, bevor
    einer schreibt. Die Sperre hält bis zum Ende der Transaktion."""
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:key)::bigint)"),
        {"key": f"reservations:{tenant_id}:{day.isoformat()}"},
    )


def _by_key(session: Session, key: str) -> Reservation | None:
    return session.scalar(select(Reservation).where(Reservation.idempotency_key == key))


def _replay(
    existing: Reservation, tenant_id: uuid.UUID, zone: ZoneInfo
) -> ReservationDraft:
    if existing.tenant_id != tenant_id:
        raise Conflict("Idempotenz-Schlüssel gehört zu einem anderen Vorgang")
    return _draft(existing, zone)


def _draft(r: Reservation, zone: ZoneInfo) -> ReservationDraft:
    return ReservationDraft(
        reservation_id=r.id,
        status=r.status,
        reserved_for=r.reserved_for,
        party_size=r.party_size,
        guest_name=r.guest_name,
        phone=r.phone,
        note=r.note,
        readback=readback(r, zone),
    )


def readback(r: Reservation, zone: ZoneInfo) -> str:
    """Deterministisch aus dem Entwurf, der Agent liest ihn wörtlich vor.

    Bezug ist der Anlagezeitpunkt, nicht die aktuelle Uhrzeit: derselbe Schlüssel
    liefert dieselbe Antwort, auch wenn der Tag inzwischen gewechselt hat.
    """
    local = r.reserved_for.astimezone(zone)
    when = spoken_date(local, today=r.created_at.astimezone(zone).date())
    satz = (
        f"Ein Tisch für {spoken_party_size(r.party_size)} {when} um {spoken_time(local)}"
        f", auf den Namen {r.guest_name}"
    )
    if r.note:
        satz += f", mit dem Hinweis: {r.note}"
    return satz + ". Passt das so?"
