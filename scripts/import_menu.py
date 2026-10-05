"""Speisekarte aus CSV einspielen (docs/14, T-4.2).

Aufruf:
    python -m scripts.import_menu imports/ --dry-run
    python -m scripts.import_menu imports/ [--apply-price-changes] [--tenant-name N]
    python -m scripts.import_menu imports/ --dry-run --deactivate-missing   # Kasse

Erwartet im Ordner menu_items.csv und optional item_options.csv,
item_allergens.csv, item_aliases.csv (UTF-8, Semikolon, Dezimalkomma). Der
Ordner imports/ ist im .gitignore: echte Kartendaten kommen nicht ins Repo.

Exit code: 0 imported (or a dry run without errors), 1 check errors in the
files, a database error before the commit (no connection, or a row the
database rejects - a dry run sends every row too), or the commit did not
finish and the outcome is unknown (the message says how to find out),
2 folder or tenant not found. Die Logik steckt in
api/domain/menu/importer.py, hier nur Dateien lesen und Bericht drucken.
"""

import argparse
import sys
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import DBAPIError

from api.config import settings
from api.db import SessionLocal
from api.domain.menu.importer import (
    FILES,
    CommitInterruptedError,
    CommitOutcomeUnknownError,
    MassDeactivationError,
    apply,
    parse,
)
from api.models import Tenant


def read_files(folder: Path) -> dict[str, str | None]:
    return {
        name: (folder / name).read_text(encoding="utf-8-sig")
        if (folder / name).is_file()
        else None
        for name in FILES
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Speisekarte aus CSV einspielen")
    parser.add_argument("folder", type=Path, help="Ordner mit den CSV-Dateien")
    parser.add_argument("--tenant-name", default=settings.tenant_name)
    parser.add_argument(
        "--dry-run", action="store_true", help="nur prüfen und berichten"
    )
    parser.add_argument(
        "--apply-price-changes",
        action="store_true",
        help="geänderte Preise bestehender Gerichte übernehmen",
    )
    parser.add_argument(
        "--deactivate-missing",
        action="store_true",
        help="Gerichte, die nicht in der Datei stehen, inaktiv setzen (Kasse als Quelle)",
    )
    parser.add_argument(
        "--allow-large-deactivation",
        action="store_true",
        help="mehr als die Hälfte der aktiven Karte deaktivieren erlauben",
    )
    args = parser.parse_args(argv)

    if not args.folder.is_dir():
        print(f"Ordner nicht gefunden: {args.folder}", file=sys.stderr)
        return 2
    plan = parse(read_files(args.folder))
    if not plan.ok:
        for warning in plan.warnings:
            print(f"Warnung: {warning}")
        for error in plan.errors:
            print(f"Fehler: {error}", file=sys.stderr)
        print(f"{len(plan.errors)} Fehler - nichts eingespielt.", file=sys.stderr)
        return 1

    with SessionLocal() as session:
        try:
            tenant = session.scalar(
                select(Tenant).where(Tenant.name == args.tenant_name)
            )
            if tenant is None:
                print(f"Mandant nicht gefunden: {args.tenant_name}", file=sys.stderr)
                return 2
            report = apply(
                session,
                tenant.id,
                plan,
                apply_price_changes=args.apply_price_changes,
                deactivate_missing=args.deactivate_missing,
                allow_large_deactivation=args.allow_large_deactivation,
                dry_run=args.dry_run,
            )
        except MassDeactivationError as exc:
            print(
                f"Fehler: {exc} Ist es gewollt, mit --allow-large-deactivation "
                "wiederholen.",
                file=sys.stderr,
            )
            return 1
        except ValueError as exc:
            # Bestand passt nicht zum Plan (z. B. 23A und 23a): nichts eingespielt.
            print(f"Fehler: {exc}", file=sys.stderr)
            return 1
        except (CommitOutcomeUnknownError, CommitInterruptedError) as exc:
            # Every row was accepted, then the commit raised or was interrupted:
            # the server may have committed before its answer got lost (Codex
            # PR #169, PR #176).
            print(
                f"Error: the commit did not finish, so it is unknown whether the "
                f"import was stored: {exc}\n"
                "Run the same command again with --dry-run (same files, same "
                "switches). If the report counts nothing new, changed, removed or "
                "deactivated, the import went through, and audit_log holds a "
                "menu.imported entry if it changed anything. If it counts the "
                "same changes as before, the import was not stored: repeat it. "
                "Price changes that are not applied, dishes not in the file and "
                "warnings are listed either way.",
                file=sys.stderr,
            )
            return 1
        except DBAPIError as exc:
            # A database error before the commit: no connection, or a row the
            # database refuses although the checks of the files let it through.
            # A dry run sends every row too, so it ends here as well - with a
            # message instead of a stack trace (review PR #169).
            session.rollback()
            print(
                f"Error: database error before the commit, nothing was stored: "
                f"{str(exc.orig or exc).strip()}",
                file=sys.stderr,
            )
            return 1
    print(report.as_text())
    return 0


if __name__ == "__main__":
    sys.exit(main())
