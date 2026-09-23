"""Prüfungen vor der Summe: offen, aktiv, nicht aus, Optionen gültig, Pflichtgruppen gewählt.

Im Code, nicht im Modell (docs/04 §draft_order). Eine fehlende Pflichtwahl wird
nicht mit der Voreinstellung gefüllt, sondern erfragt - die Voreinstellung wäre
geraten (CLAUDE.md §2 Regel 2). Jeder Verstoß kommt mit einem `say`, das der
Agent vorlesen kann.
"""

import uuid
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.core.errors import Closed, Conflict, InvalidInput, NotFound, ServiceUnavailable
from api.domain.menu.items import is_sold_out, option_key
from api.domain.menu.search import SAY_SOLD_OUT
from api.domain.ordering.pricing import Line
from api.domain.status.hours import load_hours, open_window_at
from api.domain.status.service import PICKUP
from api.models import ItemOption, MenuItem
from api.schemas.orders import OrderItemIn

SAY_PICKUP_CLOSED = "Abholung ist gerade leider nicht möglich."
SAY_ITEM_UNKNOWN = "Ein Gericht habe ich nicht auf der Karte gefunden. Können Sie mir die Nummer sagen?"
SAY_OPTION_UNKNOWN = "{option} gibt es zu {name} nicht."
SAY_OPTION_MISSING = "Welche Auswahl bei {group} möchten Sie zu {name}?"
SAY_OPTION_REPEATED = "{option} zu {name} habe ich schon. Einmal {option}, richtig?"
SAY_MENU_BROKEN = (
    "Das kann ich gerade nicht sicher aufnehmen. Ich verbinde Sie mit dem Restaurant."
)
SAY_OPTION_TWICE = "Bei {group} zu {name} geht nur eine Auswahl. Welche möchten Sie?"

# Gruppe -> Option -> Zeile, beide Schlüssel über option_key() vereinheitlicht.
Offered = dict[str, dict[str, ItemOption]]


def require_pickup_open(
    session: Session, tenant_id: uuid.UUID, now: datetime, zone: ZoneInfo
) -> None:
    if not pickup_open_at(session, tenant_id, now, zone):
        raise Closed("Abholung gerade geschlossen", say=SAY_PICKUP_CLOSED)


def pickup_open_at(
    session: Session, tenant_id: uuid.UUID, at: datetime, zone: ZoneInfo
) -> bool:
    data = load_hours(session, tenant_id, at.astimezone(zone).date())
    return open_window_at(data, at, PICKUP, zone) is not None


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


def _options(item: MenuItem, wanted: OrderItemIn, offered: Offered) -> list[dict]:
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

    options = [
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
