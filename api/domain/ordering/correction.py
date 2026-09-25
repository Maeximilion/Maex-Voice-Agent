"""Korrektur einer bestaetigten Bestellung im Tablet (docs/06 §3 "Korrigieren", T-4.7).

Das Team aendert Mengen, entfernt Positionen, tauscht ein Gericht gegen ein
anderes (Menge und Hinweis bleiben) oder nimmt eines dazu, und nennt einen
Grund. Jede Korrektur ist Rohstoff fuer die Genauigkeits-KPI (T-8.4): sie steht
als `order.corrected` im audit_log, mit Grund und vorher/nachher, ohne Name,
Telefon oder Hinweistext - das audit_log bleibt laenger als die Bestellung, und
ein Hinweis kann eine Allergie tragen.

Vorschau und Speichern rechnen mit derselben Funktion (`_plan`). Preise kommen
aus der Datenbank, nie aus dem Tablet (CLAUDE.md §2 Regel 1): eine behaltene
Position behaelt ihren eingefrorenen Grundpreis, eine getauschte oder neue
bekommt den Grundpreis der Karte von jetzt; die Summe entsteht neu ueber die
Positionen, die danach in der Datenbank stehen. Eine Pflichtauswahl wird
gewaehlt, nie mit der Voreinstellung gefuellt (Regel 2).

Ein Hinweis an einer Position ("WICHTIG: Keine Erdnuesse. Grund: Allergie", E14)
geht nie still verloren: eine Position mit Hinweis faellt nur mit
ausdruecklichem `drop_note` weg. "Falsches Gericht" korrigiert das Team mit
Tauschen, dann bleibt der Hinweis an der Position.
"""

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from api.core.errors import Conflict, InvalidInput, NotFound
from api.core.time import utcnow
from api.domain.menu.items import is_sold_out, option_groups, option_key

# Dieselbe Nummernsuche wie search_menu ("7" findet "07", "23A" findet "23a").
# Privat importiert, bis PR #139 search.py nicht mehr anfasst.
from api.domain.menu.search import _by_number
from api.domain.ordering.labels import ACTION_CORRECTED, labels_for_rows
from api.domain.ordering.pricing import row_cents
from api.domain.ordering.ticket import send_ticket
from api.models import AuditLog, MenuItem, Order, OrderItem
from api.schemas.menu import OptionGroup

# Dieselbe Obergrenze wie beim Agenten (draft_order).
from api.schemas.orders import MAX_QUANTITY

ACTOR_TABLET = "gui:tablet"
# Gruende aus docs/06 §3. "Falsche Adresse" gibt es nur bei Lieferungen.
REASONS = {
    "wrong_item": "falsches Gericht",
    "wrong_quantity": "falsche Menge",
    "wrong_address": "falsche Adresse",
    "other": "Sonstiges",
}
DELIVERY_ONLY = ("wrong_address",)
EDITABLE = ("confirmed", "approved")

NO_CHANGE = "Noch nichts geändert."
NO_POSITION = (
    "Mindestens ein Gericht muss bleiben. Eine leere Bestellung ist eine Stornierung."
)
MISSING_OPTION = "Bei {number} {name} fehlt noch: {groups}."
STALE = "Die Bestellung wurde inzwischen geändert. Bitte nochmal prüfen."
NOTE_KEPT = "Position mit Hinweis: bitte Tauschen statt Entfernen, oder den Hinweis ausdrücklich streichen."


@dataclass(frozen=True)
class Choice:
    group: str
    name: str


@dataclass(frozen=True)
class RowEdit:
    """Eine bestehende Position. Menge 0 heisst entfernen."""

    order_item_id: uuid.UUID
    quantity: int
    swap_to: uuid.UUID | None = None
    options: tuple[Choice, ...] = ()
    drop_note: bool = False


@dataclass(frozen=True)
class AddEdit:
    menu_item_id: uuid.UUID
    quantity: int
    options: tuple[Choice, ...] = ()


@dataclass(frozen=True)
class CorrectionRequest:
    """Was das Tablet schickt. `version` ist der Stand, den das Team gesehen hat."""

    version: str
    rows: tuple[RowEdit, ...] = ()
    added: tuple[AddEdit, ...] = ()
    edit_id: str = ""


@dataclass
class PlannedLine:
    kind: str  # kept · changed · removed · swapped · added
    number: str
    name: str
    quantity: int
    unit_price_cents: int
    options: list[dict]
    note: str | None
    menu_item_id: uuid.UUID
    # Nur fuer getauschte und neue Positionen: Auswahl, die das Tablet anbietet.
    groups: list[OptionGroup] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    sold_out: bool = False
    # Bestehende Position, auf die sich die Zeile bezieht; None bei neuen.
    order_item_id: uuid.UUID | None = None

    @property
    def cents(self) -> int:
        return row_cents(self.unit_price_cents, self.options) * self.quantity


@dataclass
class CorrectionPlan:
    order_id: uuid.UUID
    version: str
    rows: list[PlannedLine]
    added: list[PlannedLine]
    before_total_cents: int
    items_total_cents: int
    total_cents: int
    changed: bool
    # Warum nicht gespeichert werden kann, in Worten fuer das Team.
    blockers: list[str]
    reasons: dict[str, str]


def order_version(rows: list[OrderItem]) -> str:
    """Fingerabdruck der Positionen: der Stand, auf den sich eine Korrektur bezieht.

    Bewusst nicht updated_at: Uebergabe und Freigabe aendern die Zeile, aber
    nicht die Positionen; eine Korrektur waere sonst grundlos veraltet.
    """
    data = [
        [
            str(r.id),
            str(r.menu_item_id),
            r.quantity,
            r.unit_price_cents,
            r.options,
            r.note,
        ]
        for r in rows
    ]
    return hashlib.md5(
        json.dumps(data, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def find_by_number(
    session: Session, tenant_id: uuid.UUID, number: str
) -> MenuItem | None:
    """Aktives Gericht zu einer Kartennummer, nur bei genau einem Treffer."""
    text = number.strip().lower()
    if not text:
        return None
    items = _by_number(session, tenant_id, text)
    return items[0] if len(items) == 1 else None


def preview_correction(
    session: Session,
    tenant_id: uuid.UUID,
    order_id: uuid.UUID,
    req: CorrectionRequest | None = None,
    now: datetime | None = None,
) -> CorrectionPlan:
    """Vorschau fuer das Tablet. Schreibt nichts."""
    order = _order(session, tenant_id, order_id, lock=False)
    if order.status not in EDITABLE:
        # Storniert oder noch Entwurf: gar nicht erst bearbeiten lassen, statt
        # erst beim Speichern abzulehnen.
        raise Conflict(f"Bestellung {order.id} ist {order.status}", say=STALE)
    rows = _rows(session, order)
    if req is None:
        req = CorrectionRequest(version=order_version(rows))
    elif req.version != order_version(rows):
        raise Conflict(STALE)
    return _plan(session, order, rows, req, now or utcnow())


def apply_correction(
    session: Session,
    tenant_id: uuid.UUID,
    order_id: uuid.UUID,
    req: CorrectionRequest,
    reason: str,
    actor: str = ACTOR_TABLET,
    now: datetime | None = None,
) -> None:
    """Korrektur speichern. Zweimal getippt oder an zwei Tablets: einmal gespeichert."""
    now = now or utcnow()
    order = _order(session, tenant_id, order_id, lock=True)
    if order.status not in EDITABLE:
        raise Conflict(f"Bestellung {order.id} ist {order.status}", say=STALE)
    if req.edit_id and _already_saved(session, order, req.edit_id):
        # Derselbe Tap kam zweimal an: die erste Korrektur gilt, keine zweite.
        session.commit()
        return
    if reason not in _reasons(order):
        raise InvalidInput(f"Grund {reason!r} unbekannt")

    rows = _rows(session, order)
    if req.version != order_version(rows):
        raise Conflict("Positionen seit dem Oeffnen geaendert", say=STALE)
    plan = _plan(session, order, rows, req, now)
    if plan.blockers:
        raise InvalidInput(plan.blockers[0], say=plan.blockers[0])
    _check_notes(rows, req)

    labels = labels_for_rows(session, order.id, rows)
    before = [_audit_line(r, lbl) for r, lbl in zip(rows, labels, strict=True)]
    by_id = {r.id: r for r in rows}
    for line, edit in zip(plan.rows, _row_edits(rows, req), strict=True):
        row = by_id[edit.order_item_id]
        if line.kind == "removed":
            session.delete(row)
            continue
        row.quantity = line.quantity
        if line.kind == "swapped":
            row.menu_item_id = line.menu_item_id
            row.unit_price_cents = line.unit_price_cents
            row.options = line.options
    # Neue Positionen hinter die letzte: die Reihenfolge ist created_at (draft.py).
    last = max(r.created_at for r in rows)
    for k, line in enumerate(plan.added, start=1):
        session.add(
            OrderItem(
                tenant_id=order.tenant_id,
                order_id=order.id,
                menu_item_id=line.menu_item_id,
                quantity=line.quantity,
                unit_price_cents=line.unit_price_cents,
                options=line.options,
                note=None,
                created_at=last + timedelta(microseconds=k),
            )
        )
    session.flush()

    after_rows = _rows(session, order)
    kept = [line for line in plan.rows if line.kind != "removed"] + plan.added
    after_labels = [[line.number, line.name] for line in kept]
    items_total = sum(
        row_cents(r.unit_price_cents, r.options) * r.quantity for r in after_rows
    )
    order.items_total_cents = items_total
    order.total_cents = items_total + order.delivery_fee_cents
    # Auch bei gleicher Summe: der Ereignisstrom erkennt die Korrektur daran.
    order.updated_at = func.clock_timestamp()

    session.add(
        AuditLog(
            tenant_id=order.tenant_id,
            actor=actor,
            action=ACTION_CORRECTED,
            entity="order",
            entity_id=order.id,
            payload={
                "call_id": str(order.call_id),
                "edit_id": req.edit_id,
                "reason": reason,
                "before": before,
                "after": [
                    _audit_line(r, lbl)
                    for r, lbl in zip(after_rows, after_labels, strict=True)
                ],
                "before_total_cents": plan.before_total_cents,
                "after_total_cents": items_total,
                "labels": after_labels,
                "note_dropped": any(
                    e.drop_note and e.quantity == 0 and by_id[e.order_item_id].note
                    for e in req.rows
                ),
            },
        )
    )
    session.flush()
    if order.handover_state is not None:
        # Der neue Stand geht an die Kueche (ticket.py). "KORREKTUR" nur, wenn
        # sie schon einen Bon hat oder bekommt; nach "failed" hat sie keinen.
        reached = order.handover_state in ("pending", "sent")
        send_ticket(session, order, correction_reason=reason if reached else None)
        order.handover_state = "pending"
    session.commit()


# --- Bausteine ---------------------------------------------------------------------


def _order(
    session: Session, tenant_id: uuid.UUID, order_id: uuid.UUID, lock: bool
) -> Order:
    stmt = select(Order).where(
        Order.id == order_id,
        Order.tenant_id == tenant_id,
        Order.deleted_at.is_(None),
    )
    if lock:
        stmt = stmt.with_for_update()
    order = session.execute(
        stmt, execution_options={"populate_existing": True}
    ).scalar_one_or_none()
    if order is None:
        raise NotFound(f"Bestellung {order_id} nicht gefunden", say=STALE)
    return order


def _rows(session: Session, order: Order) -> list[OrderItem]:
    return list(
        session.scalars(
            select(OrderItem)
            .where(
                OrderItem.tenant_id == order.tenant_id, OrderItem.order_id == order.id
            )
            .order_by(OrderItem.created_at, OrderItem.id)
            .execution_options(populate_existing=True)
        )
    )


def _reasons(order: Order) -> dict[str, str]:
    if order.type == "delivery":
        return dict(REASONS)
    return {k: v for k, v in REASONS.items() if k not in DELIVERY_ONLY}


def _already_saved(session: Session, order: Order, edit_id: str) -> bool:
    return (
        session.scalar(
            select(func.count(AuditLog.id)).where(
                AuditLog.tenant_id == order.tenant_id,
                AuditLog.entity == "order",
                AuditLog.entity_id == order.id,
                AuditLog.action == ACTION_CORRECTED,
                AuditLog.payload["edit_id"].astext == edit_id,
            )
        )
        > 0
    )


def _row_edits(rows: list[OrderItem], req: CorrectionRequest) -> list[RowEdit]:
    """Je bestehender Position die Aenderung, fehlende bleiben wie sie sind."""
    edits = {e.order_item_id: e for e in req.rows}
    unknown = edits.keys() - {r.id for r in rows}
    if unknown:
        raise Conflict("Position gehoert nicht zur Bestellung", say=STALE)
    return [
        edits.get(r.id, RowEdit(order_item_id=r.id, quantity=r.quantity)) for r in rows
    ]


def _check_notes(rows: list[OrderItem], req: CorrectionRequest) -> None:
    by_id = {r.id: r for r in rows}
    for edit in req.rows:
        if edit.quantity == 0 and by_id[edit.order_item_id].note and not edit.drop_note:
            raise InvalidInput(NOTE_KEPT, say=NOTE_KEPT)


def _plan(
    session: Session,
    order: Order,
    rows: list[OrderItem],
    req: CorrectionRequest,
    now: datetime,
) -> CorrectionPlan:
    edits = _row_edits(rows, req)
    labels = labels_for_rows(session, order.id, rows)
    wanted_ids = {e.swap_to for e in edits if e.swap_to} | {
        a.menu_item_id for a in req.added
    }
    menu = _menu(session, order.tenant_id, wanted_ids)
    groups = option_groups(session, list(menu))

    planned_rows = []
    for row, (number, name), edit in zip(rows, labels, edits, strict=True):
        quantity = _quantity(edit.quantity, allow_zero=True)
        if quantity == 0:
            kind = "removed"
            line = PlannedLine(
                kind,
                number,
                name,
                row.quantity,
                row.unit_price_cents,
                row.options,
                row.note,
                row.menu_item_id,
            )
        elif edit.swap_to is not None:
            line = _new_line(
                "swapped",
                menu,
                groups,
                edit.swap_to,
                quantity,
                edit.options,
                row.note,
                now,
            )
            if edit.swap_to == row.menu_item_id:
                # Nur die Auswahl getauscht: der Grundpreis bleibt eingefroren.
                line.unit_price_cents = row.unit_price_cents
        else:
            kind = "changed" if quantity != row.quantity else "kept"
            line = PlannedLine(
                kind,
                number,
                name,
                quantity,
                row.unit_price_cents,
                row.options,
                row.note,
                row.menu_item_id,
            )
        line.order_item_id = row.id
        planned_rows.append(line)

    planned_added = [
        _new_line(
            "added",
            menu,
            groups,
            a.menu_item_id,
            _quantity(a.quantity),
            a.options,
            None,
            now,
        )
        for a in req.added
    ]

    remaining = [
        line for line in planned_rows if line.kind != "removed"
    ] + planned_added
    items_total = sum(line.cents for line in remaining)
    changed = bool(planned_added) or any(line.kind != "kept" for line in planned_rows)

    blockers = []
    if not changed:
        blockers.append(NO_CHANGE)
    if not remaining:
        blockers.append(NO_POSITION)
    for line in remaining:
        if line.missing:
            blockers.append(
                MISSING_OPTION.format(
                    number=line.number, name=line.name, groups=", ".join(line.missing)
                )
            )
    return CorrectionPlan(
        order_id=order.id,
        version=order_version(rows),
        rows=planned_rows,
        added=planned_added,
        before_total_cents=order.items_total_cents,
        items_total_cents=items_total,
        total_cents=items_total + order.delivery_fee_cents,
        changed=changed,
        blockers=blockers,
        reasons=_reasons(order),
    )


def _quantity(value: int, allow_zero: bool = False) -> int:
    low = 0 if allow_zero else 1
    if not low <= value <= MAX_QUANTITY:
        raise InvalidInput(f"Menge {value} ausserhalb {low}..{MAX_QUANTITY}")
    return value


def _menu(
    session: Session, tenant_id: uuid.UUID, ids: set[uuid.UUID]
) -> dict[uuid.UUID, MenuItem]:
    if not ids:
        return {}
    menu = {
        m.id: m
        for m in session.scalars(
            select(MenuItem).where(
                MenuItem.tenant_id == tenant_id,
                MenuItem.id.in_(ids),
                MenuItem.active.is_(True),
            )
        )
    }
    if ids - menu.keys():
        raise InvalidInput("Gericht unbekannt oder nicht mehr auf der Karte")
    return menu


def _new_line(
    kind: str,
    menu: dict[uuid.UUID, MenuItem],
    groups: dict[uuid.UUID, list[OptionGroup]],
    item_id: uuid.UUID,
    quantity: int,
    choices: tuple[Choice, ...],
    note: str | None,
    now: datetime,
) -> PlannedLine:
    item = menu[item_id]
    offered = groups.get(item_id, [])
    options = _options(item, offered, choices)
    chosen = {option_key(o["group"]) for o in options}
    missing = [
        g.group for g in offered if g.required and option_key(g.group) not in chosen
    ]
    return PlannedLine(
        kind=kind,
        number=item.number,
        name=item.name,
        quantity=quantity,
        # Grundpreis ohne Optionen, wie draft_order ihn ablegt (pricing.row_cents).
        unit_price_cents=item.price_cents,
        options=options,
        note=note,
        menu_item_id=item.id,
        groups=offered,
        missing=missing,
        # Das Team darf ein Gericht nehmen, das heute aus ist - es sieht den Hinweis.
        sold_out=is_sold_out(item, now),
    )


def _options(
    item: MenuItem, offered: list[OptionGroup], choices: tuple[Choice, ...]
) -> list[dict]:
    """Gewaehlte Optionen in der Form von order_items.options, je Gruppe hoechstens eine."""
    by_group = {option_key(g.group): g for g in offered}
    picked: dict[str, dict] = {}
    for choice in choices:
        group = by_group.get(option_key(choice.group))
        opt = (
            next(
                (
                    o
                    for o in group.options
                    if option_key(o.name) == option_key(choice.name)
                ),
                None,
            )
            if group
            else None
        )
        if group is None or opt is None:
            raise InvalidInput(
                f"Option {choice.group}/{choice.name} gibt es an {item.number} nicht"
            )
        picked[option_key(group.group)] = {
            "group": group.group,
            "option": opt.name,
            "price_delta_cents": opt.price_delta_cents,
        }
    options = [picked[k] for k in by_group if k in picked]
    if row_cents(item.price_cents, options) < 0:
        raise InvalidInput(f"Preis von {item.number} mit Optionen negativ")
    return options


def _audit_line(row: OrderItem, label: list[str]) -> dict:
    """Nur Kartendaten: kein Hinweis, er kann eine Allergie tragen."""
    return {
        "number": label[0],
        "name": label[1],
        "quantity": row.quantity,
        "options": [o["option"] for o in row.options],
        "unit_price_cents": row.unit_price_cents,
    }
