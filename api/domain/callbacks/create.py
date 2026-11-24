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
from sqlalchemy.exc import IntegrityError
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
# The card of an order read back and never answered, which only the core
# writes (`ConversationLoop.offer_unanswered`). Where the call has an open
# card already, the order joins it (#993, Maxi 24.11.2026, option A).
ORDER_OPEN = "order_open"
ACTION_ORDER_ADDED = "callback.order_added"
# How the core's cards name an order that stands unplaced (`agent/loop.py`,
# #207). A card of the core that names one joins the open card too: a
# handoff's or a team leg's card would otherwise meet the older card and the
# order stand on none (#998).
SUMMARY_OPEN_ORDER = "Offene Bestellung, nicht aufgegeben:"
# A card's note when the guest spoke of an allergy in the call: the team asks
# about the kitchen note when it calls back, the card never names the allergy
# (L13). Draft wording; the core's cards lead with it (`agent/loop.py`).
SUMMARY_NOTE_TO_CLEAR = "Hinweis für die Küche mit dem Gast klären"
_MARKER = f"{SUMMARY_NOTE_TO_CLEAR}."
COMPLAINT = "complaint"
# The partial unique index of migration 047: one open card per call (#981).
OPEN_PER_CALL = "uq_callbacks_open_per_call"
# How each line `booked` writes begins: only a tail of such lines is the
# database's own, read again when an order joins a card (#993 review).
_BOOKED_LINES = tuple(
    line.split("{")[0] for line in (_TABLE_BOOKED, *_ORDER_BOOKED.values())
)


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
        return _replay(session, open_task, req, summary, actor)
    said = summary
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
    try:
        with session.begin_nested():
            session.add(callback)
            session.flush()
    except IntegrityError as exc:
        # The index behind the lock (#981): a writer that did not take the
        # lock put its card in first. That card is the call's card.
        if OPEN_PER_CALL not in str(exc.orig):
            raise
        open_task = _open_for_call(session, req)
        if open_task is None:
            raise
        return _replay(session, open_task, req, said, actor)
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


def _replay(
    session: Session,
    open_task: Callback,
    req: CreateCallbackRequest,
    summary: str,
    actor: str,
) -> CallbackTask:
    """The call's open card, as a second request gets it. Only a card that
    names an open order changes the open one; any other comes back as the
    card it was, a model's retry too (docs/04)."""
    if req.reason == ORDER_OPEN or SUMMARY_OPEN_ORDER in summary:
        _add_open_order(session, open_task, summary, actor, req.reason)
    session.commit()
    return _task(open_task)


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
        named.append((order.created_at, order_line(order)))
    if not named:
        return None
    named.sort(key=lambda entry: entry[0])
    return f"{SUMMARY_BOOKED} {'; '.join(line for _, line in named)}."


def order_line(order: Order) -> str:
    """An order as a card names it: its kind, the guest's name, its code."""
    line = _ORDER_BOOKED[order.type].format(name=order.customer_name)
    return f"{line}, Code {order.pickup_code}" if order.pickup_code else line


def _with_bookings(summary: str, bookings: str | None) -> str:
    """The card's text with the bookings of its call at its end. The text is
    cut to make room, the bookings never: a card without one lets the team
    book it a second time. Bookings alone longer than a card leave no room
    for its text; the reason still labels the card."""
    if bookings is None:
        return summary
    room = max(SUMMARY_MAX - len(bookings) - 1, 0)
    return f"{summary[:room].rstrip()} {bookings}".lstrip()


def _add_open_order(
    session: Session, card: Callback, order: str, actor: str, reason: str
) -> None:
    """The open order onto the open card of its call (#993): one card per
    call, and the team reads the order where it looks already. The card keeps
    its reason and number. Its own text gives way to the order, and the
    bookings at its end are read again from the database: a table booked
    since stands on it (CLAUDE.md §2 rule 1). The push names the order, the
    push of the card named only its old reason (#993 review). Once: a second
    hang-up of the call finds the order on it and changes nothing, and a
    card that names an open order already gets none more, its lines fitted
    to another room as they may be (#998). The kitchen marker stands once,
    in front, where no cut reaches it (#998 review). A complaint that joins
    makes the card a complaint: it goes to the top and alarms the tablet, and
    the push says so (#998 review); `reason` is the joining card's."""
    own = _own_text(card.summary)
    if SUMMARY_OPEN_ORDER in own:
        return
    marked = _MARKER in own or _MARKER in order
    own, order = _unmarked(own), _unmarked(order)
    if order in own:
        return
    bookings = booked(session, card.tenant_id, card.call_id)
    lead = len(_MARKER) + 1 if marked else 0
    room = SUMMARY_MAX - lead - len(order) - 1 - (len(bookings) + 1 if bookings else 0)
    # shortcut: a big order or the guest's words of a joining handoff leave
    # little of the card's own text, which is cut at its end; fit both in the
    # loop if the team misses a guest's words.
    text = " ".join(
        part
        for part in (_MARKER if marked else "", own[: max(room, 0)].rstrip(), order)
        if part
    )
    joined = _with_bookings(text, bookings)
    pushed = COMPLAINT if reason == COMPLAINT else ORDER_OPEN
    if joined == card.summary:
        return
    card.summary = joined
    if pushed == COMPLAINT:
        card.reason = COMPLAINT
    enqueue(
        session,
        tenant_id=card.tenant_id,
        event_type=CALLBACK_CREATED,
        payload={
            "callback_id": str(card.id),
            "call_id": str(card.call_id),
            "phone": card.phone,
            "reason": pushed,
            "summary": joined,
        },
    )
    session.add(
        AuditLog(
            tenant_id=card.tenant_id,
            actor=actor,
            action=ACTION_ORDER_ADDED,
            entity="callback",
            entity_id=card.id,
            payload={"call_id": str(card.call_id), "reason": pushed},
        )
    )


def _unmarked(text: str) -> str:
    """`text` without its kitchen marker, which a joined card carries once."""
    at = text.find(_MARKER)
    if at < 0:
        return text
    return f"{text[:at].rstrip()} {text[at + len(_MARKER) :].lstrip()}".strip()


def _own_text(summary: str) -> str:
    """A card's text without the bookings `booked` put at its end. Text that
    only reads like them stays: the model's words are not cut away."""
    head, cut, tail = summary.rpartition(SUMMARY_BOOKED)
    lines = tail.strip().removesuffix(".").split("; ")
    if cut and all(line.startswith(_BOOKED_LINES) for line in lines):
        return head.rstrip()
    return summary


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
    Behind it stands the partial unique index `uq_callbacks_open_per_call`
    (migration 047, #981): a writer without this lock meets it at the insert,
    and `create_callback` answers that conflict as a replay.
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
        .limit(1)
        # "Erledigt" takes the row, not the call's lock (`mark_done`): an order
        # that joins the card waits for it and then finds no open card (#993
        # review), never writes onto a done one.
        .with_for_update(),
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
