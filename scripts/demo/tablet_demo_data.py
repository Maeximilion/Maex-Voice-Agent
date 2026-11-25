"""The invented restaurant of the public tablet demo (#892, task B1).

Seeded by `scripts/demo/tablet_demo_build.py` into a scratch database of its own,
never into a database that holds anything else (`fill` refuses one with a
tenant). Everything here is made up: five categories with five dishes each,
rooms, orders, callbacks, reservations and a week of calls, so the board,
"Kennzahlen" and "Anrufe" have something to show. Nothing is copied from the
pilot or the workbench.

Every phone number comes from the Bundesnetzagentur's numbers for media
productions ("drama numbers", Berlin 030 23125 000 to 999): they are never given
to a subscriber, so no number on the public page rings anyone.
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from api.domain.callbacks.create import create_callback
from api.domain.confirm import confirm, confirm_tablet
from api.domain.menu.importer import (
    ALIASES_FILE,
    ALLERGENS_FILE,
    MENU_FILE,
    OPTIONS_FILE,
    apply,
    parse,
)
from api.domain.ordering import draft_order
from api.domain.reservations.manual import create_tablet_reservation
from api.models import (
    Call,
    Callback,
    Capacity,
    DeliveryZone,
    MenuItem,
    OpeningHours,
    Order,
    ServiceConfig,
    Tenant,
)
from api.models.tenants import SERVICES
from api.schemas.callbacks import CreateCallbackRequest
from api.schemas.confirm import ConfirmRequest
from api.schemas.orders import DraftOrderRequest
from api.schemas.reservations import TabletReservationRequest
from scripts.seed import seed

TENANT = "Beispiel-Restaurant"
TIMEZONE = "Europe/Berlin"
ZONE = ZoneInfo(TIMEZONE)
# Bundesnetzagentur, "Rufnummern für Medienproduktionen": +49 30 23125 000-999.
EXAMPLE_PREFIX = "+493023125"
SLOT_MINUTES = 30
MAX_GUESTS = 60
WAIT_PICKUP, WAIT_DELIVERY = 20, 45

# Five categories, five dishes each (Maxi, 2026-11-19). Invented names and prices.
DISHES = {
    "Vorspeisen": (
        ("101", "Bruschetta", "6,50"),
        ("102", "Tomatensuppe", "5,90"),
        ("103", "Gemischter Salat", "7,50"),
        ("104", "Knoblauchbrot", "4,50"),
        ("105", "Antipasti-Teller", "9,80"),
    ),
    "Pizza": (
        ("201", "Pizza Margherita", "8,90"),
        ("202", "Pizza Salami", "9,90"),
        ("203", "Pizza Funghi", "9,50"),
        ("204", "Pizza Vier Käse", "10,90"),
        ("205", "Pizza Gemüse", "10,50"),
    ),
    "Pasta": (
        ("301", "Spaghetti Tomate", "9,50"),
        ("302", "Penne Arrabbiata", "9,90"),
        ("303", "Tagliatelle mit Pilzen", "11,50"),
        ("304", "Lasagne", "11,90"),
        ("305", "Gnocchi in Salbeibutter", "10,90"),
    ),
    "Hauptgerichte": (
        ("401", "Schnitzel", "14,90"),
        ("402", "Lachsfilet", "16,90"),
        ("403", "Gemüsecurry", "12,90"),
        ("404", "Hähnchenbrust", "13,90"),
        ("405", "Rinderbraten", "17,50"),
    ),
    "Desserts": (
        ("501", "Tiramisu", "5,90"),
        ("502", "Panna Cotta", "5,50"),
        ("503", "Eis, drei Kugeln", "4,90"),
        ("504", "Apfelstrudel", "5,90"),
        ("505", "Schokoladenkuchen", "5,50"),
    ),
}
OPTIONS = (
    "201;Größe;Normal;0,00;ja;ja\n"
    "201;Größe;Groß;2,50;nein;ja\n"
    "401;Beilage;Pommes;0,00;ja;ja\n"
    "401;Beilage;Kartoffelsalat;0,00;nein;ja\n"
)
# The dish marked "aus" on the board.
SOLD_OUT = "204"

# A week of calls for "Kennzahlen" and "Anrufe": per day before today, how many
# calls of each outcome. Invented, and labelled as example values on the page (B3).
WEEK = (
    {"completed": 14, "transferred": 2, "callback": 1, "abandoned": 1},
    {"completed": 11, "transferred": 1, "callback": 1},
    {"completed": 17, "transferred": 3, "abandoned": 2},
    {"completed": 9, "callback": 2},
    {"completed": 15, "transferred": 1, "abandoned": 1},
    {"completed": 19, "transferred": 2, "callback": 1, "error": 1},
)
INTENT_OF = {
    "completed": ("pickup", "reservation", "delivery", "info"),
    "transferred": ("complaint", "unknown"),
    "callback": ("complaint", "info"),
    "abandoned": ("unknown",),
    "error": ("unknown",),
}


def example_number(n: int) -> str:
    """The n-th drama number, 0 to 999."""
    if not 0 <= n <= 999:
        raise ValueError("the example range has 1000 numbers")
    return f"{EXAMPLE_PREFIX}{n:03d}"


@dataclass
class Seeded:
    """What the build needs to know about the data it renders."""

    tenant_id: uuid.UUID
    orders: dict[str, uuid.UUID] = field(default_factory=dict)
    callbacks: dict[str, uuid.UUID] = field(default_factory=dict)
    calls: list[uuid.UUID] = field(default_factory=list)
    zones: list[uuid.UUID] = field(default_factory=list)


def _menu() -> dict[str, str]:
    rows = "".join(
        f"{number};{name};{category};{price};;ja\n"
        for category, dishes in DISHES.items()
        for number, name, price in dishes
    )
    return {
        MENU_FILE: "number;name;category;price_eur;description;active\n" + rows,
        OPTIONS_FILE: "number;group_name;option_name;price_delta_eur;is_default;"
        "required\n" + OPTIONS,
        ALLERGENS_FILE: "number;allergen_codes;confirmed_by\n",
        ALIASES_FILE: "number;alias\n",
    }


def _open_every_day(session: Session, tenant_id: uuid.UUID) -> None:
    """Open from 11:00 to 23:00 on every day, so the board never shows a closed
    restaurant while a visitor clicks through it."""
    for model in (OpeningHours, Capacity):
        session.query(model).filter(model.tenant_id == tenant_id).delete()
    session.add_all(
        [
            OpeningHours(
                tenant_id=tenant_id,
                weekday=d,
                opens_at=time(11, 0),
                closes_at=time(23, 0),
                service=s,
            )
            for d in range(7)
            for s in SERVICES
        ]
        + [
            Capacity(
                tenant_id=tenant_id,
                weekday=d,
                slot_start=time(11, 0),
                slot_end=time(23, 0),
                max_guests=MAX_GUESTS,
                slot_minutes=SLOT_MINUTES,
            )
            for d in range(7)
        ]
    )


def _call(
    session: Session,
    tenant_id: uuid.UUID,
    at: datetime,
    n: int,
    *,
    outcome: str = "completed",
    intent: str = "pickup",
) -> uuid.UUID:
    seconds = 40 + (n * 37) % 150
    call = Call(
        tenant_id=tenant_id,
        external_session_id=f"demo-{n}",
        caller_id=example_number(100 + n % 900),
        started_at=at,
        ended_at=at + timedelta(seconds=seconds),
        duration_seconds=seconds,
        intent=intent,
        outcome=outcome,
        transfer_reason="complaint" if outcome == "transferred" else None,
        cost_cents=4 + n % 5,
        delete_after=at.date() + timedelta(days=30),
    )
    session.add(call)
    session.flush()
    return call.id


def _item(session: Session, tenant_id: uuid.UUID, number: str) -> uuid.UUID:
    return session.scalars(
        select(MenuItem.id).where(
            MenuItem.tenant_id == tenant_id, MenuItem.number == number
        )
    ).one()


def _order(
    session: Session,
    tenant_id: uuid.UUID,
    now: datetime,
    n: int,
    who: str,
    lines: list[dict[str, Any]],
) -> uuid.UUID:
    """A confirmed pickup, made by the same two domain steps a call ends with."""
    call_id = _call(session, tenant_id, now - timedelta(minutes=2), n)
    session.commit()
    draft = draft_order(
        session,
        DraftOrderRequest.model_validate(
            {
                "call_id": call_id,
                "tenant_id": tenant_id,
                "idempotency_key": f"demo-order-{n}",
                "type": "pickup",
                "customer": {"name": who, "phone": example_number(n)},
                "items": [
                    {
                        "menu_item_id": _item(session, tenant_id, line["number"]),
                        "quantity": line.get("quantity", 1),
                        "options": line.get("options", []),
                        "note": line.get("note"),
                    }
                    for line in lines
                ],
            }
        ),
        now=now,
    )
    confirm(
        session,
        ConfirmRequest(
            call_id=call_id,
            tenant_id=tenant_id,
            entity="order",
            entity_id=draft.order_id,
            idempotency_key=f"demo-confirm-{n}",
        ),
        now=now,
    )
    # The database stamps the order with its own clock; the board and the
    # figures read that stamp, so it follows the demo's clock instead.
    session.execute(
        update(Order).where(Order.id == draft.order_id).values(created_at=now)
    )
    session.commit()
    return draft.order_id


def _callback(
    session: Session, tenant_id: uuid.UUID, now: datetime, n: int, reason: str, say: str
) -> uuid.UUID:
    call_id = _call(session, tenant_id, now, n, outcome="callback", intent="complaint")
    session.commit()
    task = create_callback(
        session,
        CreateCallbackRequest(
            call_id=call_id,
            tenant_id=tenant_id,
            phone=example_number(n),
            reason=reason,  # type: ignore[arg-type]
            summary=say,
        ),
    )
    # Stamped by the database's clock, shown as "Annahme": the demo's instead.
    session.execute(
        update(Callback)
        .where(Callback.id == task.callback_id)
        .values(created_at=now - timedelta(minutes=n))
    )
    session.commit()
    return task.callback_id


def _week_of_calls(session: Session, seeded: Seeded, now: datetime, first: int) -> None:
    """The six days before today, with orders behind the completed pickups, so
    the figures of the week count orders too."""
    n = first
    for days_back, day in enumerate(WEEK, start=1):
        noon = (
            (now - timedelta(days=days_back))
            .astimezone(ZONE)
            .replace(hour=12, minute=0, second=0, microsecond=0)
        )
        index = 0
        for outcome, count in day.items():
            for _ in range(count):
                intents = INTENT_OF[outcome]
                at = noon + timedelta(minutes=23 * index)
                seeded.calls.append(
                    _call(
                        session,
                        seeded.tenant_id,
                        at,
                        n,
                        outcome=outcome,
                        intent=intents[index % len(intents)],
                    )
                )
                index += 1
                n += 1
        session.commit()
        for dish in ("202", "304"):
            order = _order(
                session, seeded.tenant_id, noon, n, "Gast", [{"number": dish}]
            )
            # Done that day: otherwise it stands on today's board as an order
            # never typed into the register.
            session.execute(
                update(Order)
                .where(Order.id == order)
                .values(
                    status="handed_over",
                    handover_state="sent",
                    register_entered_at=noon,
                )
            )
            session.commit()
            n += 1


def fill(session: Session, now: datetime) -> Seeded:
    """The demo restaurant, seen at `now`. Refuses a database that holds a tenant:
    the demo is seeded into a scratch database of its own, never next to data."""
    if session.scalar(select(Tenant.id).limit(1)) is not None:
        raise SystemExit("the database already holds a restaurant: use a scratch one")
    tenant_id = uuid.UUID(
        seed(session, tenant_name=TENANT, timezone=TIMEZONE).tenant_id
    )
    seeded = Seeded(tenant_id)
    _open_every_day(session, tenant_id)
    session.execute(
        update(ServiceConfig)
        .where(ServiceConfig.tenant_id == tenant_id)
        .values(
            call_mode="primary",
            pickup_enabled=True,
            delivery_enabled=True,
            reservations_enabled=True,
            team_phone=example_number(900),
            privacy_notice_address=None,
            pickup_wait_standard_minutes=WAIT_PICKUP,
            delivery_wait_standard_minutes=WAIT_DELIVERY,
            pickup_wait_today_minutes=None,
            pickup_wait_today_until=None,
            delivery_wait_today_minutes=None,
            delivery_wait_today_until=None,
            reservation_max_party_size=12,
        )
    )
    session.add_all(
        [
            DeliveryZone(
                tenant_id=tenant_id,
                name=name,
                match_type="postal_code",
                postal_codes=[code],
                fee_cents=fee,
                min_order_cents=minimum,
                eta_minutes=eta,
            )
            for name, code, fee, minimum, eta in (
                ("Innenstadt", "12345", 200, 1500, 30),
                ("Nordviertel", "12346", 350, 2000, 45),
            )
        ]
    )
    session.commit()
    seeded.zones = list(
        session.scalars(
            select(DeliveryZone.id).where(DeliveryZone.tenant_id == tenant_id)
        )
    )

    plan = parse(_menu())
    if not plan.ok:
        raise SystemExit(f"demo menu does not parse: {plan.errors}")
    apply(session, tenant_id, plan)

    orders: tuple[tuple[str, str, list[dict[str, Any]]], ...] = (
        (
            "neu",
            "Frau Berger",
            [
                {
                    "number": "201",
                    "quantity": 2,
                    "options": [{"group": "Größe", "name": "Groß"}],
                },
                {"number": "501"},
            ],
        ),
        (
            "auswahl",
            "Herr Yilmaz",
            [
                {
                    "number": "401",
                    "options": [{"group": "Beilage", "name": "Kartoffelsalat"}],
                    "note": "ohne Zitrone",
                },
                {"number": "103"},
            ],
        ),
        ("kasse", "Familie Novak", [{"number": "304"}, {"number": "302"}]),
        ("rot", "Herr Schulte", [{"number": "203"}]),
    )
    for n, (key, who, lines) in enumerate(orders, start=1):
        seeded.orders[key] = _order(
            session, tenant_id, now - timedelta(minutes=40 - 9 * n), n, who, lines
        )
    # The one red card: a slip that did not print.
    session.execute(
        update(Order)
        .where(Order.id == seeded.orders["rot"])
        .values(handover_state="failed")
    )
    session.commit()

    seeded.callbacks["offen"] = _callback(
        session, tenant_id, now, 20, "complaint", "Die Pizza kam kalt an."
    )
    seeded.callbacks["rueckfrage"] = _callback(
        session,
        tenant_id,
        now,
        21,
        "human_requested",
        "Möchte eine Feier für zwanzig Personen besprechen.",
    )
    seeded.callbacks["erledigt"] = _callback(
        session, tenant_id, now, 22, "not_understood", "Frage nach glutenfreier Pasta."
    )

    for n, (who, size, hours, note) in enumerate(
        (
            ("Familie Krüger", 4, 2, None),
            ("Herr Demir", 2, 3, "Fensterplatz, wenn möglich"),
        ),
        start=30,
    ):
        at = now + timedelta(hours=hours)
        at = (at + timedelta(minutes=-at.minute % SLOT_MINUTES)).replace(
            second=0, microsecond=0
        )
        draft = create_tablet_reservation(
            session,
            TabletReservationRequest(
                tenant_id=tenant_id,
                idempotency_key=f"demo-reservation-{n}",
                guest_name=who,
                phone=example_number(n),
                party_size=size,
                reserved_for=at,
                note=note,
            ),
            now=now,
        )
        confirm_tablet(session, tenant_id, draft.reservation_id, now=now)

    _week_of_calls(session, seeded, now, first=100)
    return seeded
