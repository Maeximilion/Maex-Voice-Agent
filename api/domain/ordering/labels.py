"""Nummer und Name je Position, wie sie dem Gast vorgelesen oder vom Team korrigiert wurden.

Die Karte von jetzt ist dafuer die falsche Quelle: ein Gericht kann seit dem
Anruf umbenannt sein, auf Bon und Tablet steht aber, was vorgelesen wurde. Der
Stand liegt deshalb im audit_log, beim Anlegen (`order.draft_created`) und nach
jeder Korrektur im Tablet (`order.corrected`, T-4.7). Die juengste Zeile gilt.

Die Liste passt Position fuer Position zu `order_items` in der Reihenfolge von
`created_at`. Wer Positionen aendert, schreibt in derselben Transaktion eine neue
Liste - sonst zaehlen Bon und Replay die Positionen gegen die falschen Namen.
"""

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.core.logging import get_logger
from api.models import AuditLog, MenuItem, OrderItem

ACTION_DRAFT_CREATED = "order.draft_created"
ACTION_CORRECTED = "order.corrected"
LABEL_ACTIONS = (ACTION_DRAFT_CREATED, ACTION_CORRECTED)

logger = get_logger("api.domain.ordering.labels")


def current_labels_many(
    session: Session, order_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, list[list[str]]]:
    """Juengste Liste je Bestellung, alle in einer Abfrage (Spalte "Neue Bestellungen")."""
    if not order_ids:
        return {}
    rows = session.execute(
        select(AuditLog.entity_id, AuditLog.payload)
        .where(
            AuditLog.entity == "order",
            AuditLog.entity_id.in_(order_ids),
            AuditLog.action.in_(LABEL_ACTIONS),
        )
        # id ist fortlaufend: die spaetere Zeile ueberschreibt die fruehere.
        .order_by(AuditLog.id)
    ).all()
    labels: dict[uuid.UUID, list[list[str]]] = {}
    for entity_id, payload in rows:
        labels[entity_id] = payload["labels"]
    return labels


def current_labels(session: Session, order_id: uuid.UUID) -> list[list[str]] | None:
    """Juengste Liste einer Bestellung, `None` ohne Audit-Zeile."""
    return current_labels_many(session, [order_id]).get(order_id)


def menu_labels(session: Session, rows: Sequence[OrderItem]) -> list[list[str]]:
    """Nummer und Name aus der Karte von jetzt - nur der Notbehelf, siehe unten."""
    menu = {
        m.id: m
        for m in session.scalars(
            select(MenuItem).where(MenuItem.id.in_([r.menu_item_id for r in rows]))
        )
    }
    return [[menu[r.menu_item_id].number, menu[r.menu_item_id].name] for r in rows]


def labels_for_rows(
    session: Session,
    order_id: uuid.UUID,
    rows: Sequence[OrderItem],
    known: list[list[str]] | None = None,
) -> list[list[str]]:
    """Namensliste, die sicher zu `rows` passt.

    Passt die gespeicherte Liste nicht (darf nicht vorkommen: Entwurf, Korrektur
    und Liste entstehen in einer Transaktion), soll eine kaputte Bestellung
    weder die Spalte leeren noch die Korrektur mit einem Fehler 500 abbrechen.
    Dann eben die Namen der Karte von jetzt, mit Eintrag im Log.
    """
    labels = known if known is not None else current_labels(session, order_id)
    if labels is not None and len(labels) == len(rows):
        return labels
    logger.error("Positionen ohne passende Namensliste: Bestellung %s", order_id)
    return menu_labels(session, rows)
