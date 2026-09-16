"""Kennungen: UUIDs und deterministische Idempotenz-Schlüssel (docs/11 §core)."""

import hashlib
import uuid


def new_id() -> uuid.UUID:
    return uuid.uuid4()


def idempotency_key(call_id: uuid.UUID | str, tool: str, *parts: object) -> str:
    """Gleicher Anruf, gleiches Tool, gleiche Eingabe → gleicher Schlüssel.

    Für Aufrufer, die keinen eigenen Schlüssel mitbringen (Simulator, Agent-Kern).
    Die Voice-Plattform darf ihren eigenen schicken.
    """
    raw = "\x1f".join([str(call_id), tool, *(str(p) for p in parts)])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
