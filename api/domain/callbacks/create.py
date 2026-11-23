"""create_callback: Rückruf-Aufgabe für das Team anlegen (docs/04 §create_callback).

Der Rückruf ist der Ausweg aus jeder Sackgasse: verstanden hat der Agent nichts,
der Gast will einen Menschen, es geht um eine Beschwerde. Die Aufgabe landet in
der Datenbank, das Team sieht sie in der Oberfläche, n8n bekommt das Ereignis.
"""

import uuid
from datetime import datetime
from typing import cast
from zoneinfo import ZoneInfo

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from api.core.errors import SAY_ON_FAILURE, InvalidInput, NotFound
from api.core.time import utcnow
from api.domain.customers.contact import note_contact
from api.domain.customers.phone import normalize_phone
from api.events import enqueue
from api.events.types import CALLBACK_CREATED
from api.models import AuditLog, Call, Callback, Order, Reservation, Tenant
from api.schemas.callbacks import (
    CallbackReason,
    CallbackStatus,
    CallbackTask,
    CreateCallbackRequest,
)

ACTOR_AGENT = "agent"
ACTION_CREATED = "callback.created"
SAY_NOTED = (
    "Ich habe Ihre Nummer notiert. Das Restaurant ruft Sie so bald wie möglich zurück."
)
# How a card names the bookings of its call that stand, so the team does not
# book one a second time (Codex PR #347, #743, #764). Every card takes them
# from the database here, whoever wrote its text. Drafts.
SUMMARY_BOOKED = "Bereits gebucht:"
_TABLE_BOOKED = "Reservierung, {party} Personen, {when:%d.%m.%Y %H:%M} Uhr, Name {name}"
_ORDER_BOOKED = {
    "pickup": "Bestellung zur Abholung, Name {name}",
    "delivery": "Bestellung zur Lieferung, Name {name}",
}
# What the guest's yes made of an order (`confirm`) and nobody took back.
_ORDER_STANDS = ("confirmed", "approved", "handed_over")
# `CreateCallbackRequest.summary`: the card's own text gives way to the
# bookings, which are never cut.
SUMMARY_MAX = 1000


def create_callback(
    session: Session,
    req: CreateCallbackRequest,
    now: datetime | None = None,
    *,
    actor: str = ACTOR_AGENT,
) -> CallbackTask:
    now = now or utcnow()
    if session.get(Tenant, req.tenant_id) is None:
        raise NotFound("Mandant unbekannt")

    call = session.get(Call, req.call_id)
    if call is None or call.tenant_id != req.tenant_id:
        raise NotFound("Anruf unbekannt", say=SAY_ON_FAILURE)

    summary = req.summary.strip()
    if not summary:
        raise InvalidInput("summary: darf nicht leer sein")
    phone = normalize_phone(req.phone)

    # Ein Anruf, ein offener Rückruf: löst der Agent nach einem Zeitüberlauf ein
    # zweites Mal aus, bekommt das Team keinen zweiten Zettel. Deshalb traegt der
    # Zustand die Idempotenz und nicht ein Schlüssel (wie in domain/confirm.py).
    _lock_call(session, req.tenant_id, req.call_id)
    open_task = _open_for_call(session, req)
    if open_task is not None:
        session.commit()
        return _task(open_task)
    summary = _with_bookings(summary, booked(session, req.tenant_id, req.call_id))

    callback = Callback(
        tenant_id=req.tenant_id,
        call_id=req.call_id,
        phone=phone,
        reason=req.reason,
        summary=summary,
        status="open",
        created_at=now,
    )
    session.add(callback)
    session.flush()
    note_contact(session, req.tenant_id, req.call_id, phone, now)

    session.add(
        AuditLog(
            tenant_id=req.tenant_id,
            actor=actor,
            action=ACTION_CREATED,
            entity="callback",
            entity_id=callback.id,
            payload={"call_id": str(req.call_id), "reason": req.reason},
        )
    )
    enqueue(
        session,
        tenant_id=req.tenant_id,
        event_type=CALLBACK_CREATED,
        # Vollständig, damit n8n den Rückruf ohne zweite Abfrage melden kann.
        payload={
            "callback_id": str(callback.id),
            "call_id": str(req.call_id),
            "phone": phone,
            "reason": req.reason,
            "summary": summary,
        },
    )
    session.commit()
    return _task(callback)


def booked(session: Session, tenant_id: uuid.UUID, call_id: uuid.UUID) -> str | None:
    """The reservations and orders of the call that stand, in the order they
    were made, from the database (CLAUDE.md §2 rule 1). Without their notes:
    one can name the guest's health, and a card is kept longer than that
    (L13). Fresh rows: the call's session can hold one the team has moved
    on the tablet since (`expire_on_commit=False`)."""
    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        return None
    zone = ZoneInfo(tenant.timezone)
    fresh = {"populate_existing": True}
    tables = session.scalars(
        select(Reservation).where(
            Reservation.tenant_id == tenant_id,
            Reservation.call_id == call_id,
            Reservation.status == "confirmed",
            Reservation.deleted_at.is_(None),
        ),
        execution_options=fresh,
    ).all()
    orders = session.scalars(
        select(Order).where(
            Order.tenant_id == tenant_id,
            Order.call_id == call_id,
            Order.status.in_(_ORDER_STANDS),
            Order.deleted_at.is_(None),
        ),
        execution_options=fresh,
    ).all()
    named = [
        (
            table.created_at,
            _TABLE_BOOKED.format(
                party=table.party_size,
                when=table.reserved_for.astimezone(zone),
                name=table.guest_name,
            ),
        )
        for table in tables
    ]
    for order in orders:
        line = _ORDER_BOOKED[order.type].format(name=order.customer_name)
        if order.pickup_code:
            line += f", Code {order.pickup_code}"
        named.append((order.created_at, line))
    if not named:
        return None
    named.sort(key=lambda entry: entry[0])
    return f"{SUMMARY_BOOKED} {'; '.join(line for _, line in named)}."


def _with_bookings(summary: str, bookings: str | None) -> str:
    """The card's text with the bookings of its call at its end. The text is
    cut to make room, the bookings never: a card without one lets the team
    book it a second time. Bookings alone longer than a card leave no room
    for its text; the reason still labels the card."""
    if bookings is None:
        return summary
    room = max(SUMMARY_MAX - len(bookings) - 1, 0)
    return f"{summary[:room].rstrip()} {bookings}".lstrip()


def _lock_call(session: Session, tenant_id: uuid.UUID, call_id: uuid.UUID) -> None:
    """Prüfen und Anlegen laufen je Anruf nacheinander.

    Eine Sperre auf die *Rückruf*-Zeile reicht nicht: beim ersten Rückruf gibt es
    sie noch nicht, und zwei gleichzeitige Aufrufe finden beide nichts und legen
    beide an. Die Zeile des Anrufs waere sperrbar (sie existiert), doch die
    Advisory-Sperre sagt genau das, was gemeint ist — dieser Anruf wird gerade
    bearbeitet — ohne eine fremde Zeile dafuer zu belegen, und sie ist im Repo
    bereits etabliert (domain/reservations/create.py).

    Vertrag dieser Sperre, an den sich jeder kuenftige Schreibpfad auf callbacks
    halten muss:
    - wer einen Rückruf anlegt, haelt vorher diese Sperre fuer denselben Anruf,
    - die Datenbank faehrt READ COMMITTED (api/db.py), sonst sieht der Wartende
      den fremden Commit nicht und legt trotzdem ein zweites Mal an.
    Ein partieller Unique-Index waere der haertere Riegel; er kommt, sobald es
    einen zweiten Schreibpfad gibt (siehe docs/01_STATUS.md, Offene Punkte).
    """
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:key)::bigint)"),
        {"key": f"callbacks:{tenant_id}:{call_id}"},
    )


def _open_for_call(session: Session, req: CreateCallbackRequest) -> Callback | None:
    """Offener Rückruf desselben Anrufs. Die Sperre hält bereits, siehe _lock_call."""
    return session.execute(
        select(Callback)
        .where(
            Callback.call_id == req.call_id,
            Callback.tenant_id == req.tenant_id,
            Callback.status == "open",
            Callback.deleted_at.is_(None),
        )
        .order_by(Callback.created_at)
        .limit(1),
        execution_options={"populate_existing": True},
    ).scalar_one_or_none()


def _task(c: Callback) -> CallbackTask:
    # The columns are text; ck_callbacks_status and ck_callbacks_reason keep them
    # to these values, and CallbackTask checks them again.
    return CallbackTask(
        callback_id=c.id,
        status=cast(CallbackStatus, c.status),
        phone=c.phone,
        reason=cast(CallbackReason, c.reason),
        summary=c.summary,
        say=SAY_NOTED,
    )
