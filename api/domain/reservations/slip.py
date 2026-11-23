"""A short slip on the print bridge for every table the agent books (#863 C).

Maxi, 23.11.2026: the team works with the tablet software in the background, so a
reservation the agent confirms also comes out of the printer: day, time, party
size, name and the guest's note. A table the team enters on the tablet prints
nothing: they typed it themselves.

The slip rides the kitchen queue like the self-test's (`selftest_slip.py`): an
`order.confirmed` event the bridge claims, prints and acknowledges on the path
of every slip (`handover.claim_tickets`, `ack_ticket`). Its `order_id` is the
reservation's id, which names no order: neither the bridge's answer nor the
watchdog (`handover.sweep`) turns a card red for it, the board counts no ticket
for it, and kitchen events never reach n8n or the operator's alarm on failed
events (`events/dispatcher.py`, `jobs/monitor.py`). `reservation_id` carries
the same id, so a data request and the deletion job find the row as they find
the reservation's own event (`customers/requests.py`, `jobs/retention.py`).

A slip is only due while its table stands: `still_booked` is read at every
pickup and by the watchdog, which takes back a slip whose reservation was
cancelled or deleted, or whose time has passed, so none stays queued for it.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import ColumnElement, cast, exists
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Session

from api.config import settings
from api.domain.reservations.spoken import WEEKDAYS
from api.events import enqueue
from api.events.types import ORDER_CONFIRMED
from api.models import OutboxEvent, Reservation

# The slip's `type`: the bridge prints the reservation layout for it
# (`printbridge/escpos.py`), and the handover tells the slip apart by it.
RESERVATION = "reservation"


# Never NULL, so its negation is safe on a payload without a `type`.
IS_RESERVATION_SLIP: ColumnElement[bool] = OutboxEvent.payload[
    "type"
].astext.is_not_distinct_from(RESERVATION)


def still_booked(now: datetime) -> ColumnElement[bool]:
    """True for a slip whose reservation is confirmed, not deleted and still
    ahead. A row the erasure removed counts as gone."""
    return exists().where(
        Reservation.id == cast(OutboxEvent.payload["reservation_id"].astext, PG_UUID),
        Reservation.tenant_id == OutboxEvent.tenant_id,
        Reservation.status == "confirmed",
        Reservation.deleted_at.is_(None),
        Reservation.reserved_for > now,
    )


def queue_reservation_slip(
    session: Session, reservation: Reservation, zone: ZoneInfo
) -> None:
    """Queue the slip of a table the agent just booked. Does not commit; the
    caller holds the reservation and commits with the booking. Nothing for a
    tenant without a print bridge: its slip would wait forever and hold the
    guest's name in the queue."""
    if str(reservation.tenant_id) != settings.kitchen_bridge_tenant_id:
        return
    local = reservation.reserved_for.astimezone(zone)
    enqueue(
        session,
        tenant_id=reservation.tenant_id,
        event_type=ORDER_CONFIRMED,
        payload={
            # The bridge keeps a printed id and revision and never prints it
            # twice (`printbridge/state.py`).
            "order_id": str(reservation.id),
            "reservation_id": str(reservation.id),
            "call_id": str(reservation.call_id) if reservation.call_id else None,
            "type": RESERVATION,
            "customer_name": reservation.guest_name,
            "party_size": reservation.party_size,
            # Local day and time: the bridge knows no time zone.
            "reserved_day": f"{WEEKDAYS[local.weekday()]} {local:%d.%m.}",
            "reserved_time": f"{local:%H:%M}",
            "note": reservation.note,
            "revision": 0,
        },
    )


def is_slip_of_a_reservation(event: OutboxEvent) -> bool:
    return event.payload.get("type") == RESERVATION
