"""Kassendateien (.dbf) in die CSV-Dateien nach docs/14 umwandeln (T-4.11).

Aufruf:
    python -m scripts.kasse_to_csv imports/kasse --out imports
    python -m scripts.kasse_to_csv imports/kasse --out imports --allergens-confirmed-by Maxi

Liest nur Kopien: artikel.DBF + .DBT, zutaten.DBF + .DBT, warengrp.dbf,
zutgrp.DBF (Groß- und Kleinschreibung egal). Schreibt menu_items.csv,
item_options.csv und item_allergens.csv in den Zielordner. item_aliases.csv aus
dem Chat bleibt; Zeilen zu Nummern, die die Kasse nicht liefert, liegen in
item_aliases.verworfen.csv und kommen zurück, sobald das Gericht wieder da ist.
Ohne ein einziges Gericht schreibt der Umwandler nichts (Exit 2). Danach wie immer:
    python -m scripts.import_menu imports --dry-run

Exit-Code: 0 geschrieben, 1 geschrieben, aber Artikel mit Fehlern im Bericht
(nicht übernommen), 2 Ordner, Datei oder Format nicht lesbar oder Ziel nicht
schreibbar - Import-Dateien nicht (vollständig) geschrieben, nicht importieren,
3 Import-Dateien geschrieben, Aliase nicht abgeglichen.
Die Logik steckt in api/domain/menu/pos_convert.py.
"""

import argparse
import sys
from pathlib import Path

from api.domain.menu.importer import ALIASES_FILE
from api.domain.menu.pos_convert import convert, split_aliases
from api.domain.menu.pos_dbf import DbfError, Table, read_table
from scripts.call_log import _replace

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
    try:
        data = _find(folder, f"{table}.dbf").read_bytes()
        memo_data = _find(folder, f"{table}.dbt").read_bytes() if memo else None
    except FileNotFoundError:
        raise
    except OSError as exc:
        # Gesperrt (Kasse hat die Datei offen), Ordner statt Datei, Lesefehler:
        # Meldung mit Exit 2 statt Traceback (Codex PR #149).
        raise DbfError(f"{table}: Datei nicht lesbar ({exc.strerror or exc})") from exc
    try:
        return read_table(data, memo_data)
    except DbfError as exc:
        raise DbfError(f"{table}: {exc}") from exc


def _write_outputs(out: Path, files: dict[str, str]) -> None:
    """Die drei Import-Dateien schreiben: erst prüfen, ob eine gesperrt ist
    (unter Windows z. B. in Excel offen), dann jede über eine Kopie tauschen.
    So liegt nie die neue Karte neben alten Optionen (Review T-4.11)."""
    for name in files:
        path = out / name
        if path.exists():
            try:
                with path.open("a", encoding="utf-8"):
                    pass
            except OSError as exc:
                raise OSError(exc.errno, exc.strerror, str(path)) from exc
    for name, text in files.items():
        _replace(out / name, text)


def _reconcile_aliases(out: Path, numbers: list[str]) -> None:
    """Alias-Datei und abgetrennte Zeilen gegen die neue Karte aufteilen."""
    source, side = out / ALIASES_FILE, out / ALIASES_DROPPED
    texts: dict[Path, str | None] = {}
    for path in (source, side):
        try:
            texts[path] = (
                path.read_text(encoding="utf-8-sig") if path.is_file() else None
            )
        except UnicodeDecodeError:
            print(f"Warnung: {path.name} ist nicht UTF-8, Aliase unverändert")
            return
    if texts[source] is None and texts[side] is None:
        return
    split = split_aliases(texts[source], texts[side], numbers)
    if split is None:
        print(
            f"Warnung: {ALIASES_FILE} ohne Spalte number oder mit anderen Spalten "
            f"als {ALIASES_DROPPED}, Aliase unverändert"
        )
        return
    # Erst sichern, dann umschreiben, jede Datei über eine Temp-Datei: scheitert
    # ein Schreiben, ist keine Alias-Zeile verloren (Codex PR #149). Steht eine
    # Zeile danach in beiden Dateien, fasst der nächste Lauf sie zusammen.
    if split.dropped is not None:
        _replace(side, split.dropped)
    _replace(source, split.kept)
    if split.dropped is None:
        # Alle Zeilen sind zurück in der Alias-Datei; die Nebendatei ist leer.
        side.unlink(missing_ok=True)
        return
    print(
        "Warnung: Aliase zu Nummern, die die Kasse nicht liefert, liegen in "
        f"{ALIASES_DROPPED} und kommen zurück, sobald das Gericht wieder "
        f"übernommen wird: {', '.join(split.dropped_numbers)}"
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
        result = convert(
            **tables,
            skip_groups=args.skip_groups.split(","),
            allergens_confirmed_by=args.allergens_confirmed_by,
        )
    except (FileNotFoundError, DbfError) as exc:
        print(f"Fehler: {exc} - nichts geschrieben.", file=sys.stderr)
        return 2
    if not result.menu:
        # Falsche Warengruppen, falscher Ordner, Kopie mitten in einer Änderung:
        # eine leere Karte würde beim Import alles deaktivieren (Review T-4.11).
        print(result.as_text())
        print("Fehler: kein Gericht übernommen - nichts geschrieben.", file=sys.stderr)
        return 2
    try:
        args.out.mkdir(parents=True, exist_ok=True)
        _write_outputs(args.out, result.csv_files())
    except OSError as exc:
        name = Path(exc.filename).name if exc.filename else args.out
        print(
            f"Fehler: {name} nicht schreibbar ({exc.strerror or exc}), Datei "
            "offen? Import-Dateien nicht vollständig geschrieben - nicht importieren, "
            "erneut umwandeln.",
            file=sys.stderr,
        )
        return 2
    print(result.as_text())
    try:
        _reconcile_aliases(args.out, [row["number"] for row in result.menu])
    except OSError as exc:
        print(
            f"Fehler: Import-Dateien geschrieben, Aliase aber nicht vollständig "
            f"abgeglichen ({exc.strerror or exc}). {ALIASES_FILE} und "
            f"{ALIASES_DROPPED} vor dem Import prüfen.",
            file=sys.stderr,
        )
        return 3
    print(
        f"Geschrieben nach {args.out}. Weiter: python -m scripts.import_menu "
        f"{args.out} --dry-run"
    )
    return 1 if result.errors else 0


if __name__ == "__main__":
    sys.exit(main())
