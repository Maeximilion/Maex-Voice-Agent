"""Fakes of ear, mouth and line for tests (docs/20 §6, `telephony/speech.py`).

No audio is understood or made here. The fake ear says what a test tells it the
caller said, the fake mouth returns the sentence itself as its "audio", and the
fake line records what it was asked to do, so a test reads the call back as a
list of actions.
"""

from collections.abc import Callable, Iterable

from api.telephony.speech import Heard


class FakeEar:
    """Implements `speech.Ear`. `hear` is the caller speaking."""

    def __init__(self) -> None:
        self.frames: list[tuple[bytes, float]] = []
        self.closed = False
        # Set to make the engine fail on the next frame.
        self.broken = False
        self._sink: Callable[[Heard], None] | None = None

    def open(self, sink: Callable[[Heard], None]) -> None:
        self._sink = sink

    def feed(self, frame: bytes, at: float) -> None:
        if self.broken:
            raise RuntimeError("speech-to-text engine failed")
        self.frames.append((frame, at))

    def close(self) -> None:
        self.closed = True

    def hear(self, heard: Heard) -> None:
        if self._sink is None:
            raise RuntimeError("the ear was not opened")
        self._sink(heard)


class FakeMouth:
    """Implements `speech.Mouth`: the sentence, encoded, in two pieces.

    That "audio" is a few bytes, so it plays in no time. A test that needs a
    sentence with a length sets `seconds`, and `FakeLine` cuts the padding off
    again."""

    RATE = 16000

    def __init__(self) -> None:
        self.spoken: list[str] = []
        # Set to make the engine fail on the next sentence.
        self.broken = False
        # How long a sentence plays, as silence behind its text.
        self.seconds = 0.0

    def speak(self, text: str) -> Iterable[bytes]:
        if self.broken:
            raise RuntimeError("text-to-speech engine failed")
        self.spoken.append(text)
        audio = text.encode("utf-8")
        half = len(audio) // 2
        yield audio[:half]
        yield audio[half:] + b"\x00" * int(self.seconds * self.RATE * 2)


class FakeLine:
    """Implements `speech.Line` and records every action."""

    def __init__(self) -> None:
        self.actions: list[tuple[str, ...]] = []
        self.mark = 0
        # Set to make the telephone system refuse a transfer, or a flush.
        self.refuses_transfer = False
        self.refuses_flush = False

    def answer(self) -> None:
        self.actions.append(("answer",))

    def play(self, audio: bytes, mark: int) -> None:
        self.actions.append(("play", audio.rstrip(b"\x00").decode("utf-8")))
        self.mark = mark

    def flush(self) -> None:
        if self.refuses_flush:
            raise RuntimeError("the telephone system refused the flush")
        self.actions.append(("flush",))

    def transfer(self, target: str) -> None:
        if self.refuses_transfer:
            raise RuntimeError("the telephone system refused the transfer")
        self.actions.append(("transfer", target))

    def hangup(self) -> None:
        self.actions.append(("hangup",))

    # -- reading back ----------------------------------------------------------------

    def played(self) -> list[str]:
        """The sentences queued for the caller, in order."""
        return [action[1] for action in self.actions if action[0] == "play"]

    def kinds(self) -> list[str]:
        return [action[0] for action in self.actions]
