"""telephony/speech_bench.py: the telephone line between mouth and ear.

Needs numpy, which the API image does not have: skipped in the full run. CI
runs this file once more with numpy installed and fails on a skip (#207).
The engines themselves are not tested here, the run of the kit is their test.
"""

import pytest

np = pytest.importorskip("numpy")

from api.telephony.audio import level, tone  # noqa: E402
from api.telephony.speech_bench import (  # noqa: E402
    EAR_RATE,
    on_the_line,
    speak,
    telephone,
)

VOICE_RATE = 22050


def through_the_line(freq_hz: int) -> bytes:
    heard = telephone(tone(freq_hz, 1.0, VOICE_RATE), VOICE_RATE)
    assert len(heard) == EAR_RATE and heard.dtype == np.float32
    return (heard * 32767).astype(np.int16).tobytes()


def test_a_tone_inside_the_band_arrives_at_the_ears_rate():
    sent = level(tone(1000, 1.0, VOICE_RATE), 1000, VOICE_RATE)
    assert level(through_the_line(1000), 1000, EAR_RATE) == pytest.approx(sent, rel=0.1)


@pytest.mark.parametrize("freq_hz", [100, 6000])
def test_a_tone_outside_the_band_does_not_arrive(freq_hz):
    sent = level(tone(freq_hz, 1.0, VOICE_RATE), freq_hz, VOICE_RATE)
    heard = through_the_line(freq_hz)
    assert level(heard, freq_hz, EAR_RATE) < 0.05 * sent
    # Nor does it come back mirrored into the band above the line's 4 kHz.
    assert level(heard, 8000 - freq_hz, EAR_RATE) < 0.05 * sent


def test_the_line_mirrors_nothing_into_the_band_above_it():
    assert level(through_the_line(3000), 5000, EAR_RATE) < 0.02


def test_silence_stays_silent():
    # Not exactly zero: A-law has no code for it and answers with its smallest step.
    assert abs(telephone(b"\x00\x00" * VOICE_RATE, VOICE_RATE)).max() < 0.001


def test_an_ear_with_a_sample_hears_every_nth_utterance(tmp_path, monkeypatch):
    """`measure` with fake engines: the sampled ear hears a spread, the other all."""
    import sys
    import types

    from api.telephony import speech_bench

    class Voice:
        def synthesize(self, text):
            yield types.SimpleNamespace(
                audio_int16_bytes=tone(1000, 0.2, VOICE_RATE), sample_rate=VOICE_RATE
            )

    piper = types.SimpleNamespace(
        PiperVoice=types.SimpleNamespace(load=lambda path: Voice())
    )
    monkeypatch.setitem(sys.modules, "piper", piper)
    # The silence detector comes with faster-whisper, which only the speech
    # image has; CI runs this file with numpy alone (#207).
    vad = types.SimpleNamespace(
        VadOptions=lambda **options: options,
        get_speech_timestamps=lambda audio, options: [{"start": 0, "end": 1}],
    )
    monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(vad=vad))
    monkeypatch.setitem(sys.modules, "faster_whisper.vad", vad)
    monkeypatch.setattr(
        speech_bench,
        "load_ear",
        lambda spec, models: (lambda audio, terms: "ja", lambda terms: []),
    )
    plan = {
        "voices": ["a", "b"],
        "agent": ["Guten Tag."],
        "sentences": [{"id": f"n{i}", "say": "Nummer acht"} for i in range(6)],
        "ears": [{"id": "all"}, {"id": "some", "sample": 3}],
        "key_terms": [],
        "silences_ms": [300],
    }
    result = speech_bench.measure(tmp_path, plan)
    heard = {
        ear: [r for r in result["heard"] if r["ear"] == ear] for ear in ("all", "some")
    }
    assert len(heard["all"]) == 12 and len(heard["some"]) == 3
    assert {r["voice"] for r in heard["some"]} == {"a", "b"}
    assert sorted(p.name for p in (tmp_path / "samples").iterdir()) == [
        "a-1.wav",
        "b-1.wav",
    ]
    assert len(result["turn_end"]) == 12 and len(result["mouths"]) == 2


def test_speak_joins_the_pieces_and_times_the_first():
    class Chunk:
        sample_rate = VOICE_RATE

        def __init__(self, audio: bytes) -> None:
            self.audio_int16_bytes = audio

    class Voice:
        def synthesize(self, text):
            yield Chunk(b"ab")
            yield Chunk(b"cd")

    pcm, rate, first, total = speak(Voice(), "Guten Tag.")
    assert (pcm, rate) == (b"abcd", VOICE_RATE)
    assert 0 <= first <= total


@pytest.mark.parametrize(
    ("spec", "options"),
    [({"language": "de"}, {"language": "de"}), ({}, {})],
)
def test_an_ear_is_told_the_language_only_if_its_model_takes_one(
    tmp_path, monkeypatch, spec, options
):
    """Canary takes the language, Parakeet finds it itself and has no way to be
    told (docs/20 §11)."""
    import sys
    import types

    from api.telephony import speech_bench

    calls: list[dict] = []

    class Model:
        def recognize(self, audio, **kwargs):
            calls.append(kwargs)
            return "ja"

    fake = types.SimpleNamespace(load_model=lambda name, path: Model())
    monkeypatch.setitem(sys.modules, "onnx_asr", fake)
    hear, fit = speech_bench.load_ear(
        {"engine": "parakeet", "model": "m", **spec}, tmp_path
    )
    assert hear(b"", ["Pho Bo"]) == "ja" and fit(["Pho Bo"]) == []
    assert calls == [{"sample_rate": EAR_RATE, **options}]


def test_the_line_carries_quiet_noise_around_the_sentence():
    """Quiet before and after, noise under all of it, no digital zeros: a
    detector that sees seconds of zeros misses the answer after them."""
    sentence = tone(1000, 0.5, EAR_RATE)
    heard = on_the_line(sentence, EAR_RATE, before=1.0, after=2.0)
    assert len(heard) == int(EAR_RATE * 3.5)

    def rms(samples) -> float:
        return float(np.sqrt(np.mean(samples**2)))

    quiet = heard[: EAR_RATE // 2]
    assert 0 < rms(quiet) < rms(heard[EAR_RATE + 1000 : EAR_RATE + 7000]) / 10
    assert np.count_nonzero(quiet == 0) < len(quiet) / 100
    # The same seed, the same line: a run hears the same audio as the last.
    assert np.array_equal(heard, on_the_line(sentence, EAR_RATE, 1.0, 2.0))
