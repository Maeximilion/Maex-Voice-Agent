"""Which key a draft request of a call is stored under (#862).

Same key, same answer (docs/04 §1) holds for a retry: the same request again
while its draft is still the call's latest. A draft that a later one of its
call replaced is a change of mind the guest went back on: replayed, it skipped
every check (the notice, the hours, a dish sold out since) and kept its old
pickup time. Such a request gets a successor key and is drafted anew, checked
like any correction. A decided draft (confirmed, cancelled) is replayed as it
stands: its yes was given, the same request after it books nothing again.
"""

import uuid
from collections.abc import Callable
from datetime import datetime

from sqlalchemy import exists, or_, select
from sqlalchemy.orm import Session

from api.core.ids import idempotency_key
from api.models import Order, Reservation

type Draft = Order | Reservation


def key_for_request(
    session: Session,
    key: str,
    tenant_id: uuid.UUID,
    call_id: uuid.UUID,
    tool: str,
    by_key: Callable[[Session, str], Draft | None],
) -> str:
    """`key` while its row is missing, decided, another call's (the caller
    refuses that one) or the call's latest draft; else the successor key of
    that row, followed the same way. Every successor is made from the row it
    follows, so a retry finds the same chain and ends on the same draft."""
    while (row := by_key(session, key)) is not None and (
        row.tenant_id == tenant_id
        and row.call_id == call_id
        and row.status == "draft"
        and _replaced(session, tenant_id, call_id, row.created_at)
    ):
        key = idempotency_key(call_id, tool, key, row.id)
    return key


def _replaced(
    session: Session, tenant_id: uuid.UUID, call_id: uuid.UUID, at: datetime
) -> bool:
    """Did the call draft anything after `at`, a table or an order? Equal
    times count as no: a clock that stands still (simulator, tests) replays."""
    newer = (
        exists().where(
            model.tenant_id == tenant_id,
            model.call_id == call_id,
            model.created_at > at,
        )
        for model in (Reservation, Order)
    )
    return bool(session.scalar(select(or_(*newer))))
