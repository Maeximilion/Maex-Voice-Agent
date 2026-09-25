"""Darstellung der Spalte "Neue Bestellungen" und Zustand der Korrektur im Tablet (T-4.7).

Nur Auswahl und Darstellung (docs/11 §1): was eine Bestellung kostet und was
gespeichert werden darf, rechnet `domain/ordering/`. Hier entsteht die Karte in
Worten des Teams und der Bearbeitungsstand, den das Tablet zwischen zwei Taps
als JSON in einem versteckten Feld mitschickt.

Der Stand ist Eingabe, keine Wahrheit: Preise und Summen kommen bei jedem Tap
neu aus der Datenbank, und beim Speichern prueft die Domain alles noch einmal.
"""

import json
import uuid
from dataclasses import dataclass

from api.core.errors import InvalidInput
from api.core.time import to_local
from api.domain.ordering.board import BoardOrder
from api.domain.ordering.correction import (
    MAX_QUANTITY,
    REASONS,
    AddEdit,
    Choice,
    CorrectionPlan,
    CorrectionRequest,
    PlannedLine,
    RowEdit,
)

TYPE_LABELS = {"pickup": "Abholung", "delivery": "Lieferung"}
UNKNOWN_NUMBER = "Nummer {number} gibt es nicht auf der Karte."
BAD_STATE = "Die Korrektur ist durcheinander geraten. Bitte neu öffnen."


def euro(cents: int) -> str:
    """1380 -> "13,80 €". Nur Anzeige; gerechnet wird in Cent."""
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}{cents // 100},{cents % 100:02d} €"


def _clock(value, tz_name: str) -> str:
    return to_local(value, tz_name).strftime("%H:%M")


def card(order: BoardOrder, tz_name: str) -> dict:
    """Eine Karte der Spalte. Zustand immer mit Text, nie nur Farbe (docs/06 §1 Regel 4)."""
    if order.handover_state == "failed":
        tone, state = "danger", "Küche nicht erreicht"
    elif order.handover_state is None:
        tone, state = "warn", "Noch nicht in der Küche"
    else:
        tone, state = "ok", "Küche hat den Bon"
    return {
        "id": str(order.order_id),
        "time": _clock(order.created_at, tz_name),
        "type": TYPE_LABELS.get(order.type, order.type),
        "pickup_code": order.pickup_code,
        "name": order.customer_name,
        "phone": order.phone,
        "lines": order.lines,
        "total": euro(order.total_cents),
        "ready": _clock(order.ready_at, tz_name) if order.ready_at else None,
        "tone": tone,
        "state": state,
        "failed": order.handover_state == "failed",
        "awaiting": order.handover_state is None,
        # "Passt" nur, solange das Team noch nicht abgehakt hat und die Kueche
        # erreichbar war; eine rote Karte braucht "Nochmal senden".
        "can_approve": order.status == "confirmed" and order.handover_state != "failed",
        "corrected": REASONS.get(order.corrected) if order.corrected else None,
    }


# --- Bearbeitungsstand --------------------------------------------------------------


@dataclass
class EditState:
    version: str
    edit_id: str
    # Je bestehender Position: q Menge, s getauscht gegen, o Auswahl, d Hinweis streichen.
    rows: list[dict]
    added: list[dict]
    # Position, die gerade auf eine Nummer zum Tauschen wartet.
    swapping: int | None = None

    def dump(self) -> str:
        return json.dumps(
            {
                "v": self.version,
                "e": self.edit_id,
                "r": self.rows,
                "a": self.added,
                "t": self.swapping,
            },
            separators=(",", ":"),
        )

    def request(self) -> CorrectionRequest:
        return CorrectionRequest(
            version=self.version,
            edit_id=self.edit_id,
            rows=tuple(
                RowEdit(
                    order_item_id=uuid.UUID(r["id"]),
                    quantity=int(r["q"]),
                    swap_to=uuid.UUID(r["s"]) if r.get("s") else None,
                    options=_choices(r.get("o")),
                    drop_note=bool(r.get("d")),
                )
                for r in self.rows
            ),
            added=tuple(
                AddEdit(
                    menu_item_id=uuid.UUID(a["m"]),
                    quantity=int(a["q"]),
                    options=_choices(a.get("o")),
                )
                for a in self.added
            ),
        )


def _choices(raw) -> tuple[Choice, ...]:
    return tuple(Choice(str(g), str(n)) for g, n in (raw or []))


def fresh_state(plan: CorrectionPlan) -> EditState:
    return EditState(
        version=plan.version,
        edit_id=uuid.uuid4().hex,
        rows=[
            {"id": str(line.order_item_id), "q": line.quantity} for line in plan.rows
        ],
        added=[],
    )


def parse_state(raw: str) -> EditState:
    try:
        data = json.loads(raw)
        state = EditState(
            version=str(data["v"]),
            edit_id=str(data["e"]),
            rows=list(data["r"]),
            added=list(data["a"]),
            swapping=data.get("t"),
        )
        state.request()  # prueft Form und ids, wirft bei Unsinn
    except (ValueError, KeyError, TypeError) as exc:
        raise InvalidInput(f"Korrekturstand unlesbar: {exc}", say=BAD_STATE) from exc
    return state


def apply_op(
    state: EditState,
    op: str,
    plan: CorrectionPlan,
    number: str,
    find_item,
) -> str | None:
    """Einen Tap anwenden. Gibt eine Meldung fuer das Team zurueck oder None.

    `find_item(nummer)` liefert die id eines aktiven Gerichts oder None.
    """
    parts = op.split(":")
    kind = parts[0]
    try:
        if kind == "nummer":
            item_id = find_item(number)
            if item_id is None:
                return UNKNOWN_NUMBER.format(number=number.strip() or "?")
            if state.swapping is not None:
                row = state.rows[state.swapping]
                row["s"], row["o"] = str(item_id), []
                state.swapping = None
            else:
                state.added.append({"m": str(item_id), "q": 1, "o": []})
            return None
        if kind in ("plus", "minus"):
            target = _target(state, parts[1], int(parts[2]))
            step = 1 if kind == "plus" else -1
            target["q"] = max(0, min(MAX_QUANTITY, int(target["q"]) + step))
            if parts[1] == "a" and target["q"] == 0:
                state.added.remove(target)
            elif (
                parts[1] == "r"
                and target["q"] == 0
                and _line(plan, "r", int(parts[2])).note
            ):
                # Mit Hinweis geht es nur ueber "Entfernen" mit Rueckfrage.
                target["q"] = 1
            return None
        if kind == "weg":
            row = state.rows[int(parts[1])]
            row["q"], row["d"] = 0, True
            return None
        if kind == "zurueck":
            i = int(parts[1])
            row = state.rows[i]
            # Eine entfernte Zeile zeigt ihre Menge von vorher.
            row.update({"q": plan.rows[i].quantity, "d": False, "s": None, "o": []})
            return None
        if kind == "tauschen":
            state.swapping = int(parts[1])
            return None
        if kind == "nicht-tauschen":
            state.swapping = None
            return None
        if kind == "opt":
            target = _target(state, parts[1], int(parts[2]))
            line = _line(plan, parts[1], int(parts[2]))
            group = line.groups[int(parts[3])]
            option = group.options[int(parts[4])]
            chosen = [c for c in target.get("o") or [] if c[0] != group.group]
            already = [group.group, option.name] in (target.get("o") or [])
            if not already or group.required:
                chosen.append([group.group, option.name])
            target["o"] = chosen
            return None
    except (IndexError, ValueError, KeyError) as exc:
        raise InvalidInput(
            f"Tap {op!r} passt nicht zum Stand: {exc}", say=BAD_STATE
        ) from exc
    raise InvalidInput(f"Unbekannter Tap {op!r}", say=BAD_STATE)


def _target(state: EditState, where: str, index: int) -> dict:
    return state.rows[index] if where == "r" else state.added[index]


def _line(plan: CorrectionPlan, where: str, index: int) -> PlannedLine:
    return plan.rows[index] if where == "r" else plan.added[index]


def edit_lines(plan: CorrectionPlan, state: EditState) -> dict:
    """Werte fuer das Template der Korrektur."""

    def view(line: PlannedLine, where: str, index: int, chosen) -> dict:
        picked = {(g, n) for g, n in (chosen or [])}
        return {
            "where": where,
            "index": index,
            "kind": line.kind,
            "number": line.number,
            "name": line.name,
            "quantity": line.quantity,
            "options": [o["option"] for o in line.options],
            "note": line.note,
            "price": euro(line.cents) if line.kind != "removed" else None,
            "missing": line.missing,
            "sold_out": line.sold_out,
            "groups": [
                {
                    "name": g.group,
                    "required": g.required,
                    "options": [
                        {
                            "name": o.name,
                            "delta": euro(o.price_delta_cents)
                            if o.price_delta_cents
                            else None,
                            "picked": (g.group, o.name) in picked,
                        }
                        for o in g.options
                    ],
                }
                for g in line.groups
            ],
        }

    return {
        "rows": [
            view(line, "r", i, state.rows[i].get("o"))
            for i, line in enumerate(plan.rows)
        ],
        "added": [
            view(line, "a", j, state.added[j].get("o"))
            for j, line in enumerate(plan.added)
        ],
        "total": euro(plan.total_cents),
        "before": euro(plan.before_total_cents),
        "blockers": plan.blockers,
        "reasons": plan.reasons,
        "swapping": state.swapping,
        "state": state.dump(),
    }
