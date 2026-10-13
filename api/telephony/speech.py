"""Ports of the own voice layer: ear, mouth and the line (docs/20 §2, §4, §6).

A call session (`telephony/session.py`) sits between three things it does not
know by name:

- the `Ear` turns the caller's audio into text (speech-to-text),
- the `Mouth` turns a sentence into audio (text-to-speech),
- the `Line` is the caller's leg: it plays audio, drops what is queued, hands
  the call over or ends it.

One adapter per engine implements `Ear` or `Mouth` (`adapters/stt_<engine>.py`,
`adapters/tts_<engine>.py`, T-10.5), the media adapter implements `Line`
(`adapters/asterisk.py`, T-1.11), and `adapters/fake_speech.py` holds a fake of
each for tests. No engine and no telephone system is named here.

Audio is raw 16 bit mono in both directions, at the rate the line was dialled
with. Times are seconds on the session's clock.
"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Heard:
    """What the ear reports about the caller's speech.

    A partial (`final=False`) says the caller is speaking right now and what
    was understood so far. An adapter for an engine without partial transcripts
    still sends partials, with an empty text, from its own speech detector: at
    least once a second while the caller speaks. The session counts silence
    from them; without them a long order would be answered with "Sind Sie noch
    da?" in the middle of it. A final is one finished utterance: the engine's
    own end of utterance, or the silence detector of an adapter for an engine
    that has none (docs/20 §6).
    """

    text: str
    final: bool = True
    # Final only: when the utterance ended, on the clock of the frames fed.
    # The session measures it against the end of a read-back (hard rule 3).
    ended_at: float = 0.0
    # Final only: was there speech energy under the words? An engine can make
    # words out of line noise; without energy they are not the caller's.
    speech: bool = True


class Ear(Protocol):
    def open(self, sink: Callable[[Heard], None]) -> None:
        """Start listening for one call. Everything heard goes to `sink`, from
        any thread; the session only enqueues there."""
        ...

    def feed(self, frame: bytes, at: float) -> None:
        """Audio from the caller, in order; `at` is when the frame arrived.
        Raises when the engine fails. Must not wait for the engine: it is
        called fifty times a second by the one worker of the call. An adapter
        whose engine blocks runs it on a thread of its own, bounds its calls
        and reports through the sink."""
        ...

    def close(self) -> None:
        """The call is over. Never raises."""
        ...


class Mouth(Protocol):
    def speak(self, text: str) -> Iterable[bytes]:
        """The audio of one sentence, in pieces as the engine delivers them.
        Raises when the engine fails. The session gives a sentence a deadline
        and treats an engine that does not finish as failed."""
        ...


class Line(Protocol):
    """The caller's leg. Every method raises when the telephone system refuses."""

    def answer(self) -> None:
        """Pick the call up. Before this the caller hears ringing."""
        ...

    def play(self, audio: bytes, mark: int) -> None:
        """Queue audio for the caller, behind what is queued already. When
        everything up to here was played, the line reports `mark` to the
        session (`CallSession.played`). Marks rise by one per call of `play`."""
        ...

    def flush(self) -> None:
        """Drop what is queued and not played yet. No mark is reported for it."""
        ...

    def transfer(self, target: str) -> None:
        """Hand the call to `target` (E.164). The leg is no longer ours."""
        ...

    def hangup(self) -> None:
        """End the call."""
        ...
