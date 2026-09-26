"""Ein Fall gegen den Datenbankzustand prüfen (docs/08 §1, §3).

Geprüft wird, was gebucht ist, nicht was das Modell sagt: ein Agent, der "ist
gebucht" sagt, ohne `confirm` aufzurufen, fällt durch. Deshalb liest `observe`
alles aus der Datenbank - Anruf, Bestellung, Reservierung, Rückruf - und
`judge` vergleicht nur Felder, die in `expected` stehen.

Unbekannte Schlüssel in `expected` sind ein Fehler im Fall, kein stilles Grün:
ein Tippfehler ("confimed") prüfte sonst nichts und sähe bestanden aus.
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from api.models import Call, Callback, MenuItem, Order, OrderItem, Reservation

CONFIRMED = ("confirmed", "approved", "handed_over")
EXPECTED_KEYS = frozenset(
    {"intent", "confirmed", "escalated", "items", "customer_name", "party_size"}
)
ITEM_KEYS = frozenset({"number", "quantity", "options"})


class CaseError(ValueError):
    """Der Fall selbst ist kaputt - der Lauf bricht ab, statt ihn zu werten."""


def validate_case(case: dict[str, Any], source: str) -> None:
    for key in ("id", "transcript", "expected"):
        if key not in case:
            raise CaseError(f"{source}: Feld '{key}' fehlt")
    unknown = set(case["expected"]) - EXPECTED_KEYS
    if unknown:
        raise CaseError(f"{source}: unbekannte Erwartung {sorted(unknown)}")
    if "now" in case:
        try:
            now = datetime.fromisoformat(case["now"])
        except (TypeError, ValueError) as exc:
            raise CaseError(
                f"{source}: now {case['now']!r} ist kein ISO-Zeitpunkt"
            ) from exc
        if now.tzinfo is None:
            raise CaseError(f"{source}: now braucht eine Zeitzone, z. B. +02:00")
    for item in case["expected"].get("items") or []:
        if set(item) - ITEM_KEYS or not {"number", "quantity"} <= set(item):
            raise CaseError(f"{source}: Position {item} braucht number und quantity")
    sold_out = case.get("sold_out", [])
    if not isinstance(sold_out, list) or not all(isinstance(n, str) for n in sold_out):
        raise CaseError(f"{source}: sold_out ist eine Liste von Kartennummern")
    pending = case.get("pending")
    if pending is not None and (not isinstance(pending, str) or not pending.strip()):
        # Eine bekannte Luecke ohne Grund waere ein stilles Rot (docs/08 §3).
        raise CaseError(f"{source}: pending braucht einen Grund mit Aufgabe")


@dataclass
class Observed:
    """Was nach dem Anruf in der Datenbank steht."""

    intent: str | None
    confirmed: bool
    escalated: bool
    items: list[dict[str, Any]]
    customer_name: str | None
    party_size: int | None
    # Bestätigte Vorgänge ohne einen confirm des Modells: an der Regel vorbei gebucht.
    confirmed_without_confirm: int = 0
    notes: list[str] = field(default_factory=list)


def observe(session: Session, call_id: uuid.UUID, confirms: int) -> Observed:
    session.expire_all()
    call = session.get(Call, call_id)
    orders = session.scalars(select(Order).where(Order.call_id == call_id)).all()
    reservations = session.scalars(
        select(Reservation).where(Reservation.call_id == call_id)
    ).all()
    callbacks = session.scalar(
        select(func.count(Callback.id)).where(Callback.call_id == call_id)
    )
    done_orders = [o for o in orders if o.status in CONFIRMED]
    done_res = [r for r in reservations if r.status in CONFIRMED]
    booked = len(done_orders) + len(done_res)

    items: list[dict[str, Any]] = []
    for order in done_orders:
        rows = session.execute(
            select(MenuItem.number, OrderItem.quantity, OrderItem.options)
            .join(MenuItem, MenuItem.id == OrderItem.menu_item_id)
            .where(OrderItem.order_id == order.id)
            .order_by(OrderItem.created_at)
        ).all()
        items += [
            {"number": n, "quantity": q, "options": [o["option"] for o in opts]}
            for n, q, opts in rows
        ]

    name = None
    if done_orders:
        name = done_orders[-1].customer_name
    elif done_res:
        name = done_res[-1].guest_name
    return Observed(
        intent=call.intent if call else None,
        confirmed=booked > 0,
        escalated=bool(callbacks) or bool(call and call.transfer_reason),
        items=items,
        customer_name=name,
        party_size=done_res[-1].party_size if done_res else None,
        confirmed_without_confirm=max(0, booked - confirms),
    )


def _matches(want: dict[str, Any], got: dict[str, Any]) -> bool:
    if str(want["number"]).lower() != str(got["number"]).lower():
        return False
    if int(want["quantity"]) != int(got["quantity"]):
        return False
    # Optionen nur, wo die Position sie nennt: "2x 23" legt die Auswahl nicht
    # fest, "47 mit Huhn" schon.
    return "options" not in want or sorted(want["options"]) == sorted(got["options"])


def _compare_items(want_items: list[dict], got_items: list[dict]) -> str | None:
    """Jede erwartete Position trifft genau eine gebuchte; Reihenfolge egal.

    Positionen mit Optionen zuerst: sonst nähme "47 ohne Angabe" die Zeile
    "47 mit Huhn" weg, die "47 mit Huhn" gebraucht hätte.
    """
    left = list(got_items)
    missing = []
    for want in sorted(want_items, key=lambda w: "options" not in w):
        hit = next((g for g in left if _matches(want, g)), None)
        if hit is None:
            missing.append(want)
        else:
            left.remove(hit)
    if not missing and not left:
        return None
    return f"items: fehlt {missing}, zu viel {left}"


def judge(expected: dict[str, Any], seen: Observed) -> list[str]:
    """Abweichungen in Worten; leer heisst bestanden. Nur Felder aus `expected`."""
    diffs: list[str] = []
    for key in ("intent", "confirmed", "escalated", "party_size"):
        if key in expected and expected[key] != getattr(seen, key):
            diffs.append(
                f"{key}: erwartet {expected[key]!r}, gebucht {getattr(seen, key)!r}"
            )
    if "customer_name" in expected:
        want, got = expected["customer_name"], seen.customer_name
        if (got or "").casefold() != str(want).casefold():
            diffs.append(f"customer_name: erwartet {want!r}, gebucht {got!r}")
    if "items" in expected:
        diff = _compare_items(expected["items"], seen.items)
        if diff:
            diffs.append(diff)
    return diffs
