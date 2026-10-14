"""The real engines of the test phase (T-10.5): Piper speaks, the sentence goes
through a modelled telephone line, Whisper hears it through the segmenting ear.

Runs only where the engines and their model files are: in the image of
`deploy/speech/` on the workbench, with the models of the kit's run folder
mounted (docs/20 §10). Skipped everywhere else, CI included. Ubuntu terminal,
in the repository folder or a worktree of it; `MODELS` is where the kit keeps
its model files (`python3 -m scripts.speech_spike` downloads them there):

    MODELS="$HOME/Maex-Voice-Agent/deploy/speech/run/models"
    docker run --rm --network none --gpus all -v "$MODELS:/models:ro" \\
      -v "$PWD:/app" -w /app -e SPEECH_MODELS=/models maex-speech-spike \\
      python -m pytest -q -s -p no:cacheprovider api/tests/test_telephony_speech_engines.py

Without a graphics card (Whisper takes seconds per sentence then):

    docker run --rm --network none -v "$MODELS:/models:ro" \\
      -v "$PWD:/app" -w /app -e SPEECH_MODELS=/models -e SPEECH_DEVICE=cpu \\
      maex-speech-spike \\
      python -m pytest -q -s -p no:cacheprovider api/tests/test_telephony_speech_engines.py
"""

import os
import time
from pathlib import Path

import pytest

pytest.importorskip("faster_whisper")
pytest.importorskip("piper")
if not os.environ.get("SPEECH_MODELS"):
    pytest.skip("no model files (SPEECH_MODELS)", allow_module_level=True)

import numpy as np
from faster_whisper.vad import VadOptions, get_speech_timestamps
from piper import PiperVoice
from piper.config import SynthesisConfig

from api.telephony.adapters.stt_whisper import SAMPLE_RATE, WINDOW, Whisper
from api.telephony.adapters.tts_piper import PiperMouth
from api.telephony.speech import Heard
from api.telephony.speech_bench import telephone

MODELS = Path(os.environ["SPEECH_MODELS"])
DEVICE = os.environ.get("SPEECH_DEVICE", "cuda")
FRAME = 640  # 20 ms


class Steady:
    """The voice without the noise it adds to every sentence, so a run of the
    test speaks the same audio as the last (docs/20 §10: single sentences
    flip between runs otherwise)."""

    def __init__(self, voice) -> None:
        self.voice = voice

    def synthesize(self, text: str):
        steady = SynthesisConfig(noise_scale=0.0, noise_w_scale=0.0)
        return self.voice.synthesize(text, syn_config=steady)


@pytest.fixture(scope="module")
def mouth():
    voice = PiperVoice.load(str(MODELS / "piper" / "de_DE-thorsten-medium.onnx"))
    return PiperMouth(Steady(voice))


@pytest.fixture(scope="module")
def whisper():
    return Whisper(
        "large-v3-turbo",
        MODELS / "whisper",
        device=DEVICE,
        compute_type="float16" if DEVICE == "cuda" else "int8",
    )


def spoken(mouth, text: str) -> tuple[bytes, float]:
    """The sentence as the caller's side of the line carries it, and the
    seconds until the mouth's first piece."""
    started = time.monotonic()
    pieces = iter(mouth.speak(text))
    first = next(pieces)
    to_first = time.monotonic() - started
    pcm = first + b"".join(pieces)
    line = telephone(pcm, SAMPLE_RATE)
    return (line * 32767).astype(np.int16).tobytes(), to_first


def hear(ear, audio: bytes, terms: list[str] | None = None):
    heard: list[Heard] = []
    ear.open(heard.append)
    if terms:
        ear.expect(terms)
    for start in range(0, len(audio), FRAME):
        ear.feed(audio[start : start + FRAME], (start + FRAME) / 2 / SAMPLE_RATE)
    fed = time.monotonic()
    while not any(h.final for h in heard) and time.monotonic() - fed < 30:
        time.sleep(0.01)
    ear.close()
    return heard, time.monotonic() - fed


def test_a_spoken_order_is_heard_as_one_utterance(mouth, whisper):
    silence = bytes(2 * SAMPLE_RATE)
    sentence, to_first = spoken(mouth, "Ich hätte gern zweimal die Nummer 45.")
    heard, took = hear(whisper.ear(), silence + sentence + silence)

    finals = [h for h in heard if h.final]
    print(f"\nmouth to first audio {to_first:.2f} s, ear after the audio {took:.2f} s")
    print("heard:", finals[0].text if finals else None)
    assert len(finals) == 1
    assert finals[0].speech
    assert "45" in finals[0].text
    assert any(not h.final for h in heard[: heard.index(finals[0])])
    # Where the speech ends by the detector run over the whole recording: the
    # ear finds the same window and stamps it with when its audio began.
    pcm = np.frombuffer(silence + sentence + silence, dtype=np.int16)
    audio = pcm.astype(np.float32) / 32768
    options = VadOptions(min_silence_duration_ms=700, speech_pad_ms=0)
    speech_end = get_speech_timestamps(audio, options)[-1]["end"] / SAMPLE_RATE
    assert finals[0].ended_at == pytest.approx(speech_end - WINDOW / SAMPLE_RATE)


def test_the_key_terms_reach_whisper(mouth, whisper):
    sentence, _ = spoken(mouth, "Einmal die Ente süß-sauer, bitte.")
    ear = whisper.ear()
    heard, _ = hear(
        ear,
        bytes(SAMPLE_RATE) + sentence + bytes(2 * SAMPLE_RATE),
        terms=["Ente knusprig", "Ente süß-sauer", "Ente mit Mango"],
    )
    finals = [h for h in heard if h.final]
    print("\nheard with key terms:", finals[0].text if finals else None)
    assert len(finals) == 1 and "Ente" in finals[0].text


def test_a_silent_line_reports_nothing(whisper):
    heard, _ = hear(whisper.ear(), bytes(2 * SAMPLE_RATE * 3))
    assert heard == []
