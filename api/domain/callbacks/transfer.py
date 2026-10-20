"""transfer_to_team: Durchwahl, Erreichbarkeit, Schleifenschutz (docs/04 §transfer_to_team).

Bei Beschwerde, Mensch-Wunsch oder Storno gibt der Agent sofort ab (docs/05 §4).
Ziel ist immer die Durchwahl aus service_config, nie eine Hauptnummer — die kommt
im Code gar nicht vor. Der Übergang läuft je Anruf höchstens einmal: erst der
Zustand auf calls.transfer_reason macht ihn wahr, ein zweiter Aufruf (Modell-Loop,
Plattform-Retry) liest nur den bereits gesetzten Grund, ohne ein zweites Mal ins
audit_log zu schreiben. Ist niemand erreichbar, findet kein Übergang statt: der
Agent legt stattdessen einen Rückruf an (create_callback), und dieser Aufruf darf
weder den Zustand noch das Audit belegen, sonst zählt ein Anruf als "transferred",
obwohl das Team ihn nie bekommen hat (Codex-Review PR #98, P2).
"""

import uuid
from datetime import datetime, timedelta
from typing import cast, get_args
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.core.errors import InvalidInput, NotFound
from api.core.time import utcnow
from api.domain.callbacks.create import create_callback
from api.domain.customers.phone import normalize_phone
from api.domain.status.hours import all_services, load_hours, open_window_at
from api.models import AuditLog, Call, ServiceConfig, Tenant
from api.schemas.callbacks import CallbackReason, CallbackTask, CreateCallbackRequest
from api.schemas.transfer import TransferResult, TransferToTeamRequest

ACTOR_AGENT = "agent"
ACTOR_TELEPHONY = "telephony"
ACTION_TRANSFERRED = "call.transferred"
SAY_CALL_UNKNOWN = "Bei mir gibt es gerade eine technische Störung. Ich verbinde Sie mit dem Restaurant."
# A call the line reports is at most this old: the dialplan's backstop of an
# hour on our leg, then up to two team legs of 30 s. The line's ids repeat
# after a restart of Asterisk; an older row of the same id is another call.
MISSED_WINDOW = timedelta(hours=2)
# For the team, like the summaries of `agent/loop.py`: what happened, and the
# guest's concern as far as the transfer named it (the reason is the card's
# label).
SUMMARY_MISSED = "Ans Team weitergeleitet, dort hat niemand abgenommen."
SUMMARY_BY_REASON = {
    "cancellation": "Kunde wollte etwas stornieren.",
    None: "Anliegen nicht bekannt.",
}
_CALLBACK_REASONS = frozenset(get_args(CallbackReason))


def transfer_to_team(
    session: Session, req: TransferToTeamRequest, now: datetime | None = None
) -> TransferResult:
    now = now or utcnow()
    tenant = session.get(Tenant, req.tenant_id)
    config = session.get(ServiceConfig, req.tenant_id)
    if tenant is None or config is None:
        raise NotFound("Mandant unbekannt oder ohne service_config")

    # Zeilensperre bis Transaktionsende, wie in domain/confirm.py: zwei
    # gleichzeitige Aufrufe desselben Anrufs duerfen den Zustand nur einmal setzen.
    call = session.execute(
        select(Call).where(Call.id == req.call_id).with_for_update(),
        execution_options={"populate_existing": True},
    ).scalar_one_or_none()
    if call is None or call.tenant_id != req.tenant_id:
        raise NotFound("Anruf unbekannt", say=SAY_CALL_UNKNOWN)

    available = _team_reachable(session, req.tenant_id, tenant.timezone, now)

    # Nur ein tatsächlicher Übergang belegt den Zustand und das Audit. Ist niemand
    # erreichbar, ist dieser Aufruf folgenlos: der Agent legt stattdessen einen
    # Rückruf an, und ein späterer echter Übergang im selben Anruf muss noch möglich sein.
    if available and call.transfer_reason is None:
        call.transfer_reason = req.reason
        session.add(
            AuditLog(
                tenant_id=req.tenant_id,
                actor=ACTOR_AGENT,
                action=ACTION_TRANSFERRED,
                entity="call",
                entity_id=call.id,
                payload={"reason": req.reason},
            )
        )
    session.commit()
    return TransferResult(transfer_to=config.team_phone, available=available)


def team_missed(
    session: Session,
    tenant_id: uuid.UUID,
    external_session_id: str,
    *,
    phone: str | None = None,
    now: datetime | None = None,
) -> CallbackTask | None:
    """Nobody answered the team leg of a call: a callback card, the team's only
    notice of the call then (docs/20 §5, T-10.8). None when there is none to
    write: the call is unknown, or there is no number to call back.

    `external_session_id` is the id the line gave the call; our leg and its log
    may be closed already. `phone` is a number the guest named in the call,
    which only the line's process still knows; else the caller id. The call's
    outcome becomes "callback", also when its log is still being written
    (`end_call` keeps it); an "error" stays."""
    now = now or utcnow()
    call = session.execute(
        select(Call)
        .where(
            Call.tenant_id == tenant_id,
            Call.external_session_id == external_session_id,
            Call.started_at >= now - MISSED_WINDOW,
        )
        .order_by(Call.started_at.desc())
        .limit(1)
        .with_for_update(),
        execution_options={"populate_existing": True},
    ).scalar_one_or_none()
    number = None if call is None else _number(phone) or _number(call.caller_id)
    if call is None or number is None:
        session.rollback()
        return None

    reason = call.transfer_reason
    if call.outcome in (None, "transferred"):
        call.outcome = "callback"
    return create_callback(
        session,
        CreateCallbackRequest(
            call_id=call.id,
            tenant_id=tenant_id,
            phone=number,
            reason=_card_reason(reason),
            summary=" ".join(
                part for part in (SUMMARY_MISSED, SUMMARY_BY_REASON.get(reason)) if part
            ),
        ),
        now=now,
        actor=ACTOR_TELEPHONY,
    )


def _card_reason(reason: str | None) -> CallbackReason:
    """The transfer's reason where it is one for a callback too. A cancellation
    is not (docs/03), as in `agent/loop.py`; none at all is a transfer the core
    gave no reason for (paused, shadow, the outage path)."""
    if reason in _CALLBACK_REASONS:
        return cast(CallbackReason, reason)
    return "human_requested" if reason == "cancellation" else "out_of_scope"


def _number(raw: str | None) -> str | None:
    """E.164, or None. The message of a refused number holds the number, so it
    goes nowhere."""
    if not raw:
        return None
    try:
        return normalize_phone(raw)
    except InvalidInput:
        return None


def _team_reachable(
    session: Session, tenant_id: uuid.UUID, timezone: str, now: datetime
) -> bool:
    """Angenommen erreichbar, solange irgendein Service gerade offen hat.

    Ein echter Leitungs-Ping auf die Durchwahl ist erst mit dem Telefonie-Adapter
    (T-1.11, nach D1) möglich. Bis dahin ist die Öffnungszeit der einzige Fakt in
    der DB, der etwas über Anwesenheit aussagt. Assumption vom 17.09.2026, siehe
    docs/01_STATUS.md — zu ersetzen, sobald der Adapter eine echte Antwort liefert.

    On the phone line the dial result now has the last word inside the opening
    hours: the team is rung, and a leg nobody answers becomes a callback card
    (`team_missed`, T-10.8). Outside them nobody is rung and the agent writes
    the card itself (Maxi, 20.10.2026). The text phone and the evals have no
    line and keep this guess alone.
    """
    zone = ZoneInfo(timezone)
    data = load_hours(session, tenant_id, now.astimezone(zone).date())
    return any(
        open_window_at(data, now, service, zone) is not None
        for service in all_services()
    )
