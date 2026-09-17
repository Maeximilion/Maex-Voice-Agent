"""confirm: der einzige Übergang von draft nach confirmed (docs/04 §confirm).

Generisch über beide Vorgangsarten. Bestellungen kommen mit Stufe 2, bis dahin
kennt der Verteiler unten nur Reservierungen.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.core.errors import Conflict, NotFound
from api.models import AuditLog, Call, OutboxEvent, Reservation, Tenant
from api.schemas.confirm import Confirmation, ConfirmRequest

ACTOR_AGENT = "agent"
SAY_STOERUNG = "Bei mir gibt es gerade eine technische Störung. Ich verbinde Sie mit dem Restaurant."
SAY_CANCELLED = (
    "Diese Reservierung wurde bereits storniert. Ich verbinde Sie mit dem Restaurant."
)

# Der Vorgang ist nach dem ersten confirm bestätigt; jeder weitere Aufruf liest
# denselben Zustand. Deshalb trägt die Zustandsmaschine die Idempotenz, nicht der
# idempotency_key: er wandert nur ins Protokoll (docs/04 §Gemeinsame Regeln).
CONFIRMED = Confirmation(status="confirmed", handover="queued", pickup_code=None)


def confirm(session: Session, req: ConfirmRequest) -> Confirmation:
    tenant = session.get(Tenant, req.tenant_id)
    if tenant is None:
        raise NotFound("Mandant unbekannt")

    call = session.get(Call, req.call_id)
    if call is None or call.tenant_id != req.tenant_id:
        raise NotFound("Anruf unbekannt", say=SAY_STOERUNG)

    if req.entity == "reservation":
        return _confirm_reservation(session, req)
    raise NotFound("Bestellungen gibt es erst ab Stufe 2", say=SAY_STOERUNG)


def _confirm_reservation(session: Session, req: ConfirmRequest) -> Confirmation:
    # Zeilensperre bis zum Ende der Transaktion: zwei gleichzeitige Aufrufe auf
    # denselben Entwurf dürfen nicht zweimal in die Outbox schreiben.
    reservation = session.execute(
        select(Reservation).where(Reservation.id == req.entity_id).with_for_update(),
        execution_options={"populate_existing": True},
    ).scalar_one_or_none()

    # Der Anruf muss derselbe sein wie beim Entwurf: das "Ja" gehört dem Gast, der
    # gerade in der Leitung ist (CLAUDE.md §2 Regel 3). Sonst bestätigt ein Anruf den
    # Tisch eines anderen Gastes, und Audit-Zeile und Outbox-Payload nennen zwei
    # verschiedene Anrufe.
    if (
        reservation is None
        or reservation.tenant_id != req.tenant_id
        or reservation.call_id != req.call_id
        or reservation.deleted_at is not None
    ):
        raise NotFound("Reservierung unbekannt", say=SAY_STOERUNG)

    if reservation.status == "cancelled":
        raise Conflict("Reservierung ist storniert", say=SAY_CANCELLED)

    if reservation.status == "confirmed":
        # Nichts zu schreiben; commit gibt nur die Sperre frei.
        session.commit()
        return CONFIRMED

    reservation.status = "confirmed"
    session.add(
        AuditLog(
            tenant_id=req.tenant_id,
            actor=ACTOR_AGENT,
            action="reservation.confirmed",
            entity="reservation",
            entity_id=reservation.id,
            payload={
                "call_id": str(req.call_id),
                "idempotency_key": req.idempotency_key,
            },
        )
    )
    session.add(
        OutboxEvent(
            tenant_id=req.tenant_id,
            event_type="reservation.confirmed",
            # Vollständig, damit der Versand im kalten Pfad ohne zweite Abfrage auskommt.
            payload={
                "reservation_id": str(reservation.id),
                "call_id": str(reservation.call_id),
                "guest_name": reservation.guest_name,
                "phone": reservation.phone,
                "party_size": reservation.party_size,
                "reserved_for": reservation.reserved_for.isoformat(),
                "note": reservation.note,
            },
        )
    )
    session.commit()
    return CONFIRMED
