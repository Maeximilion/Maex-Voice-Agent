"""Fake adapter: plays calls from files instead of a phone line (docs/11 §telephony).

It stands in for a voice platform: it feeds a call's events to `CallEvents` and
records what it was asked to do. Like a real line, it stops passing on what the
caller says once the call was hung up or transferred, and it always reports the
end of the call.

File format: the eval case format (docs/08 §1), so every case in `evals/cases/`
plays as a phone call. A customer entry carries `text`, or instead:

- `"dtmf": "4#"`: keys pressed on the phone
- `"hangup": true`: the caller hangs up here

Optional top-level fields: `caller_id` (withheld when missing) and `session_id`.
Agent entries are ignored; the agent speaks anew.
"""

import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from api.telephony.port import CallEvents

CUSTOMER = "customer"


@dataclass(frozen=True)
class Action:
    kind: str  # say, transfer, hangup, recording
    session_id: str
    value: str | None = None


def load_call(path: Path) -> dict[str, Any]:
    case = json.loads(path.read_text(encoding="utf-8"))
    if not any(_is_caller_event(entry) for entry in case.get("transcript", [])):
        # A file without anything the caller does tests only the greeting; a
        # mistake in the file should fail loudly instead.
        raise ValueError(f"{path}: no customer event in the transcript")
    return case


class FakeTelephony:
    """Implements `port.TelephonyPort` and records every action."""

    def __init__(self) -> None:
        self.actions: list[Action] = []
        self._caller_ids: dict[str, str | None] = {}
        self._open: set[str] = set()

    def open(self, session_id: str, *, caller_id: str | None = None) -> None:
        """A line rings: from here the handler may ask for its number."""
        self._caller_ids[session_id] = caller_id
        self._open.add(session_id)

    def play(self, case: dict[str, Any], events: CallEvents) -> str:
        session_id = case.get("session_id") or f"fake-{uuid.uuid4().hex[:8]}"
        self.open(session_id, caller_id=case.get("caller_id"))
        events.on_call_started(session_id)
        for entry in case.get("transcript", []):
            if session_id not in self._open or entry.get("hangup"):
                break
            if not _is_caller_event(entry):
                continue
            if entry.get("dtmf"):
                events.on_dtmf(session_id, entry["dtmf"])
            else:
                events.on_user_turn(session_id, entry["text"])
        self._open.discard(session_id)
        events.on_call_ended(session_id)
        return session_id

    # -- TelephonyPort -------------------------------------------------------------

    def caller_id(self, session_id: str) -> str | None:
        return self._caller_ids.get(session_id)

    def say(self, session_id: str, text: str) -> None:
        self.actions.append(Action("say", session_id, text))

    def transfer(self, session_id: str, target: str) -> None:
        self.actions.append(Action("transfer", session_id, target))
        self._open.discard(session_id)

    def hangup(self, session_id: str) -> None:
        self.actions.append(Action("hangup", session_id))
        self._open.discard(session_id)

    def start_recording(self, session_id: str) -> None:
        self.actions.append(Action("recording", session_id))

    # -- reading back ----------------------------------------------------------------

    def says(self, session_id: str) -> list[str]:
        return self._values("say", session_id)

    def transfers(self, session_id: str) -> list[str]:
        return self._values("transfer", session_id)

    def hangups(self, session_id: str) -> list[Action]:
        return [
            a for a in self.actions if a.kind == "hangup" and a.session_id == session_id
        ]

    def recordings(self, session_id: str) -> list[Action]:
        return [
            a
            for a in self.actions
            if a.kind == "recording" and a.session_id == session_id
        ]

    def _values(self, kind: str, session_id: str) -> list[str]:
        return [
            a.value or ""
            for a in self.actions
            if a.kind == kind and a.session_id == session_id
        ]


def _is_caller_event(entry: dict[str, Any]) -> bool:
    return entry.get("role") == CUSTOMER and bool(
        entry.get("text") or entry.get("dtmf") or entry.get("hangup")
    )
