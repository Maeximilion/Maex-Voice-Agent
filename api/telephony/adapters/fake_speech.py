"""Fakes of ear, mouth and line for tests (docs/20 §6, `telephony/speech.py`).

No audio is understood or made here. The fake ear says what a test tells it the
caller said, the fake mouth returns the sentence itself as its "audio", and the
fake line records what it was asked to do, so a test reads the call back as a
list of actions.
"""

import time
from array import array
from collections.abc import Callable, Iterable

from api.telephony.audio import tone
from api.telephony.speech import Heard


class FakeEar:
    """Implements `speech.Ear`. `hear` is the caller speaking."""

    def __init__(self) -> None:
        self.frames: list[tuple[bytes, float]] = []
        self.expected: list[str] = []
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

    def expect(self, terms: list[str]) -> None:
        self.expected = terms

    def close(self) -> None:
        self.closed = True

    def hear(self, heard: Heard) -> None:
        if self._sink is None:
            raise RuntimeError("the ear was not opened")
        self._sink(heard)


class DrillEar(FakeEar):
    """The ear of the outage drills (`telephony/drill.py`): deaf, as FakeEar,
    except that a loud frame is the caller saying "Ja", every `SAYS_YES_EVERY`
    seconds of it. No engine runs in a drill, and the consent to a recording
    takes a spoken yes (docs/09 L2): the router's caller plays a tone for it."""

    SAYS_YES_EVERY = 1.0
    LOUD = 1000

    def __init__(self) -> None:
        super().__init__()
        self._said_at: float | None = None

    def feed(self, frame: bytes, at: float) -> None:
        if (self._said_at is None or at - self._said_at >= self.SAYS_YES_EVERY) and max(
            array("h", frame[: len(frame) - len(frame) % 2]), default=0
        ) >= self.LOUD:
            self._said_at = at
            self.hear(Heard("Ja", ended_at=at))


class LoadEar(FakeEar):
    """The ear of the load test (#841, `telephony/load.py`): a stand-in for a
    speech-to-text service. In operation that service runs outside the server
    and costs it network, not processor, so this one does no work: a loud
    frame is the caller speaking, and `END_SILENCE` of quiet after it ends the
    utterance, which is heard `DELAY` later as the next of `SENTENCES`. Partials
    while the caller speaks, as an adapter without partial transcripts sends
    them (`speech.Heard`)."""

    # The silence that ends a turn (docs/20 §6, T-10.3) and what a streaming
    # service takes after it for the final transcript.
    END_SILENCE = 0.5
    DELAY = 0.1
    PARTIAL_EVERY = 0.5
    LOUD = 1000
    # What the caller says, turn by turn: a pickup order, so that every turn
    # searches the menu.
    SENTENCES = (
        "Ich möchte etwas zum Abholen bestellen",
        "Die Nummer 23 bitte",
        "Und zweimal die 12",
        "Noch eine große Cola",
        "Das wäre alles",
    )

    def __init__(self) -> None:
        super().__init__()
        self._last_loud: float | None = None
        self._partial_at = 0.0
        self._turn = 0

    def feed(self, frame: bytes, at: float) -> None:
        samples = array("h", frame[: len(frame) - len(frame) % 2])
        if max(map(abs, samples), default=0) >= self.LOUD:
            if self._last_loud is None or at - self._partial_at >= self.PARTIAL_EVERY:
                self._partial_at = at
                self.hear(Heard("", final=False, ended_at=at))
            self._last_loud = at
        elif (
            self._last_loud is not None
            and at - self._last_loud >= self.END_SILENCE + self.DELAY
        ):
            ended_at, self._last_loud = self._last_loud, None
            text = self.SENTENCES[self._turn % len(self.SENTENCES)]
            self._turn += 1
            self.hear(Heard(text, ended_at=ended_at))


class LoadMouth:
    """The mouth of the load test (#841): a stand-in for a text-to-speech
    service. It answers after `DELAY` with a tone the caller's side can find in
    its recording, as long as the sentence would take to say (`SECONDS_PER_CHAR`,
    at most `MAX_SECONDS`), so the media channel carries what a voice would."""

    RATE = 16000
    DELAY = 0.15
    SECONDS_PER_CHAR = 0.06
    MAX_SECONDS = 4.0
    FREQ_HZ = 1000

    def __init__(self, sleep: Callable[[float], None] = time.sleep) -> None:
        self._sleep = sleep

    def speak(self, text: str) -> Iterable[bytes]:
        self._sleep(self.DELAY)
        seconds = min(len(text) * self.SECONDS_PER_CHAR, self.MAX_SECONDS)
        yield tone(self.FREQ_HZ, seconds, self.RATE)


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

    def record_team_leg(self) -> None:
        self.actions.append(("record_team_leg",))

    # -- reading back ----------------------------------------------------------------

    def played(self) -> list[str]:
        """The sentences queued for the caller, in order."""
        return [action[1] for action in self.actions if action[0] == "play"]

    def kinds(self) -> list[str]:
        return [action[0] for action in self.actions]
