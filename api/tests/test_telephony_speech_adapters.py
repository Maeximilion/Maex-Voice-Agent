"""telephony/adapters: the ear and the mouth of the test phase (T-10.5, docs/20 §6).

Against stand-ins for the engines: the detector reads the first byte of a
window (1 speech, 2 in between, 0 silence), the engine returns a fixed text and
keeps what it was given. The real engines are tested in
`test_telephony_speech_engines.py`, inside the image that has them.
"""

from itertools import pairwise
from types import SimpleNamespace
from typing import Any

import pytest

from api.telephony.adapters.stt_whisper import (
    MIN_SPEECH_SECONDS,
    PAD_SECONDS,
    PARTIAL_SECONDS,
    SAMPLE_RATE,
    TURN_END_SECONDS,
    WINDOW,
    SegmentingEar,
)
from api.telephony.adapters.tts_piper import PiperMouth
from api.telephony.speech import Heard

SECONDS = WINDOW / SAMPLE_RATE  # one window
FRAME = 640  # 20 ms, what the media channel sends


def windows(seconds: float) -> int:
    return round(seconds / SECONDS)


def speech(n: int) -> bytes:
    return b"\x01" * (2 * WINDOW * n)


def between(n: int) -> bytes:
    return b"\x02" * (2 * WINDOW * n)


def silence(n: int) -> bytes:
    return b"\x00" * (2 * WINDOW * n)


def probability(window: bytes) -> float:
    return {1: 0.9, 2: 0.4, 0: 0.0}[window[0]]


class Engine:
    def __init__(self, text: str = "die 23", fails: bool = False) -> None:
        self.text = text
        self.fails = fails
        self.given: list[tuple[bytes, list[str]]] = []

    def __call__(self, audio: bytes, terms: list[str]) -> str:
        if self.fails:
            raise RuntimeError("engine failed")
        self.given.append((audio, terms))
        return self.text


def hear(ear, audio: bytes, *, frame: int = 2 * WINDOW, terms=None):
    """Feed `audio` in frames, each stamped with the time its end arrived,
    then close the ear and wait for its thread. What the ear reported."""
    heard: list[Heard] = []
    ear.open(heard.append)
    if terms is not None:
        ear.expect(terms)
    for start in range(0, len(audio), frame):
        chunk = audio[start : start + frame]
        ear.feed(chunk, (start + len(chunk)) / 2 / SAMPLE_RATE)
    ear.close()
    ear.worker.join(5)
    return heard


def at(n: int) -> Any:
    """When the n-th window (counted from 1) arrived."""
    return pytest.approx(n * SECONDS)


def finals(heard):
    return [h for h in heard if h.final]


# --- the ear -------------------------------------------------------------------------


def test_speech_is_reported_while_it_lasts_and_ends_in_one_final():
    engine = Engine()
    ear = SegmentingEar(engine, probability)
    heard = hear(ear, silence(10) + speech(31) + silence(30))

    partials = [h for h in heard if not h.final]
    assert [h.text for h in partials] == ["", ""]
    assert partials[0].ended_at == at(11)
    assert finals(heard) == [heard[-1]]
    assert (heard[-1].text, heard[-1].speech) == ("die 23", True)
    # The end of the speech, not when the silence after it was over.
    assert heard[-1].ended_at == at(41)
    # The engine hears the speech with a little before and after it.
    pad = windows(PAD_SECONDS)
    assert len(engine.given[0][0]) == 2 * WINDOW * (pad + 31 + pad)


def test_a_pause_shorter_than_the_turn_end_keeps_one_utterance():
    """A guest who stops to think between two dishes is not answered in the
    middle of the order (docs/20 §11: 500 ms cut one order in ten)."""
    engine = Engine()
    pause = windows(TURN_END_SECONDS) - 1
    heard = hear(
        SegmentingEar(engine, probability),
        speech(10) + silence(pause) + speech(10) + silence(30),
    )
    assert len(finals(heard)) == 1
    assert len(engine.given) == 1


def test_a_pause_of_the_turn_end_ends_the_utterance():
    engine = Engine()
    pause = windows(TURN_END_SECONDS)
    heard = hear(
        SegmentingEar(engine, probability),
        speech(10) + silence(pause) + speech(10) + silence(30),
    )
    assert [h.ended_at for h in finals(heard)] == [at(10), at(10 + pause + 10)]
    # The second utterance starts with the silence before it, as the first did.
    second = engine.given[1][0]
    assert second.startswith(silence(windows(PAD_SECONDS)))


def test_a_click_is_no_speech_and_the_engine_is_not_asked():
    """The engine would make words of it; words without speech are no answer,
    least of all a yes (docs/20 §4)."""
    engine = Engine()
    short = windows(MIN_SPEECH_SECONDS) - 1
    heard = hear(SegmentingEar(engine, probability), speech(short) + silence(30))
    assert finals(heard) == [heard[-1]]
    assert (heard[-1].text, heard[-1].speech) == ("", False)
    assert engine.given == []


def test_a_probability_in_between_continues_what_was_before():
    engine = Engine()
    # In between after speech: still speech.
    heard = hear(
        SegmentingEar(engine, probability), speech(10) + between(20) + silence(30)
    )
    assert finals(heard)[0].ended_at == at(30)
    # In between after silence began: still silence, the turn ends.
    heard = hear(
        SegmentingEar(engine, probability), speech(10) + silence(2) + between(30)
    )
    assert finals(heard)[0].ended_at == at(10)
    # In between alone starts nothing.
    assert hear(SegmentingEar(engine, probability), between(30) + silence(30)) == []


def test_a_long_utterance_reports_speech_at_least_twice_a_second():
    heard = hear(
        SegmentingEar(Engine(), probability), speech(windows(3.0)) + silence(30)
    )
    times = [h.ended_at for h in heard if not h.final]
    gaps = [later - earlier for earlier, later in pairwise(times)]
    assert len(times) >= 6
    assert max(gaps) <= PARTIAL_SECONDS + SECONDS


def test_the_key_terms_go_to_the_engine_with_the_utterance():
    engine = Engine()
    hear(
        SegmentingEar(engine, probability),
        speech(10) + silence(30),
        terms=["Pho Bo", "Pho Ga"],
    )
    assert engine.given[0][1] == ["Pho Bo", "Pho Ga"]


def test_frames_of_any_size_are_judged_in_windows():
    engine = Engine()
    heard = hear(
        SegmentingEar(engine, probability),
        silence(10) + speech(31) + silence(30),
        frame=FRAME,
    )
    assert len(finals(heard)) == 1
    # Arrival is stamped per frame, so the end is known to one frame.
    assert abs(finals(heard)[0].ended_at - 41 * SECONDS) <= FRAME / 2 / SAMPLE_RATE


def test_an_engine_that_fails_fails_the_next_feed():
    """The session takes that as a failure of the ear and sends the call to
    the team (CLAUDE.md §2 rule 5)."""
    ear = SegmentingEar(Engine(fails=True), probability)
    hear(ear, speech(10) + silence(30))
    with pytest.raises(RuntimeError, match="speech-to-text failed"):
        ear.feed(silence(1), 2.0)


def test_what_is_being_said_when_the_line_closes_is_dropped():
    engine = Engine()
    heard = hear(SegmentingEar(engine, probability), speech(20))
    assert heard and finals(heard) == []
    assert engine.given == []


def test_silence_reports_nothing():
    assert hear(SegmentingEar(Engine(), probability), silence(100)) == []


# --- the mouth -------------------------------------------------------------------------


class Voice:
    """Stands in for a Piper voice: one piece of a second per sentence part."""

    def __init__(self, rate: int, parts: int = 2) -> None:
        self.rate = rate
        self.parts = parts

    def synthesize(self, text: str):
        for _ in range(self.parts):
            yield SimpleNamespace(
                audio_int16_bytes=b"\x00\x10" * self.rate, sample_rate=self.rate
            )


def test_the_mouth_speaks_at_the_rate_of_the_line():
    pieces = list(PiperMouth(Voice(22050)).speak("Guten Tag."))
    assert len(pieces) == 2
    # Two seconds of audio at 16 kHz, give or take a sample of the conversion.
    assert abs(sum(len(p) for p in pieces) / 2 - 2 * SAMPLE_RATE) <= 2


def test_a_voice_at_the_rate_of_the_line_passes_unchanged():
    pieces = list(PiperMouth(Voice(SAMPLE_RATE)).speak("Guten Tag."))
    assert b"".join(pieces) == b"\x00\x10" * SAMPLE_RATE * 2


def test_a_voice_that_fails_raises_to_the_session():
    class Broken:
        def synthesize(self, text: str):
            raise RuntimeError("voice failed")
            yield

    with pytest.raises(RuntimeError):
        list(PiperMouth(Broken()).speak("Guten Tag."))
