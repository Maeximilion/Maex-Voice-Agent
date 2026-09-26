"""Synthetische dBase-Dateien für Tests (T-4.11). Echte Kassendaten nie ins Repo.

Schreibt wie die Kasse: dBase IV mit Memo (.DBT, 1024-Byte-Blöcke), Zeichensatz
German OEM (cp437). Nur die Typen, die der Leser braucht.
"""

import struct

BLOCK = 1024


def write_dbf(
    fields: list[tuple[str, str, int, int]],
    rows: list[dict[str, str]],
    *,
    deleted: tuple[int, ...] = (),
    driver: int = 0x0F,
    encoding: str = "cp437",
    memo_iv: bool = True,
) -> tuple[bytes, bytes | None]:
    """(DBF, DBT oder None). fields: (Name, Typ, Länge, Nachkommastellen)."""
    has_memo = any(t == "M" for _, t, _, _ in fields)
    memo = bytearray(BLOCK if memo_iv else 512)
    header_len = 32 + 32 * len(fields) + 1
    record_len = 1 + sum(length for _, _, length, _ in fields)
    head = bytearray(32)
    head[0] = 0x8B if has_memo else 0x03
    head[4:12] = struct.pack("<IHH", len(rows), header_len, record_len)
    head[29] = driver
    for name, typ, length, dec in fields:
        desc = bytearray(32)
        desc[: len(name)] = name.encode("ascii")
        desc[11] = ord(typ)
        desc[16] = length
        desc[17] = dec
        head += desc
    head += b"\x0d"
    body = bytearray()
    for index, row in enumerate(rows):
        body += b"*" if index in deleted else b" "
        for name, typ, length, _ in fields:
            value = row.get(name, "")
            if typ == "M":
                body += _memo_block(memo, value, encoding, memo_iv).rjust(length)
            elif typ == "N":
                body += value.encode("ascii").rjust(length)
            else:
                body += value.encode(encoding).ljust(length)[:length]
    dbt = None
    if has_memo:
        if memo_iv:
            memo[20:22] = struct.pack("<H", BLOCK)
        memo[0:4] = struct.pack("<I", len(memo) // (BLOCK if memo_iv else 512))
        dbt = bytes(memo)
    return bytes(head + body + b"\x1a"), dbt


def _memo_block(memo: bytearray, text: str, encoding: str, memo_iv: bool) -> bytes:
    if not text:
        return b""
    size = BLOCK if memo_iv else 512
    block = len(memo) // size
    data = text.encode(encoding)
    if memo_iv:
        chunk = b"\xff\xff\x08\x00" + struct.pack("<I", len(data) + 8) + data
    else:
        chunk = data + b"\x1a\x1a"
    chunk += b"\0" * (-len(chunk) % size)
    memo += chunk
    return str(block).encode("ascii")
