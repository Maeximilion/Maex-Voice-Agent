"""create_callback: Rückruf-Aufgabe für das Team anlegen (docs/04 §create_callback).

Der Rückruf ist der Ausweg aus jeder Sackgasse: verstanden hat der Agent nichts,
der Gast will einen Menschen, es geht um eine Beschwerde. Die Aufgabe landet in
der Datenbank, das Team sieht sie in der Oberfläche, n8n bekommt das Ereignis.
"""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.core.errors import InvalidInput, NotFound
from api.core.time import utcnow
from api.domain.customers.phone import normalize_phone
from api.events import enqueue
from api.events.types import CALLBACK_CREATED
from api.models import AuditLog, Call, Callback, Tenant
from api.schemas.callbacks import CallbackTask, CreateCallbackRequest

ACTOR_AGENT = "agent"
ACTION_CREATED = "callback.created"
SAY_CALL_UNKNOWN = "Bei mir gibt es gerade eine technische Störung. Ich verbinde Sie mit dem Restaurant."
SAY_NOTED = (
    "Ich habe Ihre Nummer notiert. Das Restaurant ruft Sie so bald wie möglich zurück."
)


def create_callback(
    session: Session, req: CreateCallbackRequest, now: datetime | None = None
) -> CallbackTask:
    now = now or utcnow()
    if session.get(Tenant, req.tenant_id) is None:
        raise NotFound("Mandant unbekannt")

    call = session.get(Call, req.call_id)
    if call is None or call.tenant_id != req.tenant_id:
        raise NotFound("Anruf unbekannt", say=SAY_CALL_UNKNOWN)

    summary = req.summary.strip()
    if not summary:
        raise InvalidInput("summary: darf nicht leer sein")
    phone = normalize_phone(req.phone)

    # Ein Anruf, ein offener Rückruf: löst der Agent nach einem Zeitüberlauf ein
    # zweites Mal aus, bekommt das Team keinen zweiten Zettel. Deshalb traegt der
    # Zustand die Idempotenz und nicht ein Schlüssel (wie in domain/confirm.py).
    open_task = _open_for_call(session, req)
    if open_task is not None:
        session.commit()
        return _task(open_task)

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

    session.add(
        AuditLog(
            tenant_id=req.tenant_id,
            actor=ACTOR_AGENT,
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


def _open_for_call(session: Session, req: CreateCallbackRequest) -> Callback | None:
    """Offener Rückruf desselben Anrufs, mit Zeilensperre gegen zwei gleichzeitige Aufrufe."""
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
        .with_for_update(),
        execution_options={"populate_existing": True},
    ).scalar_one_or_none()


def _task(c: Callback) -> CallbackTask:
    return CallbackTask(
        callback_id=c.id,
        status=c.status,
        phone=c.phone,
        reason=c.reason,
        summary=c.summary,
        say=SAY_NOTED,
    )
