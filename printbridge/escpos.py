"""Kuechenbon als ESC/POS-Bytes fuer Epson-Bondrucker (getestet gegen TM-T20II-Befehlssatz).

Nur Standardbibliothek: die Bruecke laeuft auf einem Rechner im Lokal, dort soll
nichts ausser Python noetig sein.

Inhalt nach Datensparsamkeit: Abholcode, Zeit, Name, Positionen mit Optionen und
Hinweis, Summe. Keine Telefonnummer - die braucht die Kueche nicht.
"""

import textwrap
import unicodedata
from datetime import UTC, datetime
from typing import Any

ESC = b"\x1b"
GS = b"\x1d"

INIT = ESC + b"@"
# Zeichentabelle PC858: Umlaute, scharfes S und Euro.
CODEPAGE = ESC + b"t\x13"
ENCODING = "cp858"
BOLD_ON, BOLD_OFF = ESC + b"E\x01", ESC + b"E\x00"
BIG_ON, BIG_OFF = ESC + b"!\x30", ESC + b"!\x00"  # doppelt hoch und breit
INVERT_ON, INVERT_OFF = GS + b"B\x01", GS + b"B\x00"
FEED = ESC + b"d\x04"
CUT = GS + b"V\x42\x00"  # vorschieben und teilweise schneiden

# 80-mm-Papier, Schrift A: 48 Zeichen je Zeile.
DEFAULT_WIDTH = 48

TYPES = {"pickup": "ABHOLUNG", "delivery": "LIEFERUNG"}
REASONS = {
    "wrong_item": "falsches Gericht",
    "wrong_quantity": "falsche Menge",
    "wrong_address": "falsche Adresse",
    "other": "sonstiges",
}


def printable(text: str) -> str:
    """Auf PC858 abbilden. Was die Tabelle nicht kennt, verliert seine Akzente.

    "Phở Bò" wird "Pho Bo" statt "Ph? B?": die Kueche soll den Namen lesen
    koennen. Umlaute bleiben, die kennt PC858.
    """
    out = []
    for char in text:
        try:
            char.encode(ENCODING)
            out.append(char)
            continue
        except UnicodeEncodeError:
            pass
        base = "".join(
            c
            for c in unicodedata.normalize("NFKD", char)
            if not unicodedata.combining(c)
        )
        try:
            base.encode(ENCODING)
            out.append(base)
        except UnicodeEncodeError:
            out.append("?")
    return "".join(out)


def _line(text: str) -> bytes:
    return printable(text).encode(ENCODING) + b"\n"


def _wrapped(text: str, width: int, indent: str = "") -> bytes:
    lines = textwrap.wrap(
        text, width=width, initial_indent=indent, subsequent_indent=indent + "  "
    ) or [indent]
    return b"".join(_line(line) for line in lines)


def _euro(cents: int | None) -> str:
    cents = cents or 0
    return f"{cents // 100},{cents % 100:02d} EUR"


def local_now() -> datetime:
    """Jetzt in der Ortszeit des Rechners im Lokal (dort stimmt sie ohne Zeitzonendaten)."""
    return datetime.now(UTC).astimezone()


def _clock(iso: str | None) -> str | None:
    if not iso:
        return None
    # Ortszeit des Rechners im Lokal: auch unter Windows ohne Zeitzonendaten.
    return datetime.fromisoformat(iso).astimezone().strftime("%H:%M")


def _option(option: Any) -> str:
    if isinstance(option, dict):
        return str(option.get("option") or option.get("name") or "")
    return str(option)


def render(
    ticket: dict[str, Any],
    width: int = DEFAULT_WIDTH,
    printed_at: datetime | None = None,
) -> bytes:
    """Ein Bon aus dem Ereignis `order.confirmed` (docs/04 §confirm)."""
    out = [INIT, CODEPAGE]
    reason = ticket.get("correction_reason")
    if reason:
        out += [BIG_ON, INVERT_ON, _line(" KORREKTUR "), INVERT_OFF, BIG_OFF]
        out.append(_line(f"Grund: {REASONS.get(reason, reason)}"))
        out.append(_line("Ersetzt den vorigen Bon dieser Bestellung."))
    kind = TYPES.get(ticket.get("type") or "", str(ticket.get("type") or "").upper())
    code = ticket.get("pickup_code") or ""
    out += [BIG_ON, _wrapped(f"{kind} {code}".strip(), width // 2), BIG_OFF]
    # Der Server liefert die Uhrzeiten in der Ortszeit des Betriebs mit; nur
    # ohne sie (Probebon) rechnet die Bruecke mit der Uhr ihres Rechners.
    ready = ticket.get("ready_time") or _clock(ticket.get("ready_at"))
    if ready:
        out += [BOLD_ON, _line(f"Fertig um {ready}"), BOLD_OFF]
    if ticket.get("customer_name"):
        out.append(_wrapped(f"Name: {ticket['customer_name']}", width))
    out.append(_line("-" * width))
    for item in ticket.get("items") or []:
        label = f"{item.get('quantity', 1)}x {item.get('number') or ''} {item.get('name') or ''}"
        out += [BOLD_ON, _wrapped(" ".join(label.split()), width), BOLD_OFF]
        for option in item.get("options") or []:
            text = _option(option)
            if text:
                out.append(_wrapped(f"+ {text}", width, indent="   "))
        if item.get("note"):
            # Hinweise (etwa eine Allergie) fett: die Kueche darf sie nicht ueberlesen.
            out += [
                BOLD_ON,
                _wrapped(f"! {item['note']}", width, indent="   "),
                BOLD_OFF,
            ]
    out.append(_line("-" * width))
    out.append(_line(f"Summe {_euro(ticket.get('total_cents'))}".rjust(width)))
    stamp = ticket.get("print_time") or (printed_at or local_now()).strftime("%H:%M")
    revision = int(ticket.get("revision") or 0)
    footer = f"Gedruckt {stamp}" + (f" - Stand {revision}" if revision else "")
    out.append(_line(footer))
    out += [FEED, CUT]
    return b"".join(out)
