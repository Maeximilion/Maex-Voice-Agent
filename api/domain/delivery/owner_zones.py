"""The delivery zones, changed by the owner behind the admin PIN (#571, D34
point 3, docs/23 §6).

A zone is a list of postal codes with its fee, its minimum order and its
delivery time. `check_delivery` reads these rows on every call, so a save is
checked against what that reader does with them:

- A postal code belongs to one active zone. `check_delivery` answers a code in
  two active zones with "not available", because the fee would be whichever
  row came first; a save that would make a second active zone for a code is
  refused and names the other zone. A zone switched off may hold any code and
  cannot be switched on while one of its codes is delivered by another zone.
- A postal code is five ASCII digits, as `check_delivery` compares it.
- A name is taken once per restaurant, whatever its case and whether its zone
  is on: `scripts/seed_zones.py` finds a zone by its name, and a second row of
  a name would be the one it neither updates nor switches off.
- The delivery time is at most the longest wait the team can set
  (`config.MAX_WAIT_MINUTES`): `draft_order` promises the longer of the two.

Amounts are typed in euros with a decimal comma and kept in integer cents. The
bounds on them catch a typing error (250 for 2,50) and are no rule about a
restaurant. A value sent back as it is stored is taken without its bound: a
zone the import wrote with a higher fee or a longer time still takes a change
of its other fields.

Nothing is deleted: addresses point at their zone, so a zone no longer wanted
is switched off, as the import does. A zone on a map (`polygon`) is neither
listed nor changed here.

Every save runs under the lock of the restaurant's settings row, so two
devices cannot each add the same code to a different zone. It leaves an audit
row with the zone before and after.
"""

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.core.errors import Conflict, InvalidInput, NotFound
from api.domain.delivery.zones import POSTAL_CODE
from api.domain.menu.importer import parse_eur
from api.domain.status.config import MAX_WAIT_MINUTES
from api.domain.status.staff_changes import ACTOR_OWNER, _locked
from api.models import AuditLog, DeliveryZone

POSTAL = "postal_code"
MAX_NAME = 40
# Bounds on what one form can write, not rules about the restaurant.
MAX_CODES = 200
MAX_FEE_CENTS = 50_00
MAX_MIN_ORDER_CENTS = 500_00
# How much of a typed postal code a message repeats.
ECHO = 12
MINUTES = re.compile(r"[0-9]{1,4}")
# `draft_order` promises the longer of the zone's time and the team's wait.
MAX_ETA_MINUTES = MAX_WAIT_MINUTES

NO_NAME = "Bitte einen Namen für die Zone angeben."
NAME_TOO_LONG = f"Der Name der Zone darf höchstens {MAX_NAME} Zeichen haben."
NAME_TAKEN = (
    "Eine Zone mit diesem Namen gibt es schon. Bitte einen anderen Namen wählen."
)
NO_CODES = "Bitte mindestens eine Postleitzahl angeben."
NO_CODE = "„{code}“ ist keine Postleitzahl. Eine Postleitzahl hat fünf Ziffern."
TOO_MANY_CODES = f"Höchstens {MAX_CODES} Postleitzahlen je Zone."
CODE_TAKEN = (
    "Die Postleitzahl {code} gehört schon zur Zone „{zone}“. "
    "Eine Postleitzahl kann nur in einer aktiven Zone stehen."
)
NO_AMOUNT = (
    "Bitte {field} als Betrag in Euro angeben, zum Beispiel 2,50. Ohne Betrag: 0."
)
AMOUNT_TOO_HIGH = "{field} darf höchstens {most} Euro betragen."
# An amount of the form: how a message asks for it, how one names it, its bound.
FEE = ("die Lieferpauschale", "Die Lieferpauschale", MAX_FEE_CENTS)
MIN_ORDER = ("den Mindestbestellwert", "Der Mindestbestellwert", MAX_MIN_ORDER_CENTS)
NO_MINUTES = (
    f"Bitte die Lieferzeit in ganzen Minuten angeben, von 0 bis {MAX_ETA_MINUTES}."
)
CHANGED_SINCE = "Diese Zone hat sich gerade geändert. Bitte noch einmal prüfen."


@dataclass(frozen=True)
class Typed:
    """One zone as its form sent it: text, read and checked here."""

    name: str
    postal_codes: str
    fee: str
    min_order: str
    eta: str
    active: bool


def zones(session: Session, tenant_id: uuid.UUID) -> list[DeliveryZone]:
    """The restaurant's postal code zones: the active ones first, by name."""
    rows = session.scalars(
        select(DeliveryZone)
        .where(DeliveryZone.tenant_id == tenant_id, DeliveryZone.match_type == POSTAL)
        .execution_options(populate_existing=True)
    ).all()
    return sorted(rows, key=lambda row: (not row.active, row.name.casefold(), row.id))


def zone(session: Session, tenant_id: uuid.UUID, zone_id: uuid.UUID) -> DeliveryZone:
    """One postal code zone of this restaurant. Raises NotFound."""
    row = session.scalars(
        select(DeliveryZone)
        .where(
            DeliveryZone.id == zone_id,
            DeliveryZone.tenant_id == tenant_id,
            DeliveryZone.match_type == POSTAL,
        )
        .execution_options(populate_existing=True)
    ).first()
    if row is None:
        raise NotFound(f"no postal code zone {zone_id}")
    return row


def token(row: DeliveryZone) -> str:
    """What a form saw of one zone: a save against another state is refused."""
    text = json.dumps(_values(row), sort_keys=True)
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def save_zone(
    session: Session,
    tenant_id: uuid.UUID,
    zone_id: uuid.UUID | None,
    typed: Typed,
    seen: str,
    actor: str = ACTOR_OWNER,
) -> bool:
    """Write one zone as the form sent it: a new one without `zone_id`.
    `seen` is the `token` of the zone the form showed; a zone changed since (a
    second device) raises Conflict, so a save never writes over values the
    owner did not see. False when nothing changes, whatever the token: then no
    row is written, and a save tapped twice is no conflict, also for a new
    zone. Raises InvalidInput with the reason for the owner, NotFound for a
    zone that is not this restaurant's. Commits."""
    # One save of a restaurant after the other: the check for a code in two
    # zones reads the other zones, and a second device must not write between.
    _locked(session, tenant_id)
    try:
        return _save(session, tenant_id, zone_id, typed, seen, actor)
    except (InvalidInput, Conflict, NotFound):
        session.rollback()
        raise


# --- helpers ---------------------------------------------------------------------


def _save(
    session: Session,
    tenant_id: uuid.UUID,
    zone_id: uuid.UUID | None,
    typed: Typed,
    seen: str,
    actor: str,
) -> bool:
    every = session.scalars(
        select(DeliveryZone)
        .where(DeliveryZone.tenant_id == tenant_id)
        .execution_options(populate_existing=True)
    ).all()
    stored = None
    if zone_id is not None:
        stored = next(
            (row for row in every if row.id == zone_id and row.match_type == POSTAL),
            None,
        )
        if stored is None:
            raise NotFound(f"no postal code zone {zone_id}")
    before = _values(stored) if stored is not None else None
    after = _read(typed, before or {})

    if stored is None:
        # A new zone sent twice (a double tap): the zone is there already.
        twin = next(
            (
                row
                for row in every
                if row.match_type == POSTAL and _values(row) == after
            ),
            None,
        )
        if twin is not None:
            session.commit()
            return False
    elif after == before:
        session.commit()
        return False
    elif seen != token(stored):
        raise Conflict(f"zone {zone_id} changed since shown", say=CHANGED_SINCE)

    others = [row for row in every if stored is None or row.id != stored.id]
    name = after["name"]
    renamed = stored is None or name != stored.name
    if renamed and any(
        row.name.strip().casefold() == name.casefold() for row in others
    ):
        raise InvalidInput(f"zone name taken: {name!r}", say=NAME_TAKEN)
    if after["active"]:
        for row in others:
            if not row.active or row.match_type != POSTAL:
                continue
            shared = set(after["postal_codes"]) & set(row.postal_codes or [])
            if shared:
                code = min(shared)
                raise InvalidInput(
                    f"postal code {code} is in zone {row.id} too",
                    say=CODE_TAKEN.format(code=code, zone=row.name),
                )

    if stored is None:
        stored = DeliveryZone(tenant_id=tenant_id, match_type=POSTAL)
        session.add(stored)
    stored.name = name
    stored.postal_codes = after["postal_codes"]
    stored.fee_cents = after["fee_cents"]
    stored.min_order_cents = after["min_order_cents"]
    stored.eta_minutes = after["eta_minutes"]
    stored.active = after["active"]
    session.flush()
    session.add(
        AuditLog(
            tenant_id=tenant_id,
            actor=actor,
            action="delivery_zone.created"
            if before is None
            else "delivery_zone.changed",
            entity="delivery_zones",
            entity_id=stored.id,
            payload={"from": before, "to": after},
        )
    )
    session.commit()
    return True


def _values(row: DeliveryZone) -> dict[str, Any]:
    """What the form shows and writes of a zone, and what the audit row keeps."""
    return {
        "name": row.name,
        "postal_codes": list(row.postal_codes or []),
        "fee_cents": row.fee_cents,
        "min_order_cents": row.min_order_cents,
        "eta_minutes": row.eta_minutes,
        "active": row.active,
    }


def _read(typed: Typed, kept: dict[str, Any]) -> dict[str, Any]:
    """The typed zone as values. `kept`: the stored values of a zone that is
    changed. A name, an amount or a time sent back as it is stored is taken
    as it is: the bounds catch a typing error, and a zone the import or SQL
    wrote beyond them must not block a change of its other fields."""
    return {
        "name": typed.name if typed.name == kept.get("name") else _name(typed.name),
        "postal_codes": _codes(typed.postal_codes),
        "fee_cents": _amount(typed.fee, FEE, kept.get("fee_cents")),
        "min_order_cents": _amount(
            typed.min_order, MIN_ORDER, kept.get("min_order_cents")
        ),
        "eta_minutes": _minutes(typed.eta, kept.get("eta_minutes")),
        "active": typed.active,
    }


def _name(text: str) -> str:
    name = text.strip()
    # Printable only: the name goes to the model with every zone answer and
    # onto the tablet, so no control or direction character rides along.
    if not name or not name.isprintable():
        raise InvalidInput("zone without a usable name", say=NO_NAME)
    if len(name) > MAX_NAME:
        raise InvalidInput("zone name too long", say=NAME_TOO_LONG)
    return name


def _codes(text: str) -> list[str]:
    codes = list(dict.fromkeys(part for part in re.split(r"[,\s]+", text) if part))
    if not codes:
        raise InvalidInput("zone without a postal code", say=NO_CODES)
    if len(codes) > MAX_CODES:
        raise InvalidInput("too many postal codes", say=TOO_MANY_CODES)
    for code in codes:
        if not POSTAL_CODE.fullmatch(code):
            raise InvalidInput(
                "no postal code in a zone", say=NO_CODE.format(code=code[:ECHO])
            )
    return codes


def _amount(text: str, field: tuple[str, str, int], kept: int | None) -> int:
    """Euros with a decimal comma to cents, never through a float. An empty
    field is no "free": only a written 0 is (as in the import). ASCII digits
    only, and no dot: "2.50" read as 250 euros would be a fee no guest pays."""
    asked, named, most = field
    text = text.strip()
    # The length first: `int` refuses a number of thousands of digits.
    plain = len(text) <= ECHO and text.isascii()
    cents = parse_eur(text) if plain else None
    if cents is None or cents < 0:
        raise InvalidInput(
            f"no amount: {text[:ECHO]!r}", say=NO_AMOUNT.format(field=asked)
        )
    if cents > most and cents != kept:
        raise InvalidInput(
            f"amount above the bound: {cents}",
            say=AMOUNT_TOO_HIGH.format(
                field=named, most=f"{most // 100},{most % 100:02d}"
            ),
        )
    return cents


def _minutes(text: str, kept: int | None) -> int:
    text = text.strip()
    if not MINUTES.fullmatch(text) or MAX_ETA_MINUTES < int(text) != kept:
        raise InvalidInput(f"no delivery time: {text[:ECHO]!r}", say=NO_MINUTES)
    return int(text)
