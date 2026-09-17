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
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.core.errors import NotFound
from api.core.time import utcnow
from api.domain.status.hours import all_services, load_hours, open_window_at
from api.models import AuditLog, Call, ServiceConfig, Tenant
from api.schemas.transfer import TransferResult, TransferToTeamRequest

ACTOR_AGENT = "agent"
ACTION_TRANSFERRED = "call.transferred"
SAY_CALL_UNKNOWN = "Bei mir gibt es gerade eine technische Störung. Ich verbinde Sie mit dem Restaurant."


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


def _team_reachable(
    session: Session, tenant_id: uuid.UUID, timezone: str, now: datetime
) -> bool:
    """Angenommen erreichbar, solange irgendein Service gerade offen hat.

    Ein echter Leitungs-Ping auf die Durchwahl ist erst mit dem Telefonie-Adapter
    (T-1.11, nach D1) möglich. Bis dahin ist die Öffnungszeit der einzige Fakt in
    der DB, der etwas über Anwesenheit aussagt. Assumption vom 17.09.2026, siehe
    docs/01_STATUS.md — zu ersetzen, sobald der Adapter eine echte Antwort liefert.
    """
    zone = ZoneInfo(timezone)
    data = load_hours(session, tenant_id, now.astimezone(zone).date())
    return any(
        open_window_at(data, now, service, zone) is not None
        for service in all_services()
    )
