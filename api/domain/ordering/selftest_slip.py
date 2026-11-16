"""A test slip for the print bridge, asked for by the self-test (#574).

The slip is a kitchen event like a confirmed order's (`order.confirmed`), so the
bridge claims, prints and acknowledges it on the path of every real slip
(`handover.claim_tickets`, `ack_ticket`). It belongs to no order and no call:
it books nothing, so the rule "every transaction carries a call_id" does not
reach it, and its order id names no row, so neither the bridge's answer nor
the guard of the dispatcher (`handover.sweep`) turns a card red for it. Kitchen
events never go to n8n (`events/dispatcher.py` leaves them out): the team gets
no push for it.

The slip says on its face that it is a test, and the self-test removes its row
once the bridge has answered, or when it gave up waiting and the bridge has not
claimed it yet, so it does not come out in the middle of a later service.
"""

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.events import enqueue
from api.events.types import ORDER_CONFIRMED
from api.models import OutboxEvent

SELF_TEST = "self_test"
TEST_SLIP_NAME = "SELBSTTEST"
TEST_SLIP_ITEM = "Testbon - nicht kochen"
TEST_SLIP_NOTE = "Kein echter Auftrag"


def queue_selftest_slip(
    session: Session, tenant_id: uuid.UUID, now: datetime
) -> uuid.UUID:
    """Queue one test slip for the stack's bridge and commit; its event id."""
    event = enqueue(
        session,
        tenant_id=tenant_id,
        event_type=ORDER_CONFIRMED,
        payload={
            # A fresh id each time: the bridge skips an order id and revision
            # it printed before (`printbridge/state.py`).
            "order_id": str(uuid.uuid4()),
            "call_id": None,
            SELF_TEST: True,
            "type": "pickup",
            "pickup_code": "T0",
            "customer_name": TEST_SLIP_NAME,
            "revision": 0,
            "correction_reason": None,
            "note": TEST_SLIP_NOTE,
            "items_total_cents": 0,
            "delivery_fee_cents": 0,
            "total_cents": 0,
            "items": [
                {
                    "number": "0",
                    "name": TEST_SLIP_ITEM,
                    "quantity": 1,
                    "options": [],
                    "note": TEST_SLIP_NOTE,
                }
            ],
        },
    )
    event.next_attempt_at = now
    session.commit()
    return event.id


def selftest_slip_status(session: Session, event_id: uuid.UUID) -> str | None:
    """`pending`, `sent` (printed), `failed`, or None when the row is gone."""
    return session.scalar(
        select(OutboxEvent.status)
        .where(OutboxEvent.id == event_id)
        .execution_options(populate_existing=True)
    )


def remove_selftest_slip(session: Session, event_id: uuid.UUID) -> bool:
    """Delete the slip's row and commit. Refused (False) while the bridge holds
    it: claimed and not yet answered, it may be on its way to the printer, and
    its answer would find no row. Printed, failed or never claimed, it goes.
    Never touches any other event."""
    event = session.scalar(
        select(OutboxEvent)
        .where(OutboxEvent.id == event_id)
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    )
    if event is None:
        session.commit()
        return not session.scalar(
            select(OutboxEvent.id).where(OutboxEvent.id == event_id)
        )
    if event.event_type != ORDER_CONFIRMED or not event.payload.get(SELF_TEST):
        session.rollback()
        raise ValueError(f"event {event_id} is no test slip")
    # A claim clears `last_error`; a failed print writes one (`handover.py`).
    claimed = (
        event.status == "pending" and event.attempts > 0 and event.last_error is None
    )
    if claimed:
        session.rollback()
        return False
    session.delete(event)
    session.commit()
    return True
