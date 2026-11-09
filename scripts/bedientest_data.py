"""Invented data for the operating test (T-3.5), made inside the api container.

    python -m scripts.bedientest_data fill
    python -m scripts.bedientest_data arrive a|e|callback

Run by `scripts/bedientest_fixture.py` through `docker compose exec`; it needs the
project's dependencies, which the host script does not. Each prints one JSON object.
"""

import argparse
import json
import sys
import uuid
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from api.core.time import utcnow
from api.db import SessionLocal
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
    Capacity,
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

TENANT = "Bedientest"
WAIT_PICKUP, WAIT_DELIVERY = 25, 50  # protocol section 0, "Reset before every tester"
APPROVAL_ABOVE_CENTS = 100_000  # no invented order waits for approval
SLOT_MINUTES = 30
RESERVATION_AHEAD = timedelta(minutes=90)

# Dish numbers the protocol refers to. 23 is on no card (task 6), 47 is the replacement
# dish of task 3 (one required group, choice "Nudeln"), 52 is order D (task 12: the
# sauce "Erdnuss" becomes "Kokos"), 15 is order C (a required choice and a note).
MENU = {
    MENU_FILE: (
        "number;name;category;price_eur;description;active\n"
        "7;Wan Tan;Vorspeisen;6,50;;ja\n"
        "8;Bao Buns;Vorspeisen;7,50;;ja\n"
        "11;Pho Bo;Suppen;11,90;;ja\n"
        "13;Misosuppe;Suppen;5,50;;ja\n"
        "15;Gebratener Reis;Hauptgerichte;10,50;;ja\n"
        "23;Frühlingsrollen;Vorspeisen;6,90;;ja\n"
        "47;Ente knusprig;Hauptgerichte;15,50;;ja\n"
        "52;Rotes Curry;Hauptgerichte;12,90;;ja\n"
    ),
    OPTIONS_FILE: (
        "number;group_name;option_name;price_delta_eur;is_default;required\n"
        "15;Fleisch;Huhn;0,00;ja;ja\n"
        "15;Fleisch;Tofu;0,00;nein;ja\n"
        "47;Beilage;Reis;0,00;ja;ja\n"
        "47;Beilage;Nudeln;0,00;nein;ja\n"
        "52;Sauce;Erdnuss;0,00;ja;ja\n"
        "52;Sauce;Kokos;0,00;nein;ja\n"
    ),
    ALLERGENS_FILE: "number;allergen_codes;confirmed_by\n",
    ALIASES_FILE: "number;alias\n",
}


# --- inside the api container: data ---------------------------------------------------


def _tenant_id(session: Session) -> uuid.UUID:
    tenant = session.scalar(select(Tenant).where(Tenant.name == TENANT))
    if tenant is None:
        raise SystemExit(f"no tenant {TENANT!r}: run `up` first")
    return tenant.id


def _item(session: Session, tenant_id: uuid.UUID, number: str) -> uuid.UUID:
    return session.scalars(
        select(MenuItem.id).where(
            MenuItem.tenant_id == tenant_id, MenuItem.number == number
        )
    ).one()


def _call(session: Session, tenant_id: uuid.UUID, now: datetime) -> uuid.UUID:
    call = Call(
        tenant_id=tenant_id,
        external_session_id=uuid.uuid4().hex,
        started_at=now,
        delete_after=now.date() + timedelta(days=30),
    )
    session.add(call)
    session.commit()
    return call.id


def _order(
    session: Session,
    tenant_id: uuid.UUID,
    who: str,
    phone: str,
    lines: list[dict],
) -> tuple[uuid.UUID, str]:
    """A confirmed pickup, made by the same two domain steps a call ends with."""
    now = utcnow()
    call_id = _call(session, tenant_id, now)
    draft = draft_order(
        session,
        DraftOrderRequest(
            call_id=call_id,
            tenant_id=tenant_id,
            idempotency_key=uuid.uuid4().hex,
            type="pickup",
            customer={"name": who, "phone": phone},
            items=[
                {
                    "menu_item_id": _item(session, tenant_id, line["number"]),
                    "quantity": line.get("quantity", 1),
                    "options": line.get("options", []),
                    "note": line.get("note"),
                }
                for line in lines
            ],
        ),
    )
    done = confirm(
        session,
        ConfirmRequest(
            call_id=call_id,
            tenant_id=tenant_id,
            entity="order",
            entity_id=draft.order_id,
            idempotency_key=uuid.uuid4().hex,
        ),
    )
    return draft.order_id, done.pickup_code or ""


def _callback(session: Session, tenant_id: uuid.UUID, summary: str) -> uuid.UUID:
    task = create_callback(
        session,
        CreateCallbackRequest(
            call_id=_call(session, tenant_id, utcnow()),
            tenant_id=tenant_id,
            phone="+4972155501999",
            reason="complaint",
            summary=summary,
        ),
    )
    return task.callback_id


def fill(session: Session) -> dict:
    """The start state of section 0. Refuses a stack that already holds orders, because
    `reset` (a fresh volume) is how the protocol rebuilds, never a patch on top."""
    result = seed(session, tenant_name=TENANT, timezone="Europe/Berlin")
    tenant_id = uuid.UUID(result.tenant_id)
    if session.scalar(select(Order.id).where(Order.tenant_id == tenant_id).limit(1)):
        raise SystemExit("the stack already holds orders: run `reset`")

    # Open every day around the clock, so the run never meets a closed hour (protocol
    # "Open day"); hours and capacity still come from the database, as in operation.
    for model in (OpeningHours, Capacity):
        session.query(model).filter(model.tenant_id == tenant_id).delete()
    session.add_all(
        [
            OpeningHours(
                tenant_id=tenant_id,
                weekday=d,
                opens_at=time(0, 0),
                closes_at=time(23, 59),
                service=s,
            )
            for d in range(7)
            for s in SERVICES
        ]
        + [
            Capacity(
                tenant_id=tenant_id,
                weekday=d,
                slot_start=time(0, 0),
                slot_end=time(23, 59),
                max_guests=200,
                slot_minutes=SLOT_MINUTES,
            )
            for d in range(7)
        ]
    )
    session.execute(
        update(ServiceConfig)
        .where(ServiceConfig.tenant_id == tenant_id)
        .values(
            call_mode="primary",
            delivery_enabled=True,
            pickup_wait_standard_minutes=WAIT_PICKUP,
            delivery_wait_standard_minutes=WAIT_DELIVERY,
            pickup_wait_today_minutes=None,
            pickup_wait_today_until=None,
            delivery_wait_today_minutes=None,
            delivery_wait_today_until=None,
            order_approval_above_cents=APPROVAL_ABOVE_CENTS,
            reservation_max_party_size=1000,
        )
    )
    session.commit()

    plan = parse(MENU)
    if not plan.ok:
        raise SystemExit(f"menu does not parse: {plan.errors}")
    apply(session, tenant_id, plan)

    a = "+4972155501"
    _, b = _order(session, tenant_id, "Testgast B", a + "02", [{"number": "11"}])
    _, c = _order(
        session,
        tenant_id,
        "Testgast C",
        a + "03",
        [
            {
                "number": "15",
                "options": [{"group": "Fleisch", "name": "Huhn"}],
                "note": "ohne Zwiebeln",
            }
        ],
    )
    _, d = _order(
        session,
        tenant_id,
        "Testgast D",
        a + "04",
        [{"number": "52", "options": [{"group": "Sauce", "name": "Erdnuss"}]}],
    )
    red_id, red = _order(session, tenant_id, "Testgast R", a + "05", [{"number": "7"}])
    # The only red card: the slip "not printed" (the dispatcher is not running, so the
    # sweep that would turn every unclaimed slip red never fires).
    session.execute(
        update(Order).where(Order.id == red_id).values(handover_state="failed")
    )
    session.commit()

    callback_id = _callback(
        session, tenant_id, "Invented complaint: the soup arrived cold."
    )

    zone = ZoneInfo("Europe/Berlin")
    at = utcnow() + RESERVATION_AHEAD
    at = (at + timedelta(minutes=-at.minute % SLOT_MINUTES)).replace(
        second=0, microsecond=0
    )
    if at.astimezone(zone).date() != utcnow().astimezone(zone).date():
        raise SystemExit(
            "too late in the day: the reservation would not fall on today's board"
        )
    draft = create_tablet_reservation(
        session,
        TabletReservationRequest(
            tenant_id=tenant_id,
            idempotency_key=uuid.uuid4().hex,
            guest_name="Testgast Reservierung",
            party_size=4,
            reserved_for=at,
        ),
    )
    confirm_tablet(session, tenant_id, draft.reservation_id)

    return {
        "pickup_codes": {"B": b, "C": c, "D": d, "red": red},
        "dishes": {
            "task 3 replacement": "47 with choice Nudeln",
            "task 12": "52, Erdnuss -> Kokos",
            "task 6 (sold out)": "23",
            "arrival E dish": "13",
        },
        "callback_id": str(callback_id),
        "reservation_at_local": at.astimezone(zone).strftime("%H:%M"),
        "restore_values": {
            "pickup_wait": WAIT_PICKUP,
            "delivery_wait": WAIT_DELIVERY,
            "order_approval_above_cents": APPROVAL_ABOVE_CENTS,
            "order_approval_overdue_minutes": session.get(
                ServiceConfig, tenant_id
            ).order_approval_overdue_minutes,
        },
    }


def arrive(session: Session, what: str) -> dict:
    """The live arrivals of protocol section 2a, made while the tester works."""
    tenant_id = _tenant_id(session)
    if what == "callback":
        return {
            "callback_id": str(
                _callback(session, tenant_id, "Invented complaint: wrong dish.")
            )
        }
    number = {"a": "8", "e": "13"}[what]  # E is a dish other than 23
    _, code = _order(
        session,
        tenant_id,
        f"Testgast {what.upper()}",
        "+4972155501" + "06",
        [{"number": number}],
    )
    return {"pickup_code": code}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("command", choices=["fill", "arrive"])
    parser.add_argument("what", nargs="?", choices=["a", "e", "callback"])
    args = parser.parse_args(argv)
    if args.command == "arrive" and args.what is None:
        parser.error("arrive needs a, e or callback")
    with SessionLocal() as session:
        out = fill(session) if args.command == "fill" else arrive(session, args.what)
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
