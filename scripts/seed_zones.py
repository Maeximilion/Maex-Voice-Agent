"""Delivery zones from the pilot's list into `delivery_zones` (T-6.9, docs/14).

Call:
    python -m scripts.seed_zones imports/zones.csv --dry-run
    python -m scripts.seed_zones imports/zones.csv [--tenant-name N]

UTF-8, semicolon, decimal comma, columns `name;postal_codes;fee;min_order;eta_minutes`.
imports/ is in .gitignore: real zones never go into the repository.

The file is the whole truth for postal code zones: a zone it no longer names
goes inactive and stays, because addresses point at it.

Exit code: 0 done (or a dry run without errors), 1 errors in the file, nothing
written, 2 file or tenant not found.
"""

import argparse
import csv
import json
import re
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.config import settings
from api.db import SessionLocal
from api.domain.delivery.zones import POSTAL_CODE
from api.domain.menu.importer import parse_eur
from api.models import DeliveryZone, Tenant

COLUMNS = ("name", "postal_codes", "fee", "min_order", "eta_minutes")


@dataclass
class Zone:
    name: str
    postal_codes: list[str]
    fee_cents: int
    min_order_cents: int
    eta_minutes: int


def _amount(row: dict[str, str], column: str, where: str, errors: list[str]) -> int:
    # An empty cell is no free delivery: only a written 0,00 is.
    cents = parse_eur(row[column] or "")
    if cents is None or cents < 0:
        errors.append(f"{where}: {column} is no amount: {row[column]!r}")
        return 0
    return cents


def parse(text: str) -> tuple[list[Zone], list[str]]:
    reader = csv.DictReader(text.splitlines(), delimiter=";")
    missing = [c for c in COLUMNS if c not in (reader.fieldnames or [])]
    if missing:
        return [], [f"columns missing: {', '.join(missing)}"]
    zones: list[Zone] = []
    errors: list[str] = []
    owner: dict[str, str] = {}
    for line, row in enumerate(reader, start=2):
        name = (row["name"] or "").strip()
        where = f"line {line}"
        if not name:
            errors.append(f"{where}: name is empty")
        elif any(zone.name == name for zone in zones):
            errors.append(f"{where}: zone {name!r} named twice")
        codes = list(
            dict.fromkeys(re.split(r"[,\s]+", (row["postal_codes"] or "").strip()))
        )
        if codes == [""]:
            errors.append(f"{where}: no postal code")
            codes = []
        for code in codes:
            if not POSTAL_CODE.fullmatch(code):
                errors.append(f"{where}: no postal code: {code!r}")
            elif code in owner:
                # check_delivery refuses such a code: the fee would be a guess.
                errors.append(f"{where}: {code} is in zone {owner[code]!r} too")
            else:
                owner[code] = name
        fee = _amount(row, "fee", where, errors)
        minimum = _amount(row, "min_order", where, errors)
        eta = (row["eta_minutes"] or "").strip()
        if not re.fullmatch(r"[0-9]{1,4}", eta):
            errors.append(f"{where}: eta_minutes is no number: {eta!r}")
            eta = "0"
        zones.append(Zone(name, codes, fee, minimum, int(eta)))
    if not zones and not errors:
        # An empty file would switch every zone off.
        errors.append("no zone in the file")
    return zones, errors


def apply(session: Session, tenant_id: uuid.UUID, zones: list[Zone]) -> dict[str, int]:
    known = {
        row.name: row
        for row in session.scalars(
            select(DeliveryZone).where(
                DeliveryZone.tenant_id == tenant_id,
                DeliveryZone.match_type == "postal_code",
            )
        )
    }
    created = 0
    for zone in zones:
        row = known.pop(zone.name, None)
        if row is None:
            row = DeliveryZone(
                tenant_id=tenant_id, name=zone.name, match_type="postal_code"
            )
            session.add(row)
            created += 1
        row.postal_codes = zone.postal_codes
        row.fee_cents = zone.fee_cents
        row.min_order_cents = zone.min_order_cents
        row.eta_minutes = zone.eta_minutes
        row.active = True
    deactivated = 0
    for row in known.values():
        deactivated += row.active
        row.active = False
    return {
        "created": created,
        "updated": len(zones) - created,
        "deactivated": deactivated,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("file", type=Path)
    parser.add_argument("--tenant-name", default=settings.tenant_name)
    parser.add_argument("--dry-run", action="store_true", help="check, write nothing")
    args = parser.parse_args(argv)

    if not args.file.is_file():
        print(f"file not found: {args.file}", file=sys.stderr)
        return 2
    zones, errors = parse(args.file.read_text(encoding="utf-8-sig"))
    if errors:
        for error in errors:
            print(f"error: {error}", file=sys.stderr)
        print(f"{len(errors)} errors, nothing written.", file=sys.stderr)
        return 1
    with SessionLocal() as session:
        tenant = session.scalar(select(Tenant).where(Tenant.name == args.tenant_name))
        if tenant is None:
            print(f"tenant not found: {args.tenant_name}", file=sys.stderr)
            return 2
        report = apply(session, tenant.id, zones)
        if args.dry_run:
            session.rollback()
        else:
            session.commit()
    print(json.dumps({"dry_run": args.dry_run, **report}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
