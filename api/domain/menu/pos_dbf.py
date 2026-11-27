"""dBase-Tabellen der Kasse lesen, nur lesend (T-4.11).

Eigener Leser statt einer Bibliothek: die sechs Dateien der Kasse brauchen nur
Text, Zahl, Logisch, Datum und Memo, das Format ist seit Jahrzehnten fest, und
die Tests müssen synthetische Dateien ohnehin selbst schreiben (echte Kassendaten
kommen nie ins Repo). So bleibt es bei der Standardbibliothek.

Nimmt Bytes, nicht Pfade: Datei öffnen ist Sache von `scripts/kasse_to_csv.py`.
Gelöschte Zeilen (Markierung `*`) liefert der Leser mit `deleted=True`; die
Kasse sperrt einen Artikel, indem sie ihn so markiert (docs/14 §Quelle Kasse).
"""

import bisect
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
_EOF = 0x1A
# Gültige Werte eines Logisch-Felds: wahr, falsch, unbekannt (leer oder ?).
_LOGICAL = frozenset(["", "?", *"TtYyFfNn"])
_DELETED = ord("*")
_STATUS = (ord(" "), _DELETED)
# dBase IV: Memoblock beginnt mit FF FF 08 00 und der Länge inklusive Kopf.
_MEMO_IV = b"\xff\xff\x08\x00"
_MEMO_III_END = b"\x1a"
# Versionsbyte der Tabelle -> Memo-Format. Es gilt für jeden Block der .DBT,
# geraten wird nie pro Block (Codex PR #149).
_MEMO_III, _MEMO_IV_FORMAT = "III", "IV"
_MEMO_FORMATS = {0x83: _MEMO_III, 0x8B: _MEMO_IV_FORMAT, 0x8E: _MEMO_IV_FORMAT}


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
    rest = len(data) - (header_len + count * record_len)
    if rest < 0:
        raise DbfError("Datei kürzer als im Kopf angegeben")
    if rest > 1 or (rest == 1 and data[-1] != _EOF):
        # Mehr als das Endezeichen 0x1A, auch eine halbe Zeile: Kopie mitten im
        # Anhängen, die neue Zeile fehlte sonst still (Codex PR #149).
        raise DbfError(
            "Datei länger als im Kopf angegeben, Kopie während einer Änderung?"
        )
    memo_format = _MEMO_FORMATS.get(data[0])
    if any(f.type == "M" for f in fields):
        if memo is None:
            # Ohne .DBT wären die langen Felder still leer (docs/14).
            raise DbfError("Memo-Datei (.DBT) fehlt")
        if memo_format is None:
            raise DbfError(f"Unbekanntes Memo-Format (Version 0x{data[0]:02x})")
    starts = (
        _memo_starts(data, header_len, record_len, count, fields) if memo_format else []
    )

    rows: list[dict[str, str]] = []
    deleted: list[bool] = []
    for index in range(count):
        start = header_len + index * record_len
        record = data[start : start + record_len]
        if record[0] not in _STATUS:
            # Nur " " und "*": ein gesperrter Artikel darf nicht still wieder
            # aktiv werden, weil seine Marke kaputt ist (Codex PR #149).
            raise DbfError(f"Zeile {index + 1}: Löschmarke 0x{record[0]:02x} ungültig")
        is_deleted = record[0] == _DELETED
        deleted.append(is_deleted)
        row: dict[str, str] = {}
        pos = 1
        for field in fields:
            raw = record[pos : pos + field.length]
            pos += field.length
            if field.type == "M":
                # Memo gelöschter Zeilen nie lesen: ihr Zeiger kann ins Leere
                # zeigen und würde sonst den ganzen Import blockieren.
                try:
                    row[field.name] = (
                        ""
                        if is_deleted
                        else _memo(
                            memo or b"", raw, encoding, memo_format or "", starts
                        )
                    )
                except DbfError as exc:
                    raise DbfError(f"Zeile {index + 1}: {field.name}: {exc}") from exc
            else:
                value = raw.decode(encoding).strip()
                if field.type == "L" and value not in _LOGICAL:
                    # Kaputtes WRGSHOWALL schränkte ein Extra sonst still ein
                    # (Codex PR #149).
                    raise DbfError(
                        f"Zeile {index + 1}: {field.name} „{value}“ ist kein "
                        "logischer Wert"
                    )
                row[field.name] = value
        rows.append(row)
    return Table(tuple(fields), encoding, tuple(rows), tuple(deleted))


def _memo_starts(
    data: bytes, header_len: int, record_len: int, count: int, fields: list[Field]
) -> list[int]:
    """Sorted start blocks of every memo, deleted rows too: they own their blocks."""
    spans, pos = [], 1
    for field in fields:
        if field.type == "M":
            spans.append((pos, pos + field.length))
        pos += field.length
    starts = set()
    for index in range(count):
        record = header_len + index * record_len
        for a, b in spans:
            pointer = _pointer(data[record + a : record + b])
            if pointer.isdigit() and int(pointer):
                starts.add(int(pointer))
    return sorted(starts)


def _pointer(raw: bytes) -> str:
    # One reading for _memo and _memo_starts: if they disagreed on padding, a
    # pointer _memo follows would not bound the memo before it (#1074 review).
    return raw.decode("ascii", "replace").strip()


def _memo(
    memo: bytes, pointer: bytes, encoding: str, memo_format: str, starts: list[int]
) -> str:
    text = _pointer(pointer)
    if not text or text.strip("0") == "":
        return ""  # leer oder 0: kein Memo
    if not text.isdigit():
        # Kaputter Zeiger ist kein leeres Memo: sonst verschwänden z. B. die
        # Warengruppen eines Extras still (Codex PR #149).
        raise DbfError(f"Memo-Zeiger „{text}“ unlesbar")
    if len(memo) < 32:
        raise DbfError("Memo-Datei zu kurz")
    # Blockgröße steht bei dBase IV in Byte 20-21; dBase III kennt nur 512,
    # dort sind die Bytes frei und dürfen nicht gelesen werden.
    # Little-Endian, an den echten Kassendateien geprüft: Byte 20-21 = 00 04
    # (1024), Memo-Länge 56 00 00 00 (86). Big-Endian ergäbe 4 und 1,4 Mrd.
    # (Codex PR #149 schlug Big-Endian vor, widerlegt).
    if memo_format == _MEMO_IV_FORMAT:
        block_size = struct.unpack("<H", memo[20:22])[0] or 512
    else:
        block_size = 512
    start = int(text) * block_size
    if start >= len(memo):
        raise DbfError(f"Memo-Block {text} fehlt in der .DBT")
    # A memo ends where the next memo starts, never in another record's text
    # (Codex PR #149), in both formats. A next start beyond the file is a cut
    # copy and is reported as one below.
    # shortcut: blocks no record points at (an edited memo's old blocks, rows a
    # PACK removed) and old text in a memo's own last block are not known, so a
    # damaged memo before them still reads that text; upgrade with a real
    # dBase III source (the register writes dBase IV, where the length decides).
    after = bisect.bisect_right(starts, int(text))
    nxt = starts[after] if after < len(starts) else None
    bound = nxt * block_size if nxt is not None else len(memo)
    if bound > len(memo):
        nxt, bound = None, len(memo)
    if memo_format == _MEMO_IV_FORMAT:
        if memo[start : start + 4] != _MEMO_IV:
            raise DbfError(f"Memo-Block {text} ohne dBase-IV-Kennung, .DBT beschädigt")
        if start + 8 > len(memo):
            raise DbfError(f"Memo-Block {text} abgeschnitten")
        length = struct.unpack("<I", memo[start + 4 : start + 8])[0]
        # Halbes Memo ist kein kürzeres Memo: sonst fehlten z. B. Warengruppen
        # eines Extras still (Codex PR #149).
        if length < 8 or start + length > len(memo):
            raise DbfError(f"Memo-Block {text} abgeschnitten")
        if start + length > bound:
            raise DbfError(
                f"memo block {text} runs into the next memo (block {nxt}), .DBT damaged"
            )
        body = memo[start + 8 : start + length]
    else:
        end = memo.find(_MEMO_III_END, start, bound)
        if end == -1 and nxt is not None:
            raise DbfError(
                f"memo block {text} has no end marker before the next memo "
                f"(block {nxt}), .DBT damaged"
            )
        if end == -1:
            raise DbfError(f"Memo-Block {text} ohne Ende, .DBT abgeschnitten")
        body = memo[start:end]
    # 0x8D 0x0A ist ein weicher Umbruch des Memo-Editors, kein "ì" (Review T-4.11).
    value = body.replace(b"\x8d\x0a", b"\r\n").decode(encoding)
    # "002\x00" matches no product group and no allergen word, so the extra or
    # the carrier check would drop out silently (#1074 review); like the NUL
    # check of PR #169 the file is refused instead.
    bad = next(
        (c for c in value if (c < " " and c not in "\t\r\n") or c == "\x7f"), None
    )
    if bad is not None:
        raise DbfError(f"memo block {text} holds control byte 0x{ord(bad):02x}")
    return value.strip()
