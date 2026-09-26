"""dBase-Tabellen der Kasse lesen, nur lesend (T-4.11).

Eigener Leser statt einer Bibliothek: die sechs Dateien der Kasse brauchen nur
Text, Zahl, Logisch, Datum und Memo, das Format ist seit Jahrzehnten fest, und
die Tests müssen synthetische Dateien ohnehin selbst schreiben (echte Kassendaten
kommen nie ins Repo). So bleibt es bei der Standardbibliothek.

Nimmt Bytes, nicht Pfade: Datei öffnen ist Sache von `scripts/kasse_to_csv.py`.
Gelöschte Zeilen (Markierung `*`) liefert der Leser mit `deleted=True`; die
Kasse sperrt einen Artikel, indem sie ihn so markiert (docs/14 §Quelle Kasse).
"""

import struct
from dataclasses import dataclass

# Byte 29 im Kopf: Sprachtreiber. Nur, was bekannt ist; alles andere ist ein
# Fehler statt einer geratenen Codepage, sonst würden Umlaute still falsch.
_CODEPAGES = {
    0x01: "cp437",
    0x02: "cp850",
    0x03: "cp1252",
    0x09: "cp437",
    0x0A: "cp850",
    0x0F: "cp437",  # German OEM, so schreibt die Kasse
    0x10: "cp850",
    0x57: "cp1252",
    0x58: "cp1252",
}
_HEADER_END = 0x0D
_DELETED = ord("*")
# dBase IV: Memoblock beginnt mit FF FF 08 00 und der Länge inklusive Kopf.
_MEMO_IV = b"\xff\xff\x08\x00"
_MEMO_III_END = b"\x1a"


class DbfError(ValueError):
    """Datei ist keine lesbare dBase-Tabelle."""


@dataclass(frozen=True)
class Field:
    name: str
    type: str
    length: int
    decimals: int


@dataclass(frozen=True)
class Table:
    fields: tuple[Field, ...]
    encoding: str
    rows: tuple[dict[str, str], ...]
    deleted: tuple[bool, ...]

    def live(self) -> list[dict[str, str]]:
        """Nur die nicht gelöschten Zeilen."""
        return [r for r, d in zip(self.rows, self.deleted, strict=True) if not d]


def read_table(data: bytes, memo: bytes | None = None) -> Table:
    """Tabelle lesen. Alle Werte als Text, ohne Rand-Leerzeichen; Memo aufgelöst.

    Zahlen bleiben Text ("  6.50" -> "6.50"): Geld wird erst beim Umwandeln zu
    Cent, nie über float (CLAUDE.md §8). Jede kaputte Datei endet in DbfError,
    damit der Aufrufer eine Meldung statt eines Tracebacks zeigt (Review T-4.11).
    """
    try:
        return _read(data, memo)
    except UnicodeDecodeError as exc:
        raise DbfError(
            f"Zeichensatz passt nicht zum Inhalt (Byte 0x{exc.object[exc.start]:02x})"
        ) from exc
    except (IndexError, struct.error) as exc:
        raise DbfError("Datei beschädigt oder abgeschnitten") from exc


def _read(data: bytes, memo: bytes | None) -> Table:
    if len(data) < 32:
        raise DbfError("Datei zu kurz für einen dBase-Kopf")
    count, header_len, record_len = struct.unpack("<IHH", data[4:12])
    driver = data[29]
    encoding = _CODEPAGES.get(driver)
    if encoding is None:
        raise DbfError(f"Unbekannter Zeichensatz (Sprachtreiber 0x{driver:02x})")

    if header_len > len(data):
        raise DbfError("Datei kürzer als im Kopf angegeben")
    fields: list[Field] = []
    offset = 32
    while offset < header_len and data[offset] != _HEADER_END:
        raw = data[offset : offset + 32]
        name = raw[:11].split(b"\0", 1)[0].decode("ascii")
        fields.append(Field(name, chr(raw[11]), raw[16], raw[17]))
        offset += 32
    if sum(f.length for f in fields) + 1 != record_len:
        raise DbfError("Satzlänge passt nicht zu den Feldern")
    if header_len + count * record_len > len(data):
        raise DbfError("Datei kürzer als im Kopf angegeben")
    if memo is None and any(f.type == "M" for f in fields):
        # Ohne .DBT wären die langen Felder still leer (docs/14).
        raise DbfError("Memo-Datei (.DBT) fehlt")

    rows: list[dict[str, str]] = []
    deleted: list[bool] = []
    for index in range(count):
        start = header_len + index * record_len
        record = data[start : start + record_len]
        deleted.append(record[0] == _DELETED)
        row: dict[str, str] = {}
        pos = 1
        for field in fields:
            raw = record[pos : pos + field.length]
            pos += field.length
            if field.type == "M":
                row[field.name] = _memo(memo or b"", raw, encoding)
            else:
                row[field.name] = raw.decode(encoding).strip()
        rows.append(row)
    return Table(tuple(fields), encoding, tuple(rows), tuple(deleted))


def _memo(memo: bytes, pointer: bytes, encoding: str) -> str:
    text = pointer.decode("ascii", "replace").strip()
    if not text or text.strip("0") == "":
        return ""  # leer oder 0: kein Memo
    if not text.isdigit():
        # Kaputter Zeiger ist kein leeres Memo: sonst verschwänden z. B. die
        # Warengruppen eines Extras still (Codex PR #149).
        raise DbfError(f"Memo-Zeiger „{text}“ unlesbar")
    if len(memo) < 32:
        raise DbfError("Memo-Datei zu kurz")
    # Blockgröße steht bei dBase IV in Byte 20-21; dBase III kennt nur 512.
    block_size = struct.unpack("<H", memo[20:22])[0] or 512
    start = int(text) * block_size
    if start >= len(memo):
        raise DbfError(f"Memo-Block {text} fehlt in der .DBT")
    if memo[start : start + 4] == _MEMO_IV:
        length = struct.unpack("<I", memo[start + 4 : start + 8])[0]
        body = memo[start + 8 : start + length]
    else:
        end = memo.find(_MEMO_III_END, start)
        body = memo[start : end if end != -1 else len(memo)]
    return body.decode(encoding).strip()
