"""Speisekarte aus CSV einspielen (docs/14, T-4.2).

Aufruf:
    python -m scripts.import_menu imports/ --dry-run
    python -m scripts.import_menu imports/ [--apply-price-changes] [--tenant-name N]

Erwartet im Ordner menu_items.csv und optional item_options.csv,
item_allergens.csv, item_aliases.csv (UTF-8, Semikolon, Dezimalkomma). Der
Ordner imports/ ist im .gitignore: echte Kartendaten kommen nicht ins Repo.

Exit-Code: 0 eingespielt (oder Probelauf ohne Fehler), 1 Prüffehler in den
Dateien, 2 Ordner oder Mandant nicht gefunden. Die Logik steckt in
api/domain/menu/importer.py, hier nur Dateien lesen und Bericht drucken.
"""

import argparse
import sys
from pathlib import Path

from sqlalchemy import select

from api.config import settings
from api.db import SessionLocal
from api.domain.menu.importer import FILES, apply, parse
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
        tenant = session.scalar(select(Tenant).where(Tenant.name == args.tenant_name))
        if tenant is None:
            print(f"Mandant nicht gefunden: {args.tenant_name}", file=sys.stderr)
            return 2
        try:
            report = apply(
                session,
                tenant.id,
                plan,
                apply_price_changes=args.apply_price_changes,
                dry_run=args.dry_run,
            )
        except ValueError as exc:
            # Bestand passt nicht zum Plan (z. B. 23A und 23a): nichts eingespielt.
            print(f"Fehler: {exc}", file=sys.stderr)
            return 1
    print(report.as_text())
    return 0


if __name__ == "__main__":
    sys.exit(main())
