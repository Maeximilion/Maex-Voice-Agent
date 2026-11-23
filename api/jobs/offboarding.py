"""Export a leaving customer's data before its stack is removed (#576, D32, docs/13 §8).

Call, in the customer's stack, as the host's deploy user so the files belong to it:
    docker compose run --rm -T --user "$(id -u):$(id -g)" -v <folder>:/export api \
        python -m api.jobs.offboarding export /export --key <customer key>

`scripts/remove_stack.py <key> --export` runs exactly this. It writes into the
folder, mode 700, every file mode 600:

- `menu_items.csv`, `item_options.csv`, `item_allergens.csv`, `item_aliases.csv`
  in the import format of docs/14, so `scripts/import_menu.py` reads them back
  into another stack as they were (prices as `6,90`, the format's own rule);
- `orders.csv`, `order_items.csv`, `reservations.csv`: every row of the tenant,
  amounts in integer cents (a position's unit price with its options, as
  paid), times in the tenant's zone (Europe/Berlin) with the
  zone's abbreviation, so the hour of the clock change is not ambiguous;
- `manifest.json`, written last: the key, the time, the zone, the schema
  revision, the rows and the SHA-256 of every file, and how many other tenants
  the database holds. `scripts/remove_stack.py` removes nothing without it.

Read-only: one transaction `REPEATABLE READ, READ ONLY`, so every file shows the
same moment and the database refuses any write. A rerun writes the same files
again; when none of them changed, the manifest stays as it is, time included. A
folder whose manifest names another key is refused.

The files hold what the database still holds: names and numbers of guests
within their period (docs/03 "Löschkonzept"), nothing the deletion job has
already stripped. They are the restaurant's data, handed back to it; never in
the repository, never in a log.

Exit code: 0 written or unchanged, 1 folder refused, 2 no tenant with that key.
"""

import argparse
import csv
import hashlib
import io
import json
import os
import re
import sys
import uuid
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from api.core.time import to_local, utcnow
from api.db import SessionLocal
from api.domain.menu.importer import (
    ALIASES_FILE,
    ALLERGENS_FILE,
    MENU_FILE,
    OPTIONS_FILE,
    format_eur,
)
from api.domain.ordering.pricing import row_cents
from api.models import (
    Address,
    DiningTable,
    ItemAlias,
    ItemAllergen,
    ItemOption,
    MenuItem,
    Order,
    OrderItem,
    Reservation,
    ReservationTable,
    Tenant,
)

MANIFEST = "manifest.json"
ORDERS_FILE = "orders.csv"
ORDER_ITEMS_FILE = "order_items.csv"
RESERVATIONS_FILE = "reservations.csv"
MENU_FILES = (MENU_FILE, OPTIONS_FILE, ALLERGENS_FILE, ALIASES_FILE)
FILES = (*MENU_FILES, ORDERS_FILE, ORDER_ITEMS_FILE, RESERVATIONS_FILE)
# Bumped when a file gains or loses a column; remove_stack.py reads only
# `customer_key`, `files` and `other_tenants`.
FORMAT = 1
# A spreadsheet runs a cell that starts with one of these as a formula. Guest
# text comes from speech and the tablet: it is written with a leading
# apostrophe, the usual guard. The menu files are not touched, they are the
# import format and go back in as they came out.
FORMULA_START = ("=", "+", "-", "@", "\t", "\r")

Rows = list[list[str]]


def _natural(number: str) -> tuple[Any, ...]:
    """23 before 107, s12 after 99: the card's order, not the alphabet's."""
    return tuple(int(p) if p.isdigit() else p for p in re.split(r"(\d+)", number))


def _yes(value: bool) -> str:
    return "ja" if value else "nein"


def _text(value: str | None) -> str:
    value = value or ""
    return "'" + value if value.startswith(FORMULA_START) else value


def _clock(value: datetime | None, zone: str) -> str:
    if value is None:
        return ""
    return to_local(value, zone).strftime("%Y-%m-%d %H:%M %Z")


def _csv(header: Sequence[str], rows: Iterable[Sequence[str]]) -> str:
    """Semicolon, as docs/14 and a German spreadsheet expect; UTF-8 without BOM."""
    out = io.StringIO()
    writer = csv.writer(out, delimiter=";", lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    return out.getvalue()


def menu_files(session: Session, tenant_id: uuid.UUID) -> dict[str, tuple[str, int]]:
    """The four import files of docs/14 for the tenant's whole menu, inactive
    dishes included. One `confirmed_by` per dish is all the format holds: two
    checkers of one dish are written as `A / B`. Aliases of every source go out;
    the import marks them as its own."""
    items = sorted(
        session.scalars(select(MenuItem).where(MenuItem.tenant_id == tenant_id)),
        key=lambda item: _natural(item.number),
    )
    ids = [item.id for item in items]
    options: dict[uuid.UUID, list[ItemOption]] = {}
    for option in session.scalars(
        select(ItemOption)
        .where(ItemOption.menu_item_id.in_(ids))
        .order_by(ItemOption.group_name, ItemOption.option_name)
    ):
        options.setdefault(option.menu_item_id, []).append(option)
    allergens: dict[uuid.UUID, list[ItemAllergen]] = {}
    for allergen in session.scalars(
        select(ItemAllergen)
        .where(ItemAllergen.menu_item_id.in_(ids))
        .order_by(ItemAllergen.allergen_code)
    ):
        allergens.setdefault(allergen.menu_item_id, []).append(allergen)
    aliases: dict[uuid.UUID, list[str]] = {}
    for alias in session.scalars(
        select(ItemAlias)
        .where(ItemAlias.menu_item_id.in_(ids))
        .order_by(ItemAlias.alias)
    ):
        aliases.setdefault(alias.menu_item_id, []).append(alias.alias)

    menu: Rows = []
    option_rows: Rows = []
    allergen_rows: Rows = []
    alias_rows: Rows = []
    for item in items:
        menu.append(
            [
                item.number,
                item.name,
                item.category,
                format_eur(item.price_cents),
                item.description or "",
                _yes(item.active),
                item.pos_code or "",
            ]
        )
        option_rows += [
            [
                item.number,
                o.group_name,
                o.option_name,
                format_eur(o.price_delta_cents),
                _yes(o.is_default),
                _yes(o.required),
                o.price_reason or "",
            ]
            for o in options.get(item.id, [])
        ]
        if item.id in allergens:
            found = allergens[item.id]
            checkers = sorted({a.confirmed_by for a in found})
            allergen_rows.append(
                [
                    item.number,
                    ",".join(a.allergen_code for a in found),
                    " / ".join(checkers),
                ]
            )
        alias_rows += [[item.number, alias] for alias in aliases.get(item.id, [])]
    return {
        MENU_FILE: (
            _csv(
                (
                    "number",
                    "name",
                    "category",
                    "price_eur",
                    "description",
                    "active",
                    "pos_code",
                ),
                menu,
            ),
            len(menu),
        ),
        OPTIONS_FILE: (
            _csv(
                (
                    "number",
                    "group_name",
                    "option_name",
                    "price_delta_eur",
                    "is_default",
                    "required",
                    "price_reason",
                ),
                option_rows,
            ),
            len(option_rows),
        ),
        ALLERGENS_FILE: (
            _csv(("number", "allergen_codes", "confirmed_by"), allergen_rows),
            len(allergen_rows),
        ),
        ALIASES_FILE: (_csv(("number", "alias"), alias_rows), len(alias_rows)),
    }


def order_files(
    session: Session, tenant_id: uuid.UUID, zone: str
) -> dict[str, tuple[str, int]]:
    """Every order of the tenant, drafts and cancelled ones too (the status
    says which), and its positions with the dish's number and name."""
    orders = session.execute(
        select(Order, Address)
        .outerjoin(
            Address,
            (Address.id == Order.address_id) & (Address.tenant_id == Order.tenant_id),
        )
        .where(Order.tenant_id == tenant_id)
        .order_by(Order.created_at, Order.id)
    ).all()
    rows: Rows = []
    for order, address in orders:
        rows.append(
            [
                str(order.id),
                _clock(order.created_at, zone),
                order.type,
                order.status,
                _clock(order.ready_at, zone),
                _text(order.customer_name),
                order.phone,
                _text(f"{address.street} {address.house_number}" if address else ""),
                address.postal_code if address else "",
                _text(address.city if address else ""),
                str(order.items_total_cents),
                str(order.delivery_fee_cents),
                str(order.total_cents),
                order.pickup_code or "",
                _text(order.note),
                _clock(order.register_entered_at, zone),
                _clock(order.deleted_at, zone),
                str(order.call_id),
            ]
        )
    positions = session.execute(
        select(OrderItem, MenuItem.number, MenuItem.name)
        .join(
            MenuItem,
            (MenuItem.id == OrderItem.menu_item_id)
            & (MenuItem.tenant_id == OrderItem.tenant_id),
        )
        .join(
            Order,
            (Order.id == OrderItem.order_id) & (Order.tenant_id == OrderItem.tenant_id),
        )
        .where(OrderItem.tenant_id == tenant_id)
        .order_by(Order.created_at, Order.id, OrderItem.created_at, OrderItem.id)
    ).all()
    # The stored unit price is the base price; the chosen options carry their
    # own deltas (domain/ordering/pricing.py), so the price paid is both.
    lines: Rows = [
        [
            str(line.order_id),
            number,
            name,
            str(line.quantity),
            str(row_cents(line.unit_price_cents, line.options)),
            str(line.quantity * row_cents(line.unit_price_cents, line.options)),
            _text(
                ", ".join(
                    f"{choice.get('group', '')}: {choice.get('option', '')}"
                    for choice in line.options
                )
            ),
            _text(line.note),
        ]
        for line, number, name in positions
    ]
    return {
        ORDERS_FILE: (
            _csv(
                (
                    "order_id",
                    "created",
                    "type",
                    "status",
                    "ready_at",
                    "customer_name",
                    "phone",
                    "street",
                    "postal_code",
                    "city",
                    "items_total_cents",
                    "delivery_fee_cents",
                    "total_cents",
                    "pickup_code",
                    "note",
                    "register_entered",
                    "deleted",
                    "call_id",
                ),
                rows,
            ),
            len(rows),
        ),
        ORDER_ITEMS_FILE: (
            _csv(
                (
                    "order_id",
                    "number",
                    "name",
                    "quantity",
                    "unit_price_cents",
                    "line_total_cents",
                    "options",
                    "note",
                ),
                lines,
            ),
            len(lines),
        ),
    }


def reservation_file(
    session: Session, tenant_id: uuid.UUID, zone: str
) -> dict[str, tuple[str, int]]:
    """Every reservation of the tenant, from the phone and from the tablet,
    with the names of the tables it was seated on."""
    tables: dict[uuid.UUID, list[str]] = {}
    for reservation_id, name in session.execute(
        select(ReservationTable.reservation_id, DiningTable.name)
        .join(DiningTable, DiningTable.id == ReservationTable.table_id)
        .where(ReservationTable.tenant_id == tenant_id)
        .order_by(DiningTable.name)
    ):
        tables.setdefault(reservation_id, []).append(name)
    rows: Rows = [
        [
            str(r.id),
            _clock(r.created_at, zone),
            _clock(r.reserved_for, zone),
            str(r.party_size),
            r.status,
            r.source,
            _text(r.guest_name),
            r.phone,
            _text(r.note),
            _text(", ".join(tables.get(r.id, []))),
            _clock(r.deleted_at, zone),
            str(r.call_id) if r.call_id else "",
        ]
        for r in session.scalars(
            select(Reservation)
            .where(Reservation.tenant_id == tenant_id)
            .order_by(Reservation.reserved_for, Reservation.id)
        )
    ]
    header = (
        "reservation_id",
        "created",
        "reserved_for",
        "party_size",
        "status",
        "source",
        "guest_name",
        "phone",
        "note",
        "tables",
        "deleted",
        "call_id",
    )
    return {RESERVATIONS_FILE: (_csv(header, rows), len(rows))}


def _digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _write(path: Path, content: str) -> None:
    """Under a temporary name, mode 600 from the first byte, then moved over."""
    temporary = path.with_name(f".{path.name}.new")
    temporary.unlink(missing_ok=True)
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
        handle.write(content)
    os.replace(temporary, path)


class Refused(Exception):
    """The folder cannot take this export; the message says why."""


def read_manifest(folder: Path) -> dict[str, Any] | None:
    path = folder / MANIFEST
    if not path.is_file():
        return None
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        raise Refused(
            f"{path} is not JSON; remove it or choose another folder"
        ) from None
    if not isinstance(manifest, dict):
        raise Refused(f"{path} is not an export manifest")
    return manifest


def export(
    session: Session, folder: Path, key: str, now: datetime
) -> tuple[dict[str, Any], bool] | None:
    """Write the tenant's export into `folder`. Returns the manifest and
    whether anything changed, or None when no tenant has this key. Reads only:
    the transaction is READ ONLY and is rolled back."""
    previous = read_manifest(folder)
    if previous is not None and previous.get("customer_key") != key:
        raise Refused(
            f"{folder} holds the export of {previous.get('customer_key')!r}, not {key!r}"
        )
    # First statement of the transaction: one snapshot for every file, and the
    # database itself refuses a write.
    session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
    try:
        tenant = session.scalar(select(Tenant).where(Tenant.customer_key == key))
        if tenant is None:
            return None
        zone = tenant.timezone
        files = {
            **menu_files(session, tenant.id),
            **order_files(session, tenant.id, zone),
            **reservation_file(session, tenant.id, zone),
        }
        others = session.scalar(
            select(func.count()).select_from(Tenant).where(Tenant.id != tenant.id)
        )
        revision = session.scalar(text("SELECT version_num FROM alembic_version"))
        tenant_id = str(tenant.id)
    finally:
        session.rollback()

    listed = {
        name: {"rows": rows, "sha256": _digest(content)}
        for name, (content, rows) in files.items()
    }
    facts = {
        "format": FORMAT,
        "customer_key": key,
        "tenant_id": tenant_id,
        "timezone": zone,
        "schema_revision": revision,
        # remove_stack.py refuses on this count: a tenant added since the last
        # run must reach the manifest even when the files stay the same.
        "other_tenants": others or 0,
        "files": listed,
    }
    unchanged = (
        previous is not None
        and {k: previous.get(k) for k in facts} == facts
        and all((folder / name).is_file() for name in FILES)
        and all(
            _digest((folder / n).read_text("utf-8")) == listed[n]["sha256"]
            for n in FILES
        )
    )
    if unchanged:
        assert previous is not None
        return previous, False
    if not folder.is_dir():
        # A folder made here is the owner's alone; one handed in (the bind
        # mount of remove_stack.py, made 700 on the host) keeps its mode.
        folder.mkdir(mode=0o700, parents=True)
    for name, (content, _) in files.items():
        _write(folder / name, content)
    manifest = {**facts, "exported_at": now.isoformat(timespec="seconds")}
    # Last: a folder with a manifest holds a whole export.
    _write(folder / MANIFEST, json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest, True


def _export(args: argparse.Namespace) -> int:
    try:
        with SessionLocal() as session:
            done = export(session, args.folder, args.key, utcnow())
    except (Refused, OSError) as refused:
        print(f"refused: {refused}", file=sys.stderr)
        return 1
    if done is None:
        print(f"no tenant with the key {args.key!r} in this database", file=sys.stderr)
        return 2
    manifest, changed = done
    for name, entry in manifest["files"].items():
        print(f"{name}: {entry['rows']} rows", file=sys.stderr)
    if manifest["other_tenants"]:
        print(
            f"note: this database holds {manifest['other_tenants']} other tenant(s)",
            file=sys.stderr,
        )
    print(
        ("Export written" if changed else "Export unchanged")
        + f" at {manifest['exported_at']}.",
        file=sys.stderr,
    )
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("export", help="menu, orders and reservations")
    command.add_argument("folder", type=Path)
    command.add_argument("--key", required=True, help="the tenant's customer key")
    command.set_defaults(run=_export)
    args = parser.parse_args(argv)
    run: Callable[[argparse.Namespace], int] = args.run
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
