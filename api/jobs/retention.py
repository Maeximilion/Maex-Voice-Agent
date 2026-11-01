"""Deletion job (docs/03 "Loeschkonzept", T-11.2): personal data goes when its period ends.

`python -m api.jobs.retention` runs one pass; the dispatcher process
(`api/jobs/cold_path.py`) runs one every RETENTION_INTERVAL_SECONDS. A pass
only does what is due, so passes can repeat and a missed one is caught up.

Rows stay and lose their personal fields: the statistics of calls, the items,
prices and times of orders (L18), the reason of a callback. Required text
fields become empty strings, so no column changes and no migration is needed.
Periods are settings (api/config.py), decided by Maxi with the legal check on
20.10.2026 (docs/09 §1 L8, L13). One log line per pass names what it changed.
"""

import logging
import re
from collections.abc import Callable
from datetime import datetime, timedelta

from sqlalchemy import (
    Text,
    case,
    cast,
    delete,
    func,
    or_,
    select,
    update,
)
from sqlalchemy.orm import Session

from api.config import settings
from api.core.logging import configure_logging, error_text, get_logger, log
from api.core.time import business_day, business_day_bounds_utc, to_local, tz, utcnow
from api.db import SessionLocal
from api.models import (
    AuditLog,
    Call,
    Callback,
    ConsentLog,
    Order,
    OrderItem,
    OutboxEvent,
    Reservation,
    Tenant,
)

logger = get_logger("api.jobs.retention")

# A caller's allergy in a note: the kitchen wording "Grund: Allergie"
# (domain/menu/wishes.py ALLERGY_NOTE) and what the team types by hand
# ("Nussallergie", "Laktoseintoleranz"). The whole note goes; "ohne Zwiebeln"
# alone stays until the guest details go. Whether ~* folds an umlaut depends
# on the locale of the database, hence both cases.
ALLERGY = "allergi|unvertr(ä|Ä|ae)glich|intoleran"

# What an outbox payload keeps once its period is over: the ids. The kitchen
# finds every slip of an order by them (domain/ordering/handover.py).
OUTBOX_KEEP = ("order_id", "reservation_id", "callback_id", "call_id", "revision")
UNSENT_EXPIRED = "payload removed after {days} days unsent, never sent"
# The date of an order: the promised time, or its creation for a draft.
ORDER_DAY = func.coalesce(Order.ready_at, Order.created_at)
_ALLERGY_RE = re.compile(ALLERGY, re.IGNORECASE)


def purge(session: Session, now: datetime) -> dict[str, int]:
    """One pass over every period. Commits; returns what changed per period.

    Each period runs in a savepoint of its own: one that fails (a broken time
    zone of a tenant, say) is logged by name and rolled back, and the others
    still delete (ultrareview PR #371)."""
    steps: dict[str, Callable[[], int | dict[str, int]]] = {
        "allergy_notes": lambda: _allergy_notes(
            session, now, settings.retention_allergy_note_days
        ),
        "guest_details": lambda: _guest_details(
            session, now - timedelta(days=settings.retention_guest_details_days)
        ),
        "caller_ids": lambda: _run(
            session,
            update(Call)
            .where(
                Call.started_at
                < now - timedelta(days=settings.retention_caller_id_days),
                Call.caller_id.is_not(None),
            )
            .values(caller_id=None),
        ),
        "callbacks": lambda: _run(
            session,
            update(Callback)
            .where(
                # From "done"; a callback never done from its creation
                # (Maxi, 26.10.2026): nobody calls back after the period.
                func.coalesce(Callback.done_at, Callback.created_at)
                < now - timedelta(days=settings.retention_callback_days),
                or_(Callback.phone != "", Callback.summary != ""),
            )
            .values(phone="", summary=""),
        ),
        "outbox_payloads": lambda: _outbox_payloads(session, now),
        "outbox_rows": lambda: _run(
            session,
            delete(OutboxEvent).where(
                OutboxEvent.created_at
                < now - timedelta(days=settings.retention_outbox_row_days)
            ),
        ),
        "audit_entries": lambda: _run(
            session,
            delete(AuditLog).where(
                AuditLog.at < now - timedelta(days=settings.retention_audit_log_days)
            ),
        ),
        "consent_answers": lambda: _consent_answers(
            session, now, settings.retention_consent_log_years
        ),
    }
    counts: dict[str, int] = {}
    failed: list[str] = []
    for name, step in steps.items():
        try:
            with session.begin_nested():
                result = step()
        except Exception as exc:  # noqa: BLE001 - one period must not stop the rest
            failed.append(name)
            log(
                logger,
                logging.ERROR,
                "Retention step failed",
                step=name,
                error=error_text(exc),
            )
            continue
        counts.update(result if isinstance(result, dict) else {name: result})
    session.commit()
    log(
        logger,
        logging.INFO,
        "Retention pass",
        **counts,
        **({"failed": failed} if failed else {}),
    )
    return counts


def _run(session: Session, statement) -> int:
    return session.execute(statement).rowcount  # type: ignore[attr-defined]


def _allergy_notes(session: Session, now: datetime, days: int) -> int:
    """L13: the night after the order or reservation, collected or not. The
    business day ends at 05:00 in the tenant's time zone (core/time.py).

    ponytail: no lower date bound, so each pass scans the whole order history
    (kept for the tax adviser, L18) and catches up a missed pass; add a bound
    or a partial index on notes not yet stripped if a pass gets slow."""
    changed = 0
    for tenant_id, zone in session.execute(select(Tenant.id, Tenant.timezone)).all():
        day = business_day(now, zone) - timedelta(days=days)
        cutoff = business_day_bounds_utc(day, zone)[0]
        orders = select(Order.id).where(
            Order.tenant_id == tenant_id, cutoff > ORDER_DAY
        )
        changed += _run(
            session,
            update(OrderItem)
            .where(OrderItem.order_id.in_(orders), OrderItem.note.op("~*")(ALLERGY))
            .values(note=None),
        )
        changed += _run(
            session,
            update(Order)
            .where(Order.id.in_(orders), Order.note.op("~*")(ALLERGY))
            .values(note=None),
        )
        tables = select(Reservation.id).where(
            Reservation.tenant_id == tenant_id, Reservation.reserved_for < cutoff
        )
        changed += _run(
            session,
            update(Reservation)
            .where(Reservation.id.in_(tables), Reservation.note.op("~*")(ALLERGY))
            .values(note=None),
        )
        changed += _outbox_allergy_notes(session, tenant_id, orders, tables)
    return changed


def _outbox_allergy_notes(session: Session, tenant_id, orders, tables) -> int:
    """confirm copies the notes into the outbox payload (`note`, `items[].note`;
    domain/ordering/confirm.py, domain/confirm.py), where they would stay until
    the payload is cut days later (Codex PR #371). The copies of the same
    orders and reservations lose their allergy notes in the same pass; every
    other key stays until its own period. SKIP LOCKED: an event the bridge or
    the dispatcher holds right now is caught by the next pass."""
    events = session.scalars(
        select(OutboxEvent)
        .where(
            OutboxEvent.tenant_id == tenant_id,
            or_(
                OutboxEvent.payload["order_id"].astext.in_(
                    orders.with_only_columns(cast(Order.id, Text))
                ),
                OutboxEvent.payload["reservation_id"].astext.in_(
                    tables.with_only_columns(cast(Reservation.id, Text))
                ),
            ),
            cast(OutboxEvent.payload, Text).op("~*")(ALLERGY),
        )
        .with_for_update(skip_locked=True)
    ).all()
    changed = 0
    for event in events:
        payload = _without_allergy(event.payload)
        if payload != event.payload:
            event.payload = payload
            changed += 1
    session.flush()
    return changed


def _without_allergy(payload: dict) -> dict:
    """The payload with every allergy note set to None, the rest as it was."""

    def clean(note: object) -> object:
        return None if isinstance(note, str) and _ALLERGY_RE.search(note) else note

    out = dict(payload)
    if "note" in out:
        out["note"] = clean(out["note"])
    if isinstance(out.get("items"), list):
        out["items"] = [
            {**item, "note": clean(item["note"])}
            if isinstance(item, dict) and "note" in item
            else item
            for item in out["items"]
        ]
    return out


def _consent_answers(session: Session, now: datetime, years: int) -> int:
    """An answer to a consent question (L13, T-11.1) goes at the end of the
    `years`-th calendar year after the year it was given (Maxi, 26.10.2026):
    on any day of year Y, every answer from before 1 January of Y - years. The
    year is the restaurant's: 00:30 on New Year's Day in Berlin is still the
    old year in UTC."""
    deleted = 0
    for tenant_id, zone in session.execute(select(Tenant.id, Tenant.timezone)).all():
        cutoff = datetime(to_local(now, zone).year - years, 1, 1, tzinfo=tz(zone))
        deleted += _run(
            session,
            delete(ConsentLog).where(
                ConsentLog.tenant_id == tenant_id, ConsentLog.answered_at < cutoff
            ),
        )
    return deleted


def _guest_details(session: Session, cutoff: datetime) -> dict[str, int]:
    """Name, phone, notes and the customer link, so the row cannot be traced
    through the customer record (docs/03). `address_id` joins here with T-6.1."""
    old_orders = select(Order.id).where(cutoff > ORDER_DAY)
    item_notes = _run(
        session,
        update(OrderItem)
        .where(OrderItem.order_id.in_(old_orders), OrderItem.note.is_not(None))
        .values(note=None),
    )
    orders = _run(
        session,
        update(Order)
        .where(
            cutoff > ORDER_DAY,
            or_(
                Order.customer_name != "",
                Order.phone != "",
                Order.note.is_not(None),
                Order.customer_id.is_not(None),
                Order.address_id.is_not(None),
            ),
        )
        .values(
            customer_name="", phone="", note=None, customer_id=None, address_id=None
        ),
    )
    reservations = _run(
        session,
        update(Reservation)
        .where(
            Reservation.reserved_for < cutoff,
            or_(
                Reservation.guest_name != "",
                Reservation.phone != "",
                Reservation.note.is_not(None),
            ),
        )
        .values(guest_name="", phone="", note=None),
    )
    return {
        "orders": orders,
        "order_item_notes": item_notes,
        "reservations": reservations,
    }


def _outbox_payloads(session: Session, now: datetime) -> int:
    """A sent payload goes RETENTION_OUTBOX_SENT_DAYS after dispatch, any other
    at most RETENTION_OUTBOX_UNSENT_DAYS after it was written. An unsent one is
    marked failed with it: the bridge or n8n must never get a guest-less slip."""
    thin = func.jsonb_strip_nulls(
        func.jsonb_build_object(
            *(
                arg
                for key in OUTBOX_KEEP
                for arg in (cast(key, Text), OutboxEvent.payload[key])
            )
        )
    )
    sent = _run(
        session,
        update(OutboxEvent)
        .where(
            OutboxEvent.status == "sent",
            OutboxEvent.sent_at
            < now - timedelta(days=settings.retention_outbox_sent_days),
            OutboxEvent.payload != thin,
        )
        .values(payload=thin),
    )
    days = settings.retention_outbox_unsent_days
    unsent = _run(
        session,
        update(OutboxEvent)
        .where(
            OutboxEvent.status != "sent",
            OutboxEvent.created_at < now - timedelta(days=days),
            OutboxEvent.payload != thin,
        )
        .values(
            payload=thin,
            status="failed",
            # A failed event keeps the error it failed with.
            last_error=case(
                (OutboxEvent.status == "pending", UNSENT_EXPIRED.format(days=days)),
                else_=OutboxEvent.last_error,
            ),
        ),
    )
    return sent + unsent


if __name__ == "__main__":
    configure_logging(settings.log_level)
    with SessionLocal() as session:
        purge(session, utcnow())
