"""Media channel of Asterisk: the wire format and the caller's leg on it (docs/20 §2).

Asterisk dials `WebSocket/<connection>/nc(slin16)f(json)` and connects to the
voice service once per call. Call audio travels as BINARY frames of raw samples,
control as TEXT frames with one JSON object each: events from Asterisk, commands
from us. Audio we send is queued and paced by Asterisk.

`MediaLine` implements `speech.Line` on one such connection (T-1.11), and hands
what Asterisk reports to the call session. What it relies on was seen on the wire
(Asterisk 22.10.1, frames in `api/tests/fixtures/asterisk_media_*.jsonl`):

- With `n` in the dial string the channel is not answered until `ANSWER`: the
  caller hears ringing, and a call the core sends to the team before its first
  sentence is never answered by us.
- `MARK_MEDIA` puts a mark into the queue; `MEDIA_MARK_PROCESSED` says that
  everything before it was played. That is how a sentence is reported as heard,
  to the frame. `QUEUE_DRAINED` is not used for it: a "drained" for one sentence
  can arrive after the next one was queued.
- `FLUSH_MEDIA` drops the marks queued before it; they are never reported.
- The queue holds about 1000 frames (20 s). What is sent beyond that is dropped
  without a word, and the mark behind it is still reported: 25 s sent in one go
  ended with the mark at 20.5 s. So a long read-back would count as heard while
  its end was never played (CLAUDE.md §2 rule 3). The line therefore keeps no
  more than `IN_FLIGHT_CHUNKS` messages of audio unplayed at Asterisk, each one
  acknowledged by a mark of its own, and holds the rest back.
- `HANGUP` ends our leg; the dialplan goes on (`Dial` option `g`) and asks the
  voice service what the call came to (`telephony/router.py`).

The JSON control format needs 22.8 or later. Standard library only: the probe of
the spike T-10.1 imports this module on the host without the API's packages.
"""

import json
import logging
import re
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from api.telephony.session import CallSession

# Events, Asterisk to us.
MEDIA_START = "MEDIA_START"
DTMF_END = "DTMF_END"
QUEUE_DRAINED = "QUEUE_DRAINED"
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
# Messages of audio sent and not yet played, at most. One holds 102 frames of 20
# ms at 16 kHz, so three are about 6 s: far below the 1000 frames Asterisk keeps,
# and enough that the next message is there long before the queue runs dry.
IN_FLIGHT_CHUNKS = 3
# The format we dial: raw 16 bit mono at 16 kHz, as the call session expects.
MEDIA_FORMAT = "slin16"
# A team number or another number to hand the call to: E.164 or national digits.
# The dialplan checks it again before it dials.
_TARGET = re.compile(r"\+?[0-9]{3,20}")
# Marks of the session's sentences, and of the messages of audio, on the wire.
_SENTENCE = "s"
_CHUNK = "c"

_SAMPLE_RATES = {"slin": 8000, "slin16": 16000}

logger = logging.getLogger(__name__)


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


class MediaLine:
    """`speech.Line` on one connection of the media channel, and the way back:
    what Asterisk sends goes to the call session (`attach`).

    `send` puts one frame on the connection. It is called from the worker of the
    call and from the connection, in the order frames must go out, and must not
    block (docs/20 §4: the line hands a command over and returns); the router
    queues the frame for its writer. `ended` learns how our leg ends,
    "hangup" or "transfer <target>", before `HANGUP` goes out: the dialplan asks
    for it the moment the leg is gone, and without it rings the team."""

    def __init__(
        self,
        send: Callable[[str | bytes], None],
        *,
        ended: Callable[[str], None],
    ) -> None:
        self._send = send
        self._ended = ended
        self._session: CallSession | None = None
        self._lock = threading.Lock()
        self._frame_bytes = 0
        # Audio and sentence marks that wait for room at Asterisk, in order.
        self._held: deque[tuple[str, str | bytes]] = deque()
        # Marks of the messages of audio that Asterisk has not played yet.
        self._in_flight: set[str] = set()
        self._chunks = 0

    def attach(self, session: "CallSession") -> None:
        self._session = session

    # -- speech.Line, from the worker of the call ----------------------------------

    def answer(self) -> None:
        self._send(command("ANSWER"))

    def play(self, audio: bytes, mark: int) -> None:
        if not self._frame_bytes:
            raise ProtocolError("audio for a call whose media has not started")
        # Whole frames only: Asterisk drops what does not fill one.
        audio += bytes(-len(audio) % self._frame_bytes)
        size = MAX_MESSAGE_BYTES // self._frame_bytes * self._frame_bytes
        with self._lock:
            for start in range(0, len(audio), size):
                self._held.append(("audio", audio[start : start + size]))
            done = command("MARK_MEDIA", correlation_id=f"{_SENTENCE}{mark}")
            self._held.append(("mark", done))
            self._release()

    def flush(self) -> None:
        with self._lock:
            # Asterisk drops what it has queued, its marks included, and what
            # was held back here is never sent.
            self._held.clear()
            self._in_flight.clear()
            self._send(command("FLUSH_MEDIA"))

    def transfer(self, target: str) -> None:
        if not _TARGET.fullmatch(target):
            raise ValueError("not a number a call can be handed to")
        self._end(f"transfer {target}")

    def hangup(self) -> None:
        self._end("hangup")

    # -- what Asterisk sends, from the connection ----------------------------------

    def on_text(self, text: str) -> None:
        """One TEXT frame. Raises `ProtocolError` on a frame the call cannot go
        on with; the router then drops the connection and the team is rung."""
        session = self._attached()
        event = parse_event(text)
        if event.name == MEDIA_START:
            self._start(event.data)
            session.start()
        elif event.name == DTMF_END:
            digit = event.data.get("digit")
            if isinstance(digit, str) and len(digit) == 1:
                session.key(digit)
        elif event.name == MEDIA_MARK_PROCESSED:
            self._marked(str(event.data.get("correlation_id", "")))
        elif event.name == "MEDIA_XOFF":
            # Cannot happen while the line holds back what does not fit; if it
            # does, audio may be dropped behind a mark (rule 3).
            logger.error("the media queue of Asterisk is nearly full")
        # STATUS, QUEUE_DRAINED, MEDIA_XON and events of a later Asterisk: nothing
        # to do.

    def on_audio(self, frame: bytes) -> None:
        self._attached().audio(frame)

    def closed(self) -> None:
        """The connection is gone, whoever closed it."""
        self._attached().closed()

    # -- pieces --------------------------------------------------------------------

    def _attached(self) -> "CallSession":
        if self._session is None:
            raise RuntimeError("the media line has no call session")
        return self._session

    def _start(self, data: dict[str, Any]) -> None:
        if data.get("format") != MEDIA_FORMAT:
            raise ProtocolError(f"unexpected media format: {data.get('format')}")
        size = data.get("optimal_frame_size")
        if not isinstance(size, int) or size <= 0 or size % 2:
            raise ProtocolError("MEDIA_START without a usable frame size")
        self._frame_bytes = size

    def _marked(self, correlation_id: str) -> None:
        if correlation_id.startswith(_SENTENCE) and correlation_id[1:].isdigit():
            self._attached().played(int(correlation_id[1:]))
            return
        with self._lock:
            # A mark from before a flush is not in the set any more.
            self._in_flight.discard(correlation_id)
            self._release()

    def _release(self) -> None:
        """Send what was held back, as far as there is room. Under the lock."""
        while self._held:
            kind, frame = self._held[0]
            if kind == "audio":
                if len(self._in_flight) >= IN_FLIGHT_CHUNKS:
                    return
                self._chunks += 1
                chunk = f"{_CHUNK}{self._chunks}"
                self._send(frame)
                self._send(command("MARK_MEDIA", correlation_id=chunk))
                self._in_flight.add(chunk)
            else:
                self._send(frame)
            self._held.popleft()

    def _end(self, ending: str) -> None:
        self._ended(ending)
        self._send(command("HANGUP"))
