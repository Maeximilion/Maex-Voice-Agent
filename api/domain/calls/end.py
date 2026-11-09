"""end_call: Anruf-Log schließt (docs/03 §calls, docs/04 §Endpunkte).

Zustands-Idempotenz wie bei domain/confirm.py: ein zweiter /end für denselben
Anruf (Plattform-Retry) liest nur das bereits geschriebene Ergebnis, statt es zu
überschreiben — sonst könnte ein verspätet eintreffendes "error" ein korrektes
"completed" verdrängen.

`close_left_open_calls` closes the log of a call whose process ended before it
could (#545): the line writes the log only when it reports the end of the leg.
"""

from datetime import datetime, timedelta
from typing import cast

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from api.core.errors import NotFound
from api.core.logging import get_logger
from api.core.time import utcnow
from api.models import Call, ServiceConfig
from api.schemas.calls import CallEnded, EndCallRequest, Outcome


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
            # ended_at is set only together with an outcome, here or by
            # `close_left_open_calls`. CallEnded still checks it.
            outcome=cast(Outcome, call.outcome),
        )

    call.ended_at = now
    call.duration_seconds = max(0, round((now - call.started_at).total_seconds()))
    # An outcome set before the log: the team leg nobody answered was reported
    # while our leg's log was still being written (`callbacks.team_missed`).
    # An error wins, as it does there: outages are counted by it.
    call.outcome = (
        req.outcome if req.outcome == "error" else call.outcome or req.outcome
    )
    call.intent = req.intent
    call.cost_cents = req.cost_cents
    call.model = req.model
    session.commit()
    return CallEnded(
        call_id=call.id,
        duration_seconds=call.duration_seconds,
        outcome=cast(Outcome, call.outcome),
    )


# How long a live leg may run past `max_call_seconds`: the core checks the limit
# only between turns, and the last utterance (up to 60 s), the turn, the silence
# questions and the sentence before the hangup or transfer still follow. A few
# minutes at most; a live call closed here would lose its real outcome.
LEFT_OPEN_GRACE = timedelta(minutes=10)

logger = get_logger("api.domain.calls.end")


def close_left_open_calls(session: Session, now: datetime | None = None) -> int:
    """Close as `error` every call open longer than its tenant's
    `max_call_seconds` plus `LEFT_OPEN_GRACE`: its process ended (watchdog,
    deploy, out of memory) before the line reported the end of the leg, and
    nothing else will close it (#545). How long it really lasted and what it
    cost is not known, so both stay empty. One statement: a row that
    `end_call` holds is waited for and then left alone, as the condition on
    `ended_at` is checked again after the lock."""
    now = now or utcnow()
    closed = session.execute(
        update(Call)
        .where(
            Call.ended_at.is_(None),
            Call.tenant_id == ServiceConfig.tenant_id,
            Call.started_at
            < now
            - ServiceConfig.max_call_seconds * timedelta(seconds=1)
            - LEFT_OPEN_GRACE,
        )
        .values(ended_at=now, outcome="error")
        .returning(Call.id, Call.tenant_id)
        .execution_options(synchronize_session=False)
    ).all()
    session.commit()
    for call_id, tenant_id in closed:
        logger.warning(
            "call left open by its process, closed as error",
            extra={"extra": {"call_id": str(call_id), "tenant_id": str(tenant_id)}},
        )
    return len(closed)
