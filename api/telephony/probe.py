"""Measuring endpoint for the media channel (spike T-10.1, docs/20 §10 and §11).

Stands where the service `voice` will stand: the PBX connects here once per call.
The probe plays a test tone per dialed extension, listens for the tone the caller
plays, reacts to keys and writes every control frame into one log file per call.
`api/telephony/spike.py` places the calls and judges the logs; the recorded frames
are also the test material for the media adapter (T-1.11).

    uvicorn api.telephony.probe:app --host 0.0.0.0 --port 8765

Plain ASGI and standard library only: the kit on the host imports the plans from
here without the API's packages. No database, no settings, nothing of a real call.
The log directory is `PROBE_OUT` (default `/out`).

A call without an extension is one of the product's dialplan (T-10.1 step 2,
`deploy/asterisk/docker-compose.line.yml`): somebody rang the test number at the
pilot's router. The probe answers it, plays a short tone, then sends back what
the caller says, so the caller hears both directions, and hangs up after
`LINE_SECONDS`. The caller's number is logged masked, and asked how a leg ended
the probe says "hangup", so a test call never rings the team.
"""

import asyncio
import bisect
import functools
import json
import os
import time
import uuid
from collections import Counter
from collections.abc import MutableMapping
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
    call_of,
    command,
    parse_event,
    sample_rate,
)
from api.telephony.audio import dominant, loud_seconds, tone, tone_seconds, tone_span


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
# What the softphone plays on these extensions, so each call is told apart. On
# 610 its start is the mark that puts the recording and this log on one clock.
CALLER_TONES = {"601": 1000, "602": 1200, "603": 1400, "610": 1000}
# A call of the product's dialplan: a tone to say it is us, then the echo.
LINE_PLAN = Plan(440, 1)
LINE_SECONDS = 60
# The key that stops the playback, as a caller speaking over a sentence would.
STOP_KEY = "5"

# Enough for every plan; a longer call is still counted, only not analysed further.
_KEEP_SECONDS = 60
# What the media channel is dialed with; other rates are made when they turn up.
_DIALED_RATE = 16000


# Connections open right now: two at once is a point of step 2.
_live = 0


def masked(number: str | None) -> str:
    """Enough of a number to tell its form and which phone it was, no more."""
    if not number:
        return "none"
    if len(number) <= 5:
        return "x" * len(number)
    return number[:3] + "x" * (len(number) - 5) + number[-2:]


@functools.cache
def _plan_audio(tone_hz: int, seconds: float, rate: int) -> bytes:
    return tone(tone_hz, seconds, rate)


def _make_tones() -> None:
    """Every plan's tone, once per process. Later calls take them from the cache."""
    for plan in PLANS.values():
        _plan_audio(plan.tone_hz, plan.seconds, _DIALED_RATE)


class ProbeCall:
    """One call: takes frames, returns the frames to send, logs both."""

    def __init__(
        self, exten: str, caller: str | None, out_dir: Path, live: int = 1
    ) -> None:
        self.line = not exten
        self.plan = LINE_PLAN if self.line else PLANS.get(exten)
        self._live = live
        self._answered = False
        self._hung_up = False
        self._echoed = 0
        self._started = time.monotonic()
        self._rate = 16000
        self._heard = bytearray()
        # Where each kept frame starts in `_heard`, and when it arrived.
        self._frame_starts: list[int] = []
        self._frame_times: list[float] = []
        self._bytes_in = 0
        self._frame_bytes: Counter[int] = Counter()
        self._first_audio: float | None = None
        self._last_audio: float | None = None
        self._longest_gap: float | None = None
        self._digits = ""
        out_dir.mkdir(parents=True, exist_ok=True)
        # The extension comes off the wire: only its digits and letters name a file.
        label = "".join(ch for ch in exten if ch.isalnum())[:8] or "none"
        self._log = (out_dir / f"{label}-{uuid.uuid4().hex[:8]}.jsonl").open(
            "w", encoding="utf-8"
        )
        # `mono` is this process's monotonic clock: comparable between calls.
        self._write("probe", exten=exten, caller=caller, mono=self._started)

    def on_text(self, text: str) -> list[str | bytes]:
        try:
            event = parse_event(text)
        except ProtocolError as exc:
            self._write("in", text=text)
            self._write("probe", error=str(exc))
            return []
        if self.line and event.name == MEDIA_START:
            return self._sent(self._answer(event, text))
        self._write("in", text=text)
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
        if self._last_audio is not None:
            # The mean alone hides bursts: 0 and 40 ms in turn average 20 too.
            self._longest_gap = max(self._longest_gap or 0.0, now - self._last_audio)
        self._last_audio = now
        self._bytes_in += len(data)
        self._frame_bytes[len(data)] += 1
        if len(self._heard) < _KEEP_SECONDS * self._rate * 2:
            self._frame_starts.append(len(self._heard))
            self._frame_times.append(now)
            self._heard += data
        if not self._answered or self._hung_up:
            return []
        if now >= LINE_SECONDS:
            return self.on_deadline()
        # Queued behind the tone, so the caller hears themselves about a second late.
        self._echoed += len(data)
        return [data]

    def seconds_left(self) -> float | None:
        """Until a line call is hung up; None while no deadline runs. Also without
        audio: a caller who puts the call on hold sends none."""
        if not self._answered or self._hung_up:
            return None
        return max(0.0, LINE_SECONDS - self._elapsed())

    def on_deadline(self) -> list[str | bytes]:
        if not self._answered or self._hung_up:
            return []
        self._hung_up = True
        return self._sent([command("HANGUP")])

    def close(self) -> None:
        """Analyse what was heard and end the log. Slow: not for the event loop."""
        heard = bytes(self._heard)
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
            heard_hz=dominant(heard, CALLER_TONES.values(), self._rate),
            # Every caller tone, so the tone of another call shows as cross-talk.
            tones_in={
                str(hz): tone_seconds(heard, hz, self._rate)
                for hz in CALLER_TONES.values()
            },
            # When each caller tone began, on this log's clock. Not when audio
            # began: a line may carry silence first.
            tones_in_at={
                str(hz): self._onset(heard, hz) for hz in CALLER_TONES.values()
            },
            audio_in_seconds=round(self._bytes_in / (self._rate * 2), 3),
            frame_bytes={str(size): count for size, count in self._frame_bytes.items()},
            frame_interval_ms=interval,
            frame_gap_max_ms=None
            if self._longest_gap is None
            else round(self._longest_gap * 1000, 2),
            digits=self._digits,
            voice_in_seconds=loud_seconds(heard, self._rate),
            echoed_seconds=round(self._echoed / (self._rate * 2), 3),
        )
        self._log.close()

    def _onset(self, heard: bytes, tone_hz: int) -> float | None:
        """When the frame arrived in which `tone_hz` begins, None when it never
        sounds."""
        span = tone_span(heard, tone_hz, self._rate)
        if span is None:
            return None
        at = int(span[0] * self._rate) * 2
        return self._frame_times[bisect.bisect_right(self._frame_starts, at) - 1]

    def _answer(self, start: Any, text: str) -> list[str | bytes]:
        """A call of the product's dialplan: answered, toned, then echoed. The
        number never reaches the log in full."""
        caller = None
        try:
            caller = call_of(start).caller_id
        except ProtocolError as exc:
            self._write("probe", error=str(exc))
        seen = masked(caller)
        self._write("in", text=text.replace(caller, seen) if caller else text)
        self._write("probe", caller_seen=seen, live=self._live)
        self._answered = True
        return [command("ANSWER"), *self._start(start.data)]

    def _start(self, data: dict[str, Any]) -> list[str | bytes]:
        try:
            self._rate = sample_rate(str(data.get("format")))
            frame = int(data["optimal_frame_size"])
            if frame <= 0:
                raise ValueError("optimal_frame_size is not positive")
        except (ProtocolError, KeyError, TypeError, ValueError) as exc:
            self._write("probe", error=f"unusable MEDIA_START: {exc}")
            return []
        if self.plan is None:
            return []
        # Whole frames only: Asterisk drops what does not fill one, and one message
        # above the limit ends the call.
        audio = _plan_audio(self.plan.tone_hz, self.plan.seconds, self._rate)
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


async def app(scope: MutableMapping[str, Any], receive: Any, send: Any) -> None:
    global _live
    if scope["type"] == "http":  # the health check of the Compose service
        # The product's dialplan asks how our leg ended: never the team for a test.
        body = b"hangup" if scope["path"].endswith("/ending") else b"ok"
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": body})
        return
    if scope["type"] != "websocket":
        return
    await receive()  # websocket.connect
    if scope["path"] != "/media":
        await send({"type": "websocket.close", "code": 1008})
        return
    # Before the call exists, in a thread: making a tone on the event loop would
    # hold up the frames of the calls that are live. Only the first call waits.
    await asyncio.to_thread(_make_tones)
    offered = scope.get("subprotocols") or []
    await send(
        {
            "type": "websocket.accept",
            "subprotocol": "media" if "media" in offered else None,
        }
    )
    query = parse_qs(scope["query_string"].decode())
    _live += 1
    call = ProbeCall(
        query.get("exten", [""])[0],
        query.get("caller", [None])[0],
        Path(os.environ.get("PROBE_OUT", "/out")),
        _live,
    )
    try:
        while True:
            try:
                message = await asyncio.wait_for(receive(), call.seconds_left())
            except TimeoutError:
                frames = call.on_deadline()
            else:
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
        _live -= 1
        # In a thread: the analysis would stall the frames of every other call.
        # Shielded: when this task is cancelled (the server shuts down, a test
        # client leaves), a plain wait would cancel the job while it is still
        # queued, and the log would never get its closing record.
        closing = asyncio.get_running_loop().run_in_executor(None, call.close)
        await asyncio.shield(closing)
