"""Price check of the register against the database (T-4.9, docs/14).

The register is the master for prices (docs/02 §6), and still no price changes
by itself: a copy can be old or taken in the middle of a change, and a wrong
price goes straight to the phone. The comparison is the import's
(`importer.apply` in a dry run). This module keeps what it found on the dish,
and a person answers on the tablet (docs/06 §3):

- `record` leaves the register's price in `menu_items.register_price_cents`.
- `decide` takes it into `price_cents` or rejects it, for the price the person
  saw and no other, and writes `audit_log`. `audit_log` is the only record of an
  answer once a later check has cleared the columns.

A rejected price stays out of sight until the register says another one. A
dish's own price only: the surcharge of an option stands in the report, not on
the tablet, because one changed extra would be a card on every dish it fits. A
dish whose options differ as well gets no card at all: a size costs base plus
surcharge (`pricing.row_cents`), so the base alone could make a price neither
side has. The import takes both together.
"""

import hashlib
import uuid
from dataclasses import dataclass
from typing import cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.core.errors import NotFound
from api.domain.menu.importer import Plan, Report, apply, format_eur
from api.domain.menu.numberwords import canonical_card
from api.domain.menu.sold_out import number_key
from api.domain.status.config import ACTOR_TABLET
from api.models import AuditLog, MenuItem

ACTOR_CHECK = "price_check"
ACTION_CHECKED = "menu.price_checked"
ACTION_TAKEN = "menu_item.price_taken"
ACTION_REJECTED = "menu_item.price_rejected"


@dataclass(frozen=True)
class Difference:
    menu_item_id: uuid.UUID
    number: str
    name: str
    price_cents: int
    register_cents: int


def compare_and_record(session: Session, tenant_id: uuid.UUID, plan: Plan) -> Report:
    """Compare and keep the result under one lock on the menu rows.

    The dry run ends with a rollback. In a savepoint that rollback leaves the
    lock taken here, so no import, which locks these rows first, can change a
    price between the comparison and what is recorded from it."""
    session.scalars(
        select(MenuItem.id).where(MenuItem.tenant_id == tenant_id).with_for_update()
    ).all()
    session.begin_nested()
    report = apply(session, tenant_id, plan, dry_run=True)
    record(session, tenant_id, report)
    return report


def record(session: Session, tenant_id: uuid.UUID, report: Report) -> None:
    """Keep what a comparison found and change no price.

    The same register price as last time stays as it is, open or rejected, and
    a dish in line with the register has no difference any more. A dish the
    copy does not name keeps what it has: it may be an article the converter
    could not read, so its absence proves nothing about its price."""
    found = {canonical_card(n): new for n, _, new in report.price_changes}
    unnamed = {canonical_card(n) for n in report.items_not_in_file}
    tied = {canonical_card(n) for n in report.options_differ}
    differences = []
    for item in session.scalars(
        select(MenuItem).where(MenuItem.tenant_id == tenant_id).with_for_update()
    ):
        key = canonical_card(item.number)
        if key in unnamed:
            continue
        new = found.get(key)
        if new is not None and key in tied:
            # No card: an open one goes, its take would now be wrong. A rejection
            # stands, the team's answer was not about the options.
            new = item.register_price_cents if item.register_price_rejected else None
        if new == item.register_price_cents:
            continue
        item.register_price_cents = new
        item.register_price_rejected = False
        if new is not None:
            differences.append(
                {"number": item.number, "from": item.price_cents, "to": new}
            )
    if differences:
        session.add(
            AuditLog(
                tenant_id=tenant_id,
                actor=ACTOR_CHECK,
                action=ACTION_CHECKED,
                entity="menu",
                payload={"differences": differences},
            )
        )
    session.commit()


def open_differences(session: Session, tenant_id: uuid.UUID) -> list[Difference]:
    """What the team has not answered, in menu order. An inactive dish is not
    offered, so it has no card until it is back."""
    items = session.scalars(
        select(MenuItem).where(
            MenuItem.tenant_id == tenant_id,
            MenuItem.active.is_(True),
            MenuItem.register_price_cents.is_not(None),
            MenuItem.register_price_rejected.is_(False),
        )
    )
    return [
        Difference(
            item.id,
            item.number,
            item.name,
            item.price_cents,
            cast(int, item.register_price_cents),
        )
        for item in sorted(items, key=lambda i: number_key(i.number))
    ]


def change_token(session: Session, tenant_id: uuid.UUID) -> str:
    """A fingerprint of the open differences for the event stream (gui/sse.py)."""
    shown = ",".join(
        f"{d.menu_item_id}:{d.register_cents}"
        for d in open_differences(session, tenant_id)
    )
    return hashlib.sha256(shown.encode()).hexdigest()[:16]


def decide(
    session: Session,
    tenant_id: uuid.UUID,
    menu_item_id: uuid.UUID,
    register_cents: int,
    *,
    take: bool,
) -> None:
    """The answer to the price the person saw. One tap, no question back (docs/06
    §1 rule 6).

    `NotFound` when that price is not open: a newer copy replaced it, the dish
    is gone, or somebody answered first. Nobody saw what would change then."""
    item = session.scalars(
        select(MenuItem)
        .where(
            MenuItem.tenant_id == tenant_id,
            MenuItem.id == menu_item_id,
            MenuItem.active.is_(True),
        )
        .with_for_update()
    ).first()
    # Compared here, after the lock: a number from the address that does not
    # fit the column is no price on a card, not a database error.
    if (
        item is None
        or item.register_price_cents != register_cents
        or item.register_price_rejected
    ):
        raise NotFound("No open price difference")
    if take:
        action = ACTION_TAKEN
        payload = {
            "number": item.number,
            "from": item.price_cents,
            "to": register_cents,
        }
        item.price_cents = register_cents
        item.register_price_cents = None
    else:
        action = ACTION_REJECTED
        payload = {
            "number": item.number,
            "price": item.price_cents,
            "register": register_cents,
        }
        item.register_price_rejected = True
    session.add(
        AuditLog(
            tenant_id=tenant_id,
            actor=ACTOR_TABLET,
            action=action,
            entity="menu_item",
            entity_id=item.id,
            payload=payload,
        )
    )
    session.commit()


def rejected(session: Session, tenant_id: uuid.UUID) -> set[tuple[str, int | None]]:
    """(card number, register price) of what the team rejected, for the report."""
    rows = session.execute(
        select(MenuItem.number, MenuItem.register_price_cents).where(
            MenuItem.tenant_id == tenant_id, MenuItem.register_price_rejected.is_(True)
        )
    )
    return {(canonical_card(number), cents) for number, cents in rows}


def _amounts(old: int, new: int) -> str:
    return f"{old} -> {new} cents ({format_eur(old)} € -> {format_eur(new)} €)"


def report_text(
    plan: Plan,
    report: Report,
    answered: set[tuple[str, int | None]],
    *,
    cards: int | None,
) -> str:
    """The differences per dish in cents, and what could not be compared.
    `cards`: the open differences on the tablet, None in a dry run."""
    total = len(report.price_changes) + len(report.option_price_changes)
    lines = [
        f"Price check: {total} difference(s)." if total else "No price difference."
    ]
    for number, old, new in report.price_changes:
        if (canonical_card(number), new) in answered:
            state = ", rejected on the tablet"
        elif number in report.options_differ:
            state = ", only with the import: its options differ too"
        else:
            state = ""
        name = plan.items[number].name
        lines.append(f"{number} {name}: {_amounts(old, new)}{state}")
    for number, group, option, old, new in report.option_price_changes:
        name = plan.items[number].name
        lines.append(
            f"Surcharge {number} {name}, {group}/{option}: {_amounts(old, new)}"
        )
    if report.option_price_changes:
        lines.append(
            "Surcharges are not on the tablet: take them with kasse_to_csv and "
            "import_menu --apply-price-changes."
        )
    if report.items_new:
        lines.append(
            "In the register, not in the database: " + ", ".join(report.items_new)
        )
    if report.items_not_in_file:
        lines.append(
            "In the database, not in the register copy: "
            + ", ".join(report.items_not_in_file)
        )
    if cards is None:
        lines.append("Dry run, nothing stored.")
    elif cards:
        lines.append(f"{cards} open on the tablet.")
    return "\n".join(lines)
