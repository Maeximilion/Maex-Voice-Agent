"""start_call: Anruf-Log beginnt (docs/03 §calls, docs/04 §Endpunkte).

Ohne diese Zeile hat kein Tool-Aufruf eine gültige call_id (CLAUDE.md §8: "Jede
Transaktion trägt eine call_id"). Ein Plattform-Retry darf keinen zweiten Anruf
anlegen: dieselbe external_session_id auf einen noch offenen Anruf liefert die
bestehende call_id zurück, statt eine zweite Zeile zu schreiben.
"""

import uuid
from datetime import date, datetime

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from api.core.errors import InvalidInput, NotFound
from api.core.time import utcnow
from api.domain.customers.phone import normalize_phone
from api.models import Call, Tenant
from api.schemas.calls import CallStarted, StartCallRequest

# docs/03 §Löschfristen: "calls ohne personenbezogene Felder | 24 Monate".
RETENTION_MONTHS = 24


def start_call(
    session: Session, req: StartCallRequest, now: datetime | None = None
) -> CallStarted:
    now = now or utcnow()
    if session.get(Tenant, req.tenant_id) is None:
        raise NotFound("Mandant unbekannt")

    external_session_id = req.external_session_id.strip()
    if not external_session_id:
        raise InvalidInput("external_session_id: darf nicht leer sein")

    # Prüfen und Anlegen laufen je Session nacheinander, wie in
    # domain/callbacks/create.py: ohne die Sperre finden zwei gleichzeitige
    # Plattform-Retries beide keinen offenen Anruf und legen beide einen an, mit
    # zwei verschiedenen call_id (Codex-Review PR #99, P1).
    _lock_session(session, req.tenant_id, external_session_id)

    existing = session.scalars(
        select(Call).where(
            Call.tenant_id == req.tenant_id,
            Call.external_session_id == external_session_id,
            Call.ended_at.is_(None),
        )
    ).first()
    if existing is not None:
        return CallStarted(call_id=existing.id)

    call = Call(
        tenant_id=req.tenant_id,
        external_session_id=external_session_id,
        caller_id=_caller_id(req.caller_id),
        started_at=now,
        delete_after=_delete_after(now),
    )
    session.add(call)
    session.commit()
    return CallStarted(call_id=call.id)


def _lock_session(
    session: Session, tenant_id: uuid.UUID, external_session_id: str
) -> None:
    """Advisory-Sperre statt einer Zeilensperre: die Anruf-Zeile, um die es geht,
    existiert beim ersten Start noch nicht (gleiches Muster wie
    domain/callbacks/create.py `_lock_call`). Haelt bis Transaktionsende."""
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:key)::bigint)"),
        {"key": f"calls:start:{tenant_id}:{external_session_id}"},
    )


def _caller_id(raw: str | None) -> str | None:
    """Unterdrückte Nummer bleibt NULL. Ist die Plattform-Kennung keine gültige
    Nummer, wird sie unverändert übernommen statt den Anruf abzulehnen — ein Anruf
    darf nie verloren gehen (CLAUDE.md §2 Regel 5), nur weil das Log unvollständig wäre."""
    if raw is None:
        return None
    try:
        return normalize_phone(raw)
    except InvalidInput:
        return raw


def _delete_after(started_at: datetime) -> date:
    """24 Monate ab Anrufbeginn. Tag wird aufs Zielmonat begrenzt, z. B. 31. Januar
    plus 24 Monate ergibt 31. Januar (kein Schaltproblem), 31. Dezember plus 26
    Monate würde auf den 28./29. Februar begrenzt."""
    day = started_at.date()
    month_index = day.month - 1 + RETENTION_MONTHS
    year = day.year + month_index // 12
    month = month_index % 12 + 1
    return date(year, month, min(day.day, _days_in_month(year, month)))


def _days_in_month(year: int, month: int) -> int:
    next_month = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return (next_month - date(year, month, 1)).days
