"""Prüfungen vor der Summe: offen, aktiv, nicht aus, Optionen gültig, Pflichtgruppen gewählt.

Im Code, nicht im Modell (docs/04 §draft_order). Eine fehlende Pflichtwahl wird
nicht mit der Voreinstellung gefüllt, sondern erfragt - die Voreinstellung wäre
geraten (CLAUDE.md §2 Regel 2). Jeder Verstoß kommt mit einem `say`, das der
Agent vorlesen kann.
"""

import uuid
from collections import defaultdict
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.core.errors import (
    SAY_TO_THE_TEAM,
    BelowMinimum,
    Closed,
    Conflict,
    InvalidInput,
    NotFound,
    ServiceUnavailable,
)
from api.domain.delivery.zones import told_out_of_zone
from api.domain.menu.items import is_sold_out, option_key
from api.domain.menu.search import SAY_SOLD_OUT
from api.domain.ordering.pricing import Line
from api.domain.ordering.readback import spoken_euro
from api.domain.status.mode import PAUSED
from api.domain.status.service import DELIVERY, pickup_open, service_open_at
from api.models import ItemOption, MenuItem, ServiceConfig, Tenant
from api.schemas.delivery import DeliveryCheck
from api.schemas.orders import OrderItemIn

SAY_PICKUP_CLOSED = "Abholung ist gerade leider nicht möglich."
SAY_DELIVERY_CLOSED = "Lieferung ist gerade leider nicht möglich."
SAY_OFFER_PICKUP = "Möchten Sie abholen?"
SAY_OFFER_DELIVERY = "Möchten Sie liefern lassen?"
SAY_BELOW_MINIMUM = (
    "Bis zum Mindestbestellwert fehlen noch {missing}. "
    "Möchten Sie noch etwas dazunehmen?"
)
SAY_ITEM_UNKNOWN = "Ein Gericht habe ich nicht auf der Karte gefunden. Können Sie mir die Nummer sagen?"
SAY_OPTION_UNKNOWN = "{option} gibt es zu {name} nicht."
SAY_OPTION_MISSING = "Welche Auswahl bei {group} möchten Sie zu {name}?"
SAY_OPTION_REPEATED = "{option} zu {name} habe ich schon. Einmal {option}, richtig?"
SAY_MENU_BROKEN = f"Das kann ich gerade nicht sicher aufnehmen. {SAY_TO_THE_TEAM}"
SAY_OPTION_TWICE = "Bei {group} zu {name} geht nur eine Auswahl. Welche möchten Sie?"

# Gruppe -> Option -> Zeile, beide Schlüssel über option_key() vereinheitlicht.
Offered = dict[str, dict[str, ItemOption]]


def require_pickup_open(
    session: Session,
    tenant: Tenant,
    config: ServiceConfig,
    now: datetime,
    call_id: uuid.UUID,
) -> None:
    """The hours of the pickup service and the team's switch. `config` has to be
    fresh, as for `require_delivery_open`."""
    zone = ZoneInfo(tenant.timezone)
    if not pickup_open(session, tenant.id, config, now, zone):
        raise Closed(
            "Abholung gerade geschlossen",
            say=pickup_closed_say(session, tenant.id, config, now, zone, call_id),
        )


def pickup_closed_say(
    session: Session,
    tenant_id: uuid.UUID,
    config: ServiceConfig,
    now: datetime,
    zone: ZoneInfo,
    call_id: uuid.UUID,
) -> str:
    """Delivery only where `require_delivery_open` would take it, and not to a
    call just told its address is outside every zone (#524)."""
    if (
        config.delivery_enabled
        and config.call_mode != PAUSED
        and service_open_at(session, tenant_id, now, zone, DELIVERY)
        and not told_out_of_zone(session, tenant_id, call_id)
    ):
        return f"{SAY_PICKUP_CLOSED} {SAY_OFFER_DELIVERY}"
    return SAY_PICKUP_CLOSED


def require_delivery_open(
    session: Session, tenant: Tenant, config: ServiceConfig, now: datetime
) -> None:
    """The hours of the delivery service, the team's switch, and the emergency
    stop, which a call already running outlives (docs/02 §4). Every other mode is
    decided by `confirm`, which holds such an order for "Passt". `config` has to
    be fresh: the session of a call outlives a tap on the tablet."""
    zone = ZoneInfo(tenant.timezone)
    if (
        not config.delivery_enabled
        or config.call_mode == PAUSED
        or not service_open_at(session, tenant.id, now, zone, DELIVERY)
    ):
        raise Closed(
            "delivery closed right now",
            say=delivery_closed_say(session, tenant.id, config, now, zone),
        )


def delivery_closed_say(
    session: Session,
    tenant_id: uuid.UUID,
    config: ServiceConfig,
    now: datetime,
    zone: ZoneInfo,
) -> str:
    """Pickup only where it is open and switched on: availability comes from the
    database."""
    if pickup_open(session, tenant_id, config, now, zone):
        return f"{SAY_DELIVERY_CLOSED} {SAY_OFFER_PICKUP}"
    return SAY_DELIVERY_CLOSED


def require_minimum(items_cents: int, zone: DeliveryCheck) -> None:
    """The dishes count, the fee does not."""
    missing = zone.min_order_cents - items_cents
    if missing > 0:
        raise BelowMinimum(
            f"{missing} cents below the minimum order value",
            say=SAY_BELOW_MINIMUM.format(missing=spoken_euro(missing)),
        )


def validated_lines(
    session: Session, tenant_id: uuid.UUID, items: list[OrderItemIn], now: datetime
) -> list[Line]:
    """Die Positionen in der gesprochenen Reihenfolge, Preise aus der Karte."""
    ids = {i.menu_item_id for i in items}
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
        raise NotFound("Gericht unbekannt oder inaktiv", say=SAY_ITEM_UNKNOWN)

    offered: dict[uuid.UUID, Offered] = defaultdict(lambda: defaultdict(dict))
    for opt in session.scalars(
        select(ItemOption).where(ItemOption.menu_item_id.in_(ids))
    ):
        group = offered[opt.menu_item_id][option_key(opt.group_name)]
        name = option_key(opt.option_name)
        first = next(iter(group.values()), None)
        if first is not None and (
            first.group_name != opt.group_name or first.required != opt.required
        ):
            # Dieselbe Gruppe in zwei Schreibweisen ("Sauce", "sauce") oder mit
            # uneinheitlichem required: ob sie Pflicht ist, entschiede sonst die
            # zufällig erste Zeile. Der Import lehnt beides ab; Altdaten gehen
            # ans Team (Codex PR #124).
            raise ServiceUnavailable(
                f"Gruppe {first.group_name}/{opt.group_name} an Gericht "
                f"{opt.menu_item_id} uneinheitlich",
                say=SAY_MENU_BROKEN,
            )
        if name in group:
            # Zwei Zeilen, die sich nur in der Schreibweise unterscheiden: welche
            # gemeint ist, und damit der Preis, wäre geraten. Der Import lehnt das
            # ab; Altdaten fängt diese Stelle (Codex PR #124).
            raise ServiceUnavailable(
                f"Option {opt.group_name}/{opt.option_name} an Gericht "
                f"{opt.menu_item_id} doppelt in der Karte",
                say=SAY_MENU_BROKEN,
            )
        group[name] = opt

    lines = []
    for wanted in items:
        item = menu[wanted.menu_item_id]
        if is_sold_out(item, now):
            raise Conflict(
                "Gericht ausverkauft", say=SAY_SOLD_OUT.format(name=item.name)
            )
        lines.append(
            Line(
                item=item,
                quantity=wanted.quantity,
                options=_options(item, wanted, offered[item.id]),
                note=(wanted.note or "").strip() or None,
            )
        )
    return lines


def _options(
    item: MenuItem, wanted: OrderItemIn, offered: Offered
) -> list[dict[str, Any]]:
    chosen: list[ItemOption] = []
    for choice in wanted.options:
        opt = offered.get(option_key(choice.group), {}).get(option_key(choice.name))
        if opt is None:
            raise InvalidInput(
                f"Option {choice.group}/{choice.name} gibt es an {item.number} nicht",
                say=SAY_OPTION_UNKNOWN.format(option=choice.name, name=item.name),
            )
        if opt in chosen:
            raise InvalidInput(
                f"Option {choice.group}/{choice.name} doppelt",
                say=SAY_OPTION_REPEATED.format(option=opt.option_name, name=item.name),
            )
        chosen.append(opt)

    for key, group in offered.items():
        first = next(iter(group.values()))
        if not first.required:
            continue
        count = sum(1 for opt in chosen if option_key(opt.group_name) == key)
        say_args = {"group": first.group_name, "name": item.name}
        if count == 0:
            raise InvalidInput(
                f"Pflichtgruppe {first.group_name} an {item.number} fehlt",
                say=SAY_OPTION_MISSING.format(**say_args),
            )
        if count > 1:
            raise InvalidInput(
                f"Pflichtgruppe {first.group_name} an {item.number} mehrfach",
                say=SAY_OPTION_TWICE.format(**say_args),
            )

    options: list[dict[str, Any]] = [
        {
            "group": opt.group_name,
            "option": opt.option_name,
            "price_delta_cents": opt.price_delta_cents,
        }
        for opt in chosen
    ]
    if item.price_cents + sum(o["price_delta_cents"] for o in options) < 0:
        # Datenfehler in der Karte, kein Fall für den Gast: der kann nichts
        # korrigieren, also Übergabe ans Team statt Rückfrage (Codex PR #124).
        raise ServiceUnavailable(
            f"Preis von {item.number} mit Optionen negativ", say=SAY_MENU_BROKEN
        )
    return options
