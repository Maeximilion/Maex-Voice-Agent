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
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from api.domain.ordering.confirm import ACTION_CONFIRMED as ORDER_CONFIRMED_ACTION
from api.events.types import ORDER_CONFIRMED, RESERVATION_CONFIRMED
from api.models import (
    AuditLog,
    Call,
    Callback,
    MenuItem,
    Order,
    OrderItem,
    OutboxEvent,
    Reservation,
)

CONFIRMED = ("confirmed", "approved", "handed_over")
EXPECTED_KEYS = frozenset(
    {
        "intent",
        "confirmed",
        "escalated",
        "items",
        "customer_name",
        "party_size",
        # Tools, die das Modell aufgerufen haben muss: eine Allergiefrage
        # gilt nur mit get_item_details als beantwortet (Codex PR #145, P1).
        "tools",
        # Termine aus dem letzten check_slot, Ortszeit "YYYY-MM-DDTHH:MM": was der
        # Code anbietet, steht in keiner Tabelle, ist aber kein Wortlaut, sondern
        # ein Ergebnis (am Ruhetag nie der Vortag, Befund T-5.2).
        "alternatives",
    }
)
# `note` im festen Wortlaut: der Allergiehinweis an die Kueche darf nicht still
# wegfallen (E14, Codex PR #145, P1).
ITEM_KEYS = frozenset({"number", "quantity", "options", "note"})


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
    tools = case["expected"].get("tools", [])
    if not isinstance(tools, list) or not all(_valid_tool(t) for t in tools):
        raise CaseError(
            f"{source}: tools ist eine Liste aus Namen oder {{tool, number}}"
        )
    alternatives = case["expected"].get("alternatives", [])
    if not isinstance(alternatives, list) or not all(
        isinstance(a, str) for a in alternatives
    ):
        raise CaseError(f"{source}: alternatives ist eine Liste von Ortszeiten")
    if not isinstance(case.get("repeat_confirm", False), bool):
        raise CaseError(f"{source}: repeat_confirm ist true oder false")
    pending = case.get("pending")
    if pending is not None and (not isinstance(pending, str) or not pending.strip()):
        # Eine bekannte Luecke ohne Grund waere ein stilles Rot (docs/08 §3).
        raise CaseError(f"{source}: pending braucht einen Grund mit Aufgabe")


def _valid_tool(entry: Any) -> bool:
    if isinstance(entry, str):
        return True
    return (
        isinstance(entry, dict)
        and isinstance(entry.get("tool"), str)
        and set(entry) <= {"tool", "number"}
        and isinstance(entry.get("number", ""), str)
    )


def missing_tools(
    wanted: list[Any], ok_results: list[tuple[str, dict[str, Any]]]
) -> list[Any]:
    """Erwartete Tool-Aufrufe ohne erfolgreiches Ergebnis. Mit `number` nur,
    wenn das Ergebnis genau dieses Gericht betrifft: Allergene der 24 beantworten
    keine Frage nach der 23."""
    missing = []
    for entry in wanted:
        name = entry if isinstance(entry, str) else entry["tool"]
        number = None if isinstance(entry, str) else entry.get("number")
        hit = any(
            tool == name
            and (
                number is None or str(data.get("number", "")).lower() == number.lower()
            )
            for tool, data in ok_results
        )
        if not hit:
            missing.append(entry)
    return missing


def offered_alternatives(
    ok_results: list[tuple[str, dict[str, Any]]], zone: ZoneInfo
) -> list[str] | None:
    """Alternativen des letzten erfolgreichen check_slot als Ortszeit, None ohne Aufruf."""
    slots = [data for tool, data in ok_results if tool == "check_slot"]
    if not slots:
        return None
    return [
        datetime.fromisoformat(a).astimezone(zone).strftime("%Y-%m-%dT%H:%M")
        for a in slots[-1].get("alternatives", [])
    ]


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
    # Bestaetigungen ueber die erste hinaus je Vorgang (audit_log und Outbox):
    # ein wiederholter confirm darf keinen zweiten Vorgang und keinen zweiten
    # Bon ausloesen (docs/08 §6, Idempotenz).
    duplicate_confirms: int = 0
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
            select(
                MenuItem.number, OrderItem.quantity, OrderItem.options, OrderItem.note
            )
            .join(MenuItem, MenuItem.id == OrderItem.menu_item_id)
            .where(OrderItem.order_id == order.id)
            .order_by(OrderItem.created_at)
        ).all()
        items += [
            {
                "number": n,
                "quantity": q,
                "options": [o["option"] for o in opts],
                "note": note,
            }
            for n, q, opts, note in rows
        ]

    duplicates = _duplicate_confirms(session, orders, reservations)

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
        duplicate_confirms=duplicates,
    )


def _duplicate_confirms(session: Session, orders, reservations) -> int:
    """Wie oft ein Vorgang dieses Anrufs mehr als einmal bestaetigt wurde."""
    ids = [o.id for o in orders] + [r.id for r in reservations]
    if not ids:
        return 0
    audit = session.execute(
        select(func.count(AuditLog.id))
        .where(
            AuditLog.entity_id.in_(ids),
            AuditLog.action.in_((ORDER_CONFIRMED_ACTION, "reservation.confirmed")),
        )
        .group_by(AuditLog.entity_id)
    ).scalars()
    keys = [str(i) for i in ids]
    events = session.execute(
        select(func.count(OutboxEvent.id))
        .where(
            OutboxEvent.event_type.in_((ORDER_CONFIRMED, RESERVATION_CONFIRMED)),
            func.coalesce(
                OutboxEvent.payload["order_id"].astext,
                OutboxEvent.payload["reservation_id"].astext,
            ).in_(keys),
        )
        .group_by(
            func.coalesce(
                OutboxEvent.payload["order_id"].astext,
                OutboxEvent.payload["reservation_id"].astext,
            )
        )
    ).scalars()
    return sum(n - 1 for n in audit) + sum(n - 1 for n in events)


def _matches(want: dict[str, Any], got: dict[str, Any]) -> bool:
    if str(want["number"]).lower() != str(got["number"]).lower():
        return False
    if int(want["quantity"]) != int(got["quantity"]):
        return False
    # Optionen nur, wo die Position sie nennt: "2x 23" legt die Auswahl nicht
    # fest, "47 mit Huhn" schon.
    if "options" in want and sorted(want["options"]) != sorted(got["options"]):
        return False
    return "note" not in want or _same_text(want["note"], got.get("note"))


def _same_text(want: str | None, got: str | None) -> bool:
    """Wortlaut ohne Unterschied in Gross-/Kleinschreibung und Leerzeichen."""

    def norm(text: str | None) -> str:
        return " ".join((text or "").split()).casefold()

    return norm(want) == norm(got)


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
    # Immer, nicht nur wenn erwartet: ein Vorgang, zweimal bestaetigt, ist nie richtig.
    if seen.duplicate_confirms:
        diffs.append(f"doppelt bestaetigt: {seen.duplicate_confirms} zusaetzlich")
    return diffs
