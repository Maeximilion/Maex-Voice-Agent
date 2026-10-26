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
from api.models import AuditLog, Call, Callback

ACTOR_TABLET = "gui:tablet"
ACTION_DONE = "callback.done"
OPEN = "open"
DONE = "done"

# Where the number to call back comes from. The guest may name any number
# (#252): only the one the call came from is backed by the line itself.
NUMBER_FROM_CALL = "caller_id"
NUMBER_OTHER = "other"
NUMBER_WITHHELD = "withheld"


@dataclass(frozen=True)
class OpenCallback:
    callback_id: uuid.UUID
    created_at: datetime
    phone: str
    reason: str
    summary: str
    caller_id: str | None

    @property
    def number_origin(self) -> str:
        """Both sides are E.164 where they could be normalized (create.py,
        calls/start.py). A caller ID that could not be is stored raw and never
        equals the callback number, so it counts as another number. An empty
        one (possible over /v1/calls/start) is no number at all."""
        if not (self.caller_id or "").strip():
            return NUMBER_WITHHELD
        return NUMBER_FROM_CALL if self.phone == self.caller_id else NUMBER_OTHER


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
            Call.caller_id,
        )
        .join(Call, Call.id == Callback.call_id)
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
    # The id, and whether the deletion job emptied the card (T-11.2): it clears
    # number and summary without changing the set of ids (Codex PR #371).
    ids = func.concat(
        cast(Callback.id, Text), case((Callback.phone == "", ":gone"), else_="")
    )
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
