"""Rückrufe für die Spalte "Rückrufe" der Betriebsansicht (docs/06 §3, T-3.4).

Offene Rückrufe lesen, einen als erledigt markieren. Beschwerden stehen immer
oben, danach der älteste zuerst: wer am längsten wartet, wird zuerst angerufen.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Text, case, cast, func, literal, select
from sqlalchemy.dialects.postgresql import aggregate_order_by
from sqlalchemy.orm import Session

from api.core.errors import NotFound
from api.core.time import utcnow
from api.models import AuditLog, Callback

ACTOR_TABLET = "gui:tablet"
ACTION_DONE = "callback.done"
OPEN = "open"
DONE = "done"


@dataclass(frozen=True)
class OpenCallback:
    callback_id: uuid.UUID
    created_at: datetime
    phone: str
    reason: str
    summary: str


def _open_filters(tenant_id: uuid.UUID) -> tuple:
    return (
        Callback.tenant_id == tenant_id,
        Callback.status == OPEN,
        Callback.deleted_at.is_(None),
    )


def list_open(session: Session, tenant_id: uuid.UUID) -> list[OpenCallback]:
    complaint_first = case((Callback.reason == "complaint", 0), else_=1)
    rows = session.execute(
        select(
            Callback.id,
            Callback.created_at,
            Callback.phone,
            Callback.reason,
            Callback.summary,
        )
        .where(*_open_filters(tenant_id))
        .order_by(complaint_first, Callback.created_at, Callback.id)
    ).all()
    return [OpenCallback(*row) for row in rows]


def open_change_token(session: Session, tenant_id: uuid.UUID) -> str:
    """Fingerabdruck der offenen Rückrufe für den Ereignisstrom (gui/sse.py).

    Über die Menge der offenen ids, nicht über updated_at: das setzt nur das
    ORM, ein rohes UPDATE in der Datenbank bliebe sonst auf den Tablets
    unsichtbar (Befund Codex PR #111 an der Kopfzeile).
    """
    ids = cast(Callback.id, Text)
    count, digest = session.execute(
        select(
            func.count(Callback.id),
            func.md5(
                func.coalesce(
                    func.string_agg(ids, aggregate_order_by(literal(","), ids)), ""
                )
            ),
        ).where(*_open_filters(tenant_id))
    ).one()
    return f"{count}:{digest}"


def mark_done(
    session: Session,
    tenant_id: uuid.UUID,
    callback_id: uuid.UUID,
    actor: str = ACTOR_TABLET,
) -> Callback:
    """Rückruf erledigt. Zweimal getippt oder an zwei Tablets: ein Eintrag.

    Keine Rückfrage (docs/06 §1 Regel 6: nur Löschen und Stornieren fragen nach);
    erledigt ist erledigt, der Datensatz bleibt für Auswertung und Audit.
    """
    callback = session.scalars(
        select(Callback)
        .where(
            Callback.id == callback_id,
            Callback.tenant_id == tenant_id,
            Callback.deleted_at.is_(None),
        )
        .with_for_update()
    ).first()
    if callback is None:
        raise NotFound(f"Rueckruf {callback_id} nicht gefunden")
    if callback.status != DONE:
        callback.status = DONE
        callback.done_by = actor
        callback.done_at = utcnow()
        session.add(
            AuditLog(
                tenant_id=tenant_id,
                actor=actor,
                action=ACTION_DONE,
                entity="callback",
                entity_id=callback.id,
                payload={"call_id": str(callback.call_id), "reason": callback.reason},
            )
        )
    # Auch ohne Aenderung: commit gibt die Sperre frei.
    session.commit()
    return callback
