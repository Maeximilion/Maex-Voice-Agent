"""The recording of a call the AI takes: one WAV file, written by its call session.

Two channels, 16 bit: what the line brought on the left, what the AI said on the
right. The media channel knows no recording and no way to start one in the
middle of a call, so the session writes what it has anyway. Time moves with the
caller's frames, which arrive as they are spoken: each one takes as much of the
AI's audio as it is long, and what the AI says while a frame is on its way waits
for the next. A team leg is the telephone system's (docs/20 §4).

One thread only, the worker of the call. Every method can raise `OSError`, and
the session lets no error of a recording touch the call (rule 5).
"""

import wave
from pathlib import Path


class Tape:
    def __init__(self, path: Path, sample_rate: int) -> None:
        self.path = path
        # Spoken and not yet on the left channel's clock.
        self._spoken = bytearray()
        # A missing folder fails here and not half way through `wave`, and a
        # recording is nobody else's to read.
        path.touch(mode=0o640)
        self._wav = wave.open(str(path), "wb")  # noqa: SIM115 - held for the call
        self._wav.setnchannels(2)
        self._wav.setsampwidth(2)
        self._wav.setframerate(sample_rate)

    def caller(self, frame: bytes) -> None:
        """One frame from the line, whole samples."""
        size = len(frame) - len(frame) % 2
        spoken = bytes(self._spoken[:size]).ljust(size, b"\x00")
        del self._spoken[:size]
        self._write(frame[:size], spoken)

    def spoken(self, audio: bytes) -> None:
        """A sentence the AI queued on the line."""
        self._spoken += audio[: len(audio) - len(audio) % 2]

    def flush(self) -> None:
        """The line dropped what was queued: it is not played."""
        self._spoken.clear()

    def close(self) -> None:
        """What is left of the last sentence goes on the tape: the line played it."""
        try:
            self._write(bytes(len(self._spoken)), bytes(self._spoken))
        finally:
            self._wav.close()

    def _write(self, left: bytes, right: bytes) -> None:
        """Sample by sample, a pair of bytes from each side."""
        stereo = bytearray(2 * len(left))
        stereo[0::4], stereo[1::4] = left[0::2], left[1::2]
        stereo[2::4], stereo[3::4] = right[0::2], right[1::2]
        self._wav.writeframes(bytes(stereo))
