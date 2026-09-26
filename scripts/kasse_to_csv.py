"""Kassendateien (.dbf) in die CSV-Dateien nach docs/14 umwandeln (T-4.11).

Aufruf:
    python -m scripts.kasse_to_csv imports/kasse --out imports
    python -m scripts.kasse_to_csv imports/kasse --out imports --allergens-confirmed-by Maxi

Liest nur Kopien: artikel.DBF + .DBT, zutaten.DBF + .DBT, warengrp.dbf,
zutgrp.DBF (Groß- und Kleinschreibung egal). Schreibt menu_items.csv,
item_options.csv und item_allergens.csv in den Zielordner. item_aliases.csv aus
dem Chat bleibt; Zeilen zu Nummern, die die Kasse nicht liefert, liegen in
item_aliases.verworfen.csv und kommen zurück, sobald das Gericht wieder da ist.

Der Import liest den Ordner als einen Satz Dateien. Deshalb alles oder nichts:
erst wird alles gelesen und berechnet, dann jede Datei als Kopie daneben
geschrieben, erst danach werden alle getauscht. Ohne ein einziges Gericht
schreibt der Umwandler nichts. Danach wie immer:
    python -m scripts.import_menu imports --dry-run

Exit-Code: 0 geschrieben, 1 geschrieben, aber Artikel mit Fehlern im Bericht
(nicht übernommen), 2 nichts geschrieben (Ordner, Datei oder Format nicht
lesbar, Ziel gesperrt, z. B. in Excel offen). Die Logik steckt in
api/domain/menu/pos_convert.py.
"""

import argparse
import os
import sys
from dataclasses import dataclass, field
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


class AliasFileError(OSError):
    """Alias-Datei nicht lesbar für den Import: dann wird nichts geschrieben."""


class PartialPublishError(Exception):
    """Tausch gescheitert und Zurückrollen auch: der Ordner ist gemischt."""


@dataclass
class Output:
    """Was geschrieben und was entfernt wird, dazu Hinweise für den Bericht."""

    write: dict[str, str] = field(default_factory=dict)
    remove: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def plan_aliases(out: Path, numbers: list[str], output: Output) -> None:
    """Alias-Datei und abgetrennte Zeilen gegen die neue Karte aufteilen, nur
    lesen. Ein Lesefehler (gesperrt) geht als OSError an den Aufrufer, bevor
    irgendeine Datei geschrieben ist."""
    source, side = out / ALIASES_FILE, out / ALIASES_DROPPED
    texts: dict[Path, str | None] = {}
    for path in (source, side):
        try:
            texts[path] = (
                path.read_text(encoding="utf-8-sig") if path.is_file() else None
            )
        except UnicodeDecodeError as exc:
            # import_menu liest die Datei als UTF-8 und bräche daran ab: ein
            # Satz Dateien, den der Import nicht lesen kann, wird nicht
            # geschrieben (Codex PR #149).
            raise AliasFileError(
                0, f"{path.name} ist nicht UTF-8, als UTF-8 speichern", str(path)
            ) from exc
    if texts[source] is None and texts[side] is None:
        return
    split = split_aliases(texts[source], texts[side], numbers)
    if split is None:
        output.notes.append(
            f"Warnung: {ALIASES_FILE} ohne Spalte number oder mit anderen Spalten "
            f"als {ALIASES_DROPPED}, Aliase unverändert"
        )
        return
    output.write[ALIASES_FILE] = split.kept
    if split.dropped is None:
        # Alle Zeilen sind zurück in der Alias-Datei; die Nebendatei ist leer.
        if side.exists():
            output.remove.append(ALIASES_DROPPED)
        return
    output.write[ALIASES_DROPPED] = split.dropped
    output.notes.append(
        "Warnung: Aliase zu Nummern, die die Kasse nicht liefert, liegen in "
        f"{ALIASES_DROPPED} und kommen zurück, sobald das Gericht wieder "
        f"übernommen wird: {', '.join(split.dropped_numbers)}"
    )


def publish(out: Path, output: Output) -> None:
    """Alles oder nichts, so weit das Dateisystem es zulässt (Codex PR #149):
    1. jede vorhandene Zieldatei probeweise öffnen - gesperrt (Excel) fällt auf,
       bevor etwas getauscht ist;
    2. jede Datei als Kopie daneben schreiben - Platte voll fällt auf, bevor
       etwas getauscht ist;
    3. erst dann tauschen, jede alte Datei vorher nach .bak; scheitert ein
       Tausch, wird alles Getauschte zurückgerollt.
    Der Ordner bleibt also wie er war, außer das Zurückrollen selbst scheitert
    (PartialPublishError, eigene Meldung)."""
    targets = [out / name for name in [*output.write, *output.remove]]
    for path in targets:
        if path.exists():
            try:
                with path.open("a", encoding="utf-8"):
                    pass
            except OSError as exc:
                raise OSError(exc.errno, exc.strerror, str(path)) from exc
    staged: list[tuple[Path, Path]] = []
    try:
        for name, text in output.write.items():
            path = out / name
            tmp = path.with_name(path.name + ".tmp")
            staged.append((tmp, path))
            tmp.write_text(text, encoding="utf-8")
    except BaseException:
        for tmp, _ in staged:
            tmp.unlink(missing_ok=True)
        raise
    # Jede alte Datei erst nach .bak, dann die neue an ihren Platz. Scheitert
    # ein Schritt, wird alles Getauschte zurückgerollt (Codex PR #149).
    moves = [(tmp, path) for tmp, path in staged]
    moves += [(None, out / name) for name in output.remove]
    done: list[tuple[Path, Path | None]] = []
    try:
        for tmp, path in moves:
            backup = None
            if path.exists():
                backup = path.with_name(path.name + ".bak")
                os.replace(path, backup)
            done.append((path, backup))
            if tmp is not None:
                os.replace(tmp, path)
    except BaseException:
        failed = []
        for path, backup in reversed(done):
            try:
                if backup is not None:
                    os.replace(backup, path)
                else:
                    path.unlink(missing_ok=True)
            except OSError:
                failed.append(path.name)
        for tmp, _ in staged:
            tmp.unlink(missing_ok=True)
        if failed:
            raise PartialPublishError(
                "Zurückrollen gescheitert bei " + ", ".join(failed)
            ) from None
        raise
    for _, backup in done:
        if backup is not None:
            backup.unlink(missing_ok=True)


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

    output = Output(write=result.csv_files())
    try:
        args.out.mkdir(parents=True, exist_ok=True)
        plan_aliases(args.out, [row["number"] for row in result.menu], output)
        publish(args.out, output)
    except PartialPublishError as exc:
        print(
            f"Fehler: {exc}. Der Ordner ist gemischt: nicht importieren, die "
            ".bak-Dateien zurückbenennen oder erneut umwandeln.",
            file=sys.stderr,
        )
        return 2
    except AliasFileError as exc:
        print(
            f"Fehler: {exc.strerror}. Nichts geschrieben, der Ordner ist auf dem "
            "alten Stand.",
            file=sys.stderr,
        )
        return 2
    except OSError as exc:
        name = Path(exc.filename).name if exc.filename else args.out
        print(
            f"Fehler: {name} nicht les- oder schreibbar ({exc.strerror or exc}), "
            "Datei offen? Nichts geschrieben, der Ordner ist auf dem alten Stand.",
            file=sys.stderr,
        )
        return 2
    print(result.as_text())
    for note in output.notes:
        print(note)
    print(
        f"Geschrieben nach {args.out}. Weiter: python -m scripts.import_menu "
        f"{args.out} --dry-run"
    )
    return 1 if result.errors else 0


if __name__ == "__main__":
    sys.exit(main())
