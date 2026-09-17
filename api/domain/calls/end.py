"""end_call: Anruf-Log schließt (docs/03 §calls, docs/04 §Endpunkte).

Zustands-Idempotenz wie bei domain/confirm.py: ein zweiter /end für denselben
Anruf (Plattform-Retry) liest nur das bereits geschriebene Ergebnis, statt es zu
überschreiben — sonst könnte ein verspätet eintreffendes "error" ein korrektes
"completed" verdrängen.
"""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.core.errors import NotFound
from api.core.time import utcnow
from api.models import Call
from api.schemas.calls import CallEnded, EndCallRequest


def end_call(
    session: Session, req: EndCallRequest, now: datetime | None = None
) -> CallEnded:
    now = now or utcnow()
    # Zeilensperre bis Transaktionsende, wie in domain/confirm.py: zwei gleichzeitige
    # /end-Aufrufe (Plattform-Retry) duerfen das Ergebnis nur einmal schreiben.
    call = session.execute(
        select(Call).where(Call.id == req.call_id).with_for_update(),
        execution_options={"populate_existing": True},
    ).scalar_one_or_none()
    if call is None or call.tenant_id != req.tenant_id:
        raise NotFound("Anruf unbekannt")

    if call.ended_at is not None:
        session.commit()
        return CallEnded(
            call_id=call.id,
            duration_seconds=call.duration_seconds or 0,
            outcome=call.outcome,
        )

    call.ended_at = now
    call.duration_seconds = max(0, round((now - call.started_at).total_seconds()))
    call.outcome = req.outcome
    call.intent = req.intent
    call.cost_cents = req.cost_cents
    call.model = req.model
    session.commit()
    return CallEnded(
        call_id=call.id, duration_seconds=call.duration_seconds, outcome=call.outcome
    )
