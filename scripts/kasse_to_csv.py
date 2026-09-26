"""Kassendateien (.dbf) in die CSV-Dateien nach docs/14 umwandeln (T-4.11).

Aufruf:
    python -m scripts.kasse_to_csv imports/kasse --out imports
    python -m scripts.kasse_to_csv imports/kasse --out imports --allergens-confirmed-by Maxi

Liest nur Kopien: artikel.DBF + .DBT, zutaten.DBF + .DBT, warengrp.dbf,
zutgrp.DBF (Groß- und Kleinschreibung egal). Schreibt menu_items.csv,
item_options.csv und item_allergens.csv in den Zielordner. item_aliases.csv aus
dem Chat bleibt, nur Zeilen zu Nummern, die die Kasse nicht liefert, wandern nach
item_aliases.verworfen.csv (sie würden den Import blockieren). Danach wie immer:
    python -m scripts.import_menu imports --dry-run

Exit-Code: 0 geschrieben, 1 geschrieben, aber Artikel mit Fehlern im Bericht
(nicht übernommen), 2 Ordner, Datei oder Format nicht lesbar - nichts geschrieben.
Die Logik steckt in api/domain/menu/pos_convert.py.
"""

import argparse
import csv
import sys
from pathlib import Path

from api.domain.menu.importer import ALIASES_FILE
from api.domain.menu.pos_convert import convert, split_aliases
from api.domain.menu.pos_dbf import DbfError, Table, read_table

# Warengruppen, die am Telefon nicht bestellt werden (Getränke, Menüs, Pfand,
# Interna), Maxi 26.09.2026. Mit --skip-groups überschreibbar.
ALIASES_DROPPED = "item_aliases.verworfen.csv"
DEFAULT_SKIP_GROUPS = (
    "015,016,017,018,019,020,021,022,023,024,025,100,101,102,"
    "EXS,FRE,GAH,GEH,GET,OHN,PFA,RTN,SON"
)


def _find(folder: Path, name: str) -> Path:
    for path in folder.iterdir():
        if path.name.lower() == name.lower():
            return path
    raise FileNotFoundError(f"{name} fehlt in {folder}")


def load(folder: Path, table: str, memo: bool = False) -> Table:
    data = _find(folder, f"{table}.dbf").read_bytes()
    memo_data = _find(folder, f"{table}.dbt").read_bytes() if memo else None
    try:
        return read_table(data, memo_data)
    except DbfError as exc:
        raise DbfError(f"{table}: {exc}") from exc


def _set_aside_aliases(out: Path, numbers: list[str]) -> None:
    source = out / ALIASES_FILE
    if not source.is_file():
        return
    kept, dropped = split_aliases(source.read_text(encoding="utf-8-sig"), numbers)
    if not dropped:
        return
    target = out / ALIASES_DROPPED
    new_file = not target.is_file()
    with target.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=list(dropped[0]), delimiter=";", lineterminator="\n"
        )
        if new_file:
            writer.writeheader()
        writer.writerows(dropped)
    source.write_text(kept, encoding="utf-8")
    listed = ", ".join(sorted({row["number"] for row in dropped}))
    print(
        f"Warnung: {len(dropped)} Aliase zu Nummern, die die Kasse nicht liefert "
        f"({listed}), nach {target.name} verschoben"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Kassendateien in CSV umwandeln")
    parser.add_argument("folder", type=Path, help="Ordner mit den Kopien der Kasse")
    parser.add_argument("--out", type=Path, default=Path("imports"))
    parser.add_argument(
        "--allergens-confirmed-by",
        default=None,
        help="wer die Allergene in der Kasse geprüft hat; ohne: keine übernommen",
    )
    parser.add_argument("--skip-groups", default=DEFAULT_SKIP_GROUPS)
    args = parser.parse_args(argv)

    if not args.folder.is_dir():
        print(f"Ordner nicht gefunden: {args.folder}", file=sys.stderr)
        return 2
    try:
        tables = {
            "artikel": load(args.folder, "artikel", memo=True),
            "warengrp": load(args.folder, "warengrp"),
            "zutaten": load(args.folder, "zutaten", memo=True),
            "zutgrp": load(args.folder, "zutgrp"),
        }
    except (FileNotFoundError, DbfError) as exc:
        print(f"Fehler: {exc} - nichts geschrieben.", file=sys.stderr)
        return 2

    result = convert(
        **tables,
        skip_groups=args.skip_groups.split(","),
        allergens_confirmed_by=args.allergens_confirmed_by,
    )
    args.out.mkdir(parents=True, exist_ok=True)
    for name, text in result.csv_files().items():
        (args.out / name).write_text(text, encoding="utf-8")
    print(result.as_text())
    _set_aside_aliases(args.out, [row["number"] for row in result.menu])
    print(
        f"Geschrieben nach {args.out}. Weiter: python -m scripts.import_menu "
        f"{args.out} --dry-run"
    )
    return 1 if result.errors else 0


if __name__ == "__main__":
    sys.exit(main())
