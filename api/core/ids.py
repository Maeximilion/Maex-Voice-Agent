"""Generierung von deterministischen Schlüsseln für Idempotenz und Tracing.

Idempotenzschlüssel sind deterministisch und minimal: mit dem gleichen Schlüssel
soll die gleiche Operation genau einmal ausgeführt werden, sonst ist es ein Fehler.
"""

import hashlib
import json
from typing import Any


def make_idempotency_key(call_id: str, tool_name: str, **fields: Any) -> str:
    """Deterministischer Schlüssel für Idempotenzbehandlung.

    Aus call_id, Tool-Name und den relevanten Feldern wird ein stabiler SHA-256-Hash
    gebildet. Zwei Requests mit denselben Werten erzeugen denselben Schlüssel.

    Verwendung: create_reservation mit guest_name, phone, party_size, reserved_for
    → make_idempotency_key(call_id, "create_reservation", guest_name=..., phone=..., ...)
    """
    parts = [call_id, tool_name]
    for key in sorted(fields.keys()):
        v = fields[key]
        if isinstance(v, dict):
            v = json.dumps(v, sort_keys=True, default=str)
        else:
            v = str(v)
        parts.append(f"{key}={v}")
    combined = "|".join(parts)
    return hashlib.sha256(combined.encode()).hexdigest()


__all__ = ["make_idempotency_key"]
