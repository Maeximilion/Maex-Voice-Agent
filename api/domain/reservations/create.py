"""create_reservation: Reservierung als draft mit Readback und Idempotenz."""

import re
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.core.errors import InvalidInput, NotFound
from api.core.time import utcnow
from api.domain.reservations.spoken import spoken_time
from api.models import Call, Reservation, Tenant
from api.schemas.reservations import CreateReservationResponse


def _normalize_phone(phone: str) -> str:
    """Normalisiert Telefonnummern zu E.164-Format: +49...

    Erlaubt: +49..., 0049..., +49.... Falls nicht normalisierbar: unverändert zurück.
    """
    if not phone:
        return phone
    clean = re.sub(r"\D", "", phone)
    if clean.startswith("49"):
        return f"+{clean}"
    if clean.startswith("0"):
        return f"+49{clean[1:]}"
    if not clean.startswith("1"):
        return f"+{clean}"
    return phone if phone.startswith("+") else f"+{clean}"


def _weekday_word(dt: datetime) -> str:
    """Wochentag im Nominativ für Lesbarkeit."""
    days = (
        "Montag",
        "Dienstag",
        "Mittwoch",
        "Donnerstag",
        "Freitag",
        "Samstag",
        "Sonntag",
    )
    return days[dt.weekday()]


def _format_date(dt: datetime, now: datetime) -> str:
    """Datumsbeschreibung: „heute", „morgen" oder „am Freitag"."""
    local_dt = dt.astimezone(dt.tzinfo)
    local_now = now.astimezone(dt.tzinfo)
    diff = (local_dt.date() - local_now.date()).days
    if diff == 0:
        return "heute"
    if diff == 1:
        return "morgen"
    return f"am {_weekday_word(local_dt)}"


def create_reservation(
    session: Session,
    tenant_id: uuid.UUID,
    call_id: uuid.UUID,
    idempotency_key: str,
    guest_name: str,
    phone: str,
    party_size: int,
    reserved_for: datetime,
    note: str | None = None,
) -> CreateReservationResponse:
    """Reservierung als draft. Gleicher Schlüssel → gleiche Antwort (idempotent)."""
    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        raise NotFound("Mandant unbekannt")

    call = session.get(Call, call_id)
    if call is None:
        raise NotFound("Anruf nicht gefunden")

    zone = ZoneInfo(tenant.timezone)
    now = utcnow()

    normalized_phone = _normalize_phone(phone)
    if not normalized_phone or not normalized_phone.startswith("+"):
        raise InvalidInput(
            "Ungültige Telefonnummer",
            say="Die Telefonnummer konnte nicht verarbeitet werden.",
        )

    stmt = select(Reservation).where(
        Reservation.idempotency_key == idempotency_key
    )
    existing = session.execute(stmt).scalar_one_or_none()
    if existing:
        return CreateReservationResponse(
            reservation_id=existing.id,
            status=existing.status,
            readback=_make_readback(
                existing.guest_name,
                existing.party_size,
                existing.reserved_for,
                existing.note,
                now,
                zone,
            ),
        )

    reservation = Reservation(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        call_id=call_id,
        status="draft",
        guest_name=guest_name,
        phone=normalized_phone,
        party_size=party_size,
        reserved_for=reserved_for,
        note=note,
        idempotency_key=idempotency_key,
    )
    session.add(reservation)
    session.flush()

    return CreateReservationResponse(
        reservation_id=reservation.id,
        status="draft",
        readback=_make_readback(
            guest_name, party_size, reserved_for, note, now, zone
        ),
    )


def _make_readback(
    guest_name: str,
    party_size: int,
    reserved_for: datetime,
    note: str | None,
    now: datetime,
    zone: ZoneInfo,
) -> str:
    """Satz zum Vorlesen für den Kunden."""
    local_time = reserved_for.astimezone(zone)
    date_str = _format_date(local_time, now.astimezone(zone))
    time_str = spoken_time(local_time)

    parts = [f"Reservierung für {guest_name}"]

    if party_size == 1:
        parts.append("eine Person")
    else:
        parts.append(f"{party_size} Personen")

    parts.append(f"{date_str} um {time_str}")

    if note:
        parts.append(f"{note} vorhanden")

    result = ", ".join(parts) + ". Passt das so?"
    return result


__all__ = ["create_reservation"]
