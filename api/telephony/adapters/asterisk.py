"""Media channel of Asterisk, the wire format only (docs/20 §2, spike T-10.1).

Asterisk dials `WebSocket/<connection>/c(slin16)f(json)` and connects to the voice
service once per call. Call audio travels as BINARY frames of raw samples, control
as TEXT frames with one JSON object each: events from Asterisk, commands from us.
Audio we send is queued and paced by Asterisk (about 1000 frames, 20 s), which is
why a sentence can be sent in one go and stopped with `FLUSH_MEDIA`.

Checked against Asterisk 22.10.1 (recorded frames in `api/tests/fixtures/`). The
JSON control format needs 22.8 or later. The adapter that implements
`TelephonyPort` on top of this comes with T-1.11.
"""

import json
from dataclasses import dataclass
from typing import Any

# Events, Asterisk to us.
MEDIA_START = "MEDIA_START"
DTMF_END = "DTMF_END"
STATUS = "STATUS"
QUEUE_DRAINED = "QUEUE_DRAINED"
MEDIA_XOFF = "MEDIA_XOFF"
MEDIA_XON = "MEDIA_XON"
MEDIA_BUFFERING_COMPLETED = "MEDIA_BUFFERING_COMPLETED"
MEDIA_MARK_PROCESSED = "MEDIA_MARK_PROCESSED"

# Commands, us to Asterisk. Case-sensitive on the wire.
COMMANDS = frozenset(
    {
        "ANSWER",
        "HANGUP",
        "START_MEDIA_BUFFERING",
        "STOP_MEDIA_BUFFERING",
        "FLUSH_MEDIA",
        "PAUSE_MEDIA",
        "CONTINUE_MEDIA",
        "MARK_MEDIA",
        "GET_STATUS",
        "REPORT_QUEUE_DRAINED",
        "SET_MEDIA_DIRECTION",
    }
)

# A larger message closes the socket and with it the call.
MAX_MESSAGE_BYTES = 65500

_SAMPLE_RATES = {"slin": 8000, "slin16": 16000}


class ProtocolError(ValueError):
    """A frame that is not what the media channel's JSON format allows."""


@dataclass(frozen=True)
class Event:
    name: str
    data: dict[str, Any]


def parse_event(text: str) -> Event:
    """One TEXT frame from Asterisk. An event name this code does not know still
    parses, a later Asterisk may send more; anything else raises `ProtocolError`."""
    try:
        frame = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProtocolError("control frame is not JSON") from exc
    if not isinstance(frame, dict) or not isinstance(frame.get("event"), str):
        raise ProtocolError("control frame carries no event name")
    name = frame.pop("event")
    return Event(name, frame)


def command(name: str, **params: Any) -> str:
    """The TEXT frame for one command."""
    if name not in COMMANDS:
        raise ValueError(f"unknown media command: {name}")
    return json.dumps({"command": name, **params})


def sample_rate(media_format: str) -> int:
    """Samples per second of a format we dial. Raw 16 bit mono in both."""
    try:
        return _SAMPLE_RATES[media_format]
    except KeyError:
        raise ProtocolError(f"unexpected media format: {media_format}") from None
