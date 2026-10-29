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

import json
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
from api.telephony.speech_bench import on_the_line, telephone

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


def on_line(pcm: bytes, before: float, after: float) -> bytes:
    line = on_the_line(pcm, SAMPLE_RATE, before, after)
    return (np.clip(line, -1, 1) * 32767).astype(np.int16).tobytes()


@pytest.mark.parametrize("voice", ["de_DE-thorsten-medium", "de_DE-thorsten-high"])
@pytest.mark.parametrize("silent", [4, 10])
def test_a_short_answer_after_the_silence_of_a_read_back_is_heard(
    whisper, voice, silent
):
    """While the agent reads back, the caller is silent for seconds. On a line
    that carries its noise the answer after that is heard; after digital
    silence, which no line carries, the detector missed it (docs/20 §12)."""
    voice_file = MODELS / "piper" / f"{voice}.onnx"
    speaker = PiperMouth(Steady(PiperVoice.load(str(voice_file))))
    answer = b"".join(speaker.speak("Ja, das passt so."))
    heard, _ = hear(whisper.ear(), on_line(answer, silent, 1.0))

    finals = [h for h in heard if h.final]
    print(f"\n{voice} after {silent} s:", finals)
    assert len(finals) == 1 and finals[0].speech
    assert "passt" in finals[0].text


def test_a_quiet_line_is_no_speech(whisper):
    """The noise of the line alone, for longer than a read-back: no
    utterance, or one without speech, never words."""
    heard, _ = hear(whisper.ear(), on_line(b"", 12.0, 0.0))
    assert all(not h.final or not h.speech for h in heard), heard


def test_a_call_on_the_engines_of_the_service(mouth, monkeypatch):
    """The service's own loaders (`telephony/app.py`), the media door and the
    session: Piper greets, the caller's sentence goes in as frames of the media
    channel, Whisper hears it, the core answers and hangs up (T-1.11 part 2)."""
    from fastapi.testclient import TestClient

    from api.config import settings
    from api.telephony import app as voice_app
    from api.telephony.session import Lines
    from api.tests.test_telephony_voice import (
        TOKEN,
        connect,
        media_start,
        processed,
        receive_command,
        wait_for,
    )

    monkeypatch.setattr(voice_app, "MODELS", MODELS)
    monkeypatch.setattr(settings, "voice_ear_device", DEVICE)
    watchdog = voice_app.Watchdog()

    class Core:
        def __init__(self, lines: Lines) -> None:
            self.lines = lines
            self.heard: list[str] = []

        def on_call_started(self, session_id):
            self.lines.say(session_id, "Guten Tag, was darf es sein?")

        def on_user_turn(self, session_id, text):
            self.heard.append(text)
            self.lines.say(session_id, "Vielen Dank, auf Wiederhören.")
            self.lines.hangup(session_id)

        def on_dtmf(self, session_id, digits): ...
        def on_silence(self, session_id): ...
        def on_voice_failure(self, session_id): ...
        def on_call_ended(self, session_id, speech=None): ...

    lines = Lines()
    core = Core(lines)
    voice = voice_app.Voice(
        events=core,
        lines=lines,
        token=TOKEN,
        ear=voice_app._whisper(watchdog),
        mouth=voice_app._piper(watchdog),
        silence_seconds=60.0,
    )

    def played_sentence(ws) -> int:
        """Acknowledge every mark of one sentence; the bytes of its audio."""
        audio = 0
        while True:
            message = ws.receive()
            if message.get("bytes") is not None:
                audio += len(message["bytes"])
                continue
            command = json.loads(message["text"])
            assert command["command"] == "MARK_MEDIA", command
            ws.send_text(processed(command["correlation_id"]))
            if command["correlation_id"].startswith("s"):
                return audio

    caller, _ = spoken(mouth, "Ich hätte gern zweimal die Nummer 45.")
    audio = bytes(SAMPLE_RATE) + caller + bytes(2 * SAMPLE_RATE)
    with TestClient(voice_app.create_app(voice)) as client, connect(client) as ws:
        ws.send_text(media_start())
        assert receive_command(ws) == {"command": "ANSWER"}
        greeting = played_sentence(ws)
        for start in range(0, len(audio), FRAME):
            ws.send_bytes(audio[start : start + FRAME])
        wait_for(lambda: core.heard, seconds=30.0)
        goodbye = played_sentence(ws)
        assert receive_command(ws) == {"command": "HANGUP"}

    print("\nheard on the call:", core.heard)
    # About a second and a half of speech each, at 16 kHz and 16 bit.
    assert greeting > SAMPLE_RATE and goodbye > SAMPLE_RATE
    assert len(core.heard) == 1 and "45" in core.heard[0]
