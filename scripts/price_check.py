"""Compare a copy of the register files with the database (T-4.9, docs/14).

Call:
    python -m scripts.price_check imports/kasse --dry-run
    python -m scripts.price_check imports/kasse [--tenant-name N]

Reads the copies like kasse_to_csv and compares like import_menu --dry-run, so
there is one conversion and one comparison. Changes no price and writes no file:
without --dry-run the differences go to the tablet, where a person takes or
rejects each one. The logic is in api/domain/menu/price_check.py.

Exit code: 0 compared, with or without differences. 1 the import's checks
refused the set, or a database error; run it again, it changes no price. 2
nothing compared: folder or file not readable, no dish in the copy, tenant not
found.
"""

import argparse
import sys
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import DBAPIError

from api.config import settings
from api.db import SessionLocal
from api.domain.menu import price_check
from api.domain.menu.importer import FILES, apply, parse
from api.domain.menu.pos_convert import convert
from api.domain.menu.pos_dbf import DbfError
from api.models import Tenant
from scripts.kasse_to_csv import DEFAULT_SKIP_GROUPS, read_tables


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compare a register copy with the database"
    )
    parser.add_argument("folder", type=Path, help="folder with the register copies")
    parser.add_argument("--tenant-name", default=settings.tenant_name)
    parser.add_argument(
        "--dry-run", action="store_true", help="report only, nothing goes to the tablet"
    )
    args = parser.parse_args(argv)

    if not args.folder.is_dir():
        print(f"Folder not found: {args.folder}", file=sys.stderr)
        return 2
    try:
        converted = convert(
            **read_tables(args.folder),
            skip_groups=DEFAULT_SKIP_GROUPS.split(","),
            allergens_confirmed_by=None,
        )
    except (FileNotFoundError, DbfError) as exc:
        print(f"Error: {exc}, nothing compared.", file=sys.stderr)
        return 2
    if not converted.menu:
        print("Error: no dish in the register copy, nothing compared.", file=sys.stderr)
        return 2
    # An article the converter dropped is not compared: say so.
    for error in converted.errors:
        print(f"Register: {error}")

    written = converted.csv_files()
    plan = parse({name: written.get(name) for name in FILES})
    if not plan.ok:
        for error in plan.errors:
            print(f"Error: {error}", file=sys.stderr)
        return 1

    with SessionLocal() as session:
        try:
            tenant = session.scalar(
                select(Tenant).where(Tenant.name == args.tenant_name)
            )
            if tenant is None:
                print(f"Tenant not found: {args.tenant_name}", file=sys.stderr)
                return 2
            report = apply(session, tenant.id, plan, dry_run=True)
            cards = None
            if not args.dry_run:
                price_check.record(session, tenant.id, report.price_changes)
                cards = len(price_check.open_differences(session, tenant.id))
            answered = price_check.rejected(session, tenant.id)
        except ValueError as exc:
            # Stock does not fit the set (a register number on another dish).
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        except DBAPIError as exc:
            session.rollback()
            print(
                f"Error: database error: {str(exc.orig or exc).strip()}",
                file=sys.stderr,
            )
            return 1
    print(price_check.report_text(plan, report, answered, cards=cards))
    return 0


if __name__ == "__main__":
    sys.exit(main())
