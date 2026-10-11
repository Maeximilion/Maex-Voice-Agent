"""Measuring endpoint for the media channel (spike T-10.1, docs/20 §10 and §11).

Stands where the service `voice` will stand: the PBX connects here once per call.
The probe plays a test tone per dialed extension, listens for the tone the caller
plays, reacts to keys and writes every control frame into one log file per call.
`scripts/voice_spike.py` places the calls and judges the logs; the recorded frames
are also the test material for the media adapter (T-1.11).

    uvicorn api.telephony.probe:app --host 0.0.0.0 --port 8765

Plain ASGI and standard library only: the kit on the host imports the plans from
here without the API's packages. No database, no settings, nothing of a real call.
The log directory is `PROBE_OUT` (default `/out`).
"""

import json
import os
import time
import uuid
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from api.telephony.adapters.asterisk import (
    DTMF_END,
    MAX_MESSAGE_BYTES,
    MEDIA_START,
    QUEUE_DRAINED,
    ProtocolError,
    command,
    parse_event,
    sample_rate,
)
from api.telephony.audio import dominant, tone


@dataclass(frozen=True)
class Plan:
    tone_hz: int  # what the probe plays to the caller
    seconds: float
    hangup_when_spoken: bool = False


# Dialed extension to what the probe does. The softphone's side of each call is
# deploy/asterisk/softphone/extensions.conf.
PLANS = {
    "601": Plan(440, 4),
    "602": Plan(554, 4),
    "603": Plan(659, 4),
    "610": Plan(440, 15),  # long enough to be stopped by a key
    "611": Plan(440, 1, hangup_when_spoken=True),
}
# What the softphone plays on these extensions, so each call is told apart.
CALLER_TONES = {"601": 1000, "602": 1200, "603": 1400}
# The key that stops the playback, as a caller speaking over a sentence would.
STOP_KEY = "5"

# Enough for every plan; a longer call is still counted, only not analysed further.
_KEEP_SECONDS = 60


class ProbeCall:
    """One call: takes frames, returns the frames to send, logs both."""

    def __init__(self, exten: str, caller: str | None, out_dir: Path) -> None:
        self.plan = PLANS.get(exten)
        self._started = time.monotonic()
        self._rate = 16000
        self._heard = bytearray()
        self._bytes_in = 0
        self._frame_bytes: Counter[int] = Counter()
        self._first_audio: float | None = None
        self._last_audio: float | None = None
        self._digits = ""
        out_dir.mkdir(parents=True, exist_ok=True)
        # The extension comes off the wire: only its digits and letters name a file.
        label = "".join(ch for ch in exten if ch.isalnum())[:8] or "none"
        self._log = (out_dir / f"{label}-{uuid.uuid4().hex[:8]}.jsonl").open(
            "w", encoding="utf-8"
        )
        self._write("probe", exten=exten, caller=caller, wall=time.time())

    def on_text(self, text: str) -> list[str | bytes]:
        self._write("in", text=text)
        try:
            event = parse_event(text)
        except ProtocolError as exc:
            self._write("probe", error=str(exc))
            return []
        if event.name == MEDIA_START:
            return self._sent(self._start(event.data))
        if event.name == DTMF_END:
            digit = str(event.data.get("digit", ""))
            self._digits += digit
            if digit == STOP_KEY:
                return self._sent([command("FLUSH_MEDIA"), command("GET_STATUS")])
        if event.name == QUEUE_DRAINED and self.plan and self.plan.hangup_when_spoken:
            return self._sent([command("HANGUP")])
        return []

    def on_audio(self, data: bytes) -> list[str | bytes]:
        now = self._elapsed()
        if self._first_audio is None:
            self._first_audio = now
        self._last_audio = now
        self._bytes_in += len(data)
        self._frame_bytes[len(data)] += 1
        if len(self._heard) < _KEEP_SECONDS * self._rate * 2:
            self._heard += data
        return []

    def close(self) -> None:
        frames = sum(self._frame_bytes.values())
        interval = None
        if (
            frames > 1
            and self._first_audio is not None
            and self._last_audio is not None
        ):
            interval = round(
                (self._last_audio - self._first_audio) * 1000 / (frames - 1), 2
            )
        self._write(
            "probe",
            closed=True,
            heard_hz=dominant(bytes(self._heard), CALLER_TONES.values(), self._rate),
            audio_in_seconds=round(self._bytes_in / (self._rate * 2), 3),
            first_audio_t=self._first_audio,
            frame_bytes={str(size): count for size, count in self._frame_bytes.items()},
            frame_interval_ms=interval,
            digits=self._digits,
        )
        self._log.close()

    def _start(self, data: dict[str, Any]) -> list[str | bytes]:
        try:
            self._rate = sample_rate(str(data.get("format")))
            frame = int(data["optimal_frame_size"])
        except (ProtocolError, KeyError, ValueError) as exc:
            self._write("probe", error=f"unusable MEDIA_START: {exc}")
            return []
        if self.plan is None:
            return []
        # Whole frames only: Asterisk drops what does not fill one, and one message
        # above the limit ends the call.
        audio = tone(self.plan.tone_hz, self.plan.seconds, self._rate)
        chunk = MAX_MESSAGE_BYTES // frame * frame
        messages: list[str | bytes] = [
            audio[start : start + chunk] for start in range(0, len(audio), chunk)
        ]
        return [*messages, command("REPORT_QUEUE_DRAINED")]

    def _sent(self, frames: list[str | bytes]) -> list[str | bytes]:
        for frame in frames:
            if isinstance(frame, str):
                self._write("out", text=frame)
            else:
                self._write("out", bytes=len(frame))
        return frames

    def _elapsed(self) -> float:
        return round(time.monotonic() - self._started, 4)

    def _write(self, direction: str, **fields: Any) -> None:
        record = {"t": self._elapsed(), "dir": direction, **fields}
        self._log.write(json.dumps(record) + "\n")
        self._log.flush()


async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
    if scope["type"] == "http":  # the health check of the Compose service
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})
        return
    if scope["type"] != "websocket":
        return
    await receive()  # websocket.connect
    if scope["path"] != "/media":
        await send({"type": "websocket.close", "code": 1008})
        return
    offered = scope.get("subprotocols") or []
    await send(
        {
            "type": "websocket.accept",
            "subprotocol": "media" if "media" in offered else None,
        }
    )
    query = parse_qs(scope["query_string"].decode())
    call = ProbeCall(
        query.get("exten", [""])[0],
        query.get("caller", [None])[0],
        Path(os.environ.get("PROBE_OUT", "/out")),
    )
    try:
        while True:
            message = await receive()
            if message["type"] != "websocket.receive":
                break
            if message.get("text") is not None:
                frames = call.on_text(message["text"])
            else:
                frames = call.on_audio(message.get("bytes") or b"")
            for frame in frames:
                key = "text" if isinstance(frame, str) else "bytes"
                await send({"type": "websocket.send", key: frame})
    finally:
        call.close()
