"""An ear or mouth without its model files is not loaded, so `voice` does not
start (T-1.11): the dialplan rings the team meanwhile.

Runs where the engines' packages are, in the image of `voice`. CI builds that
image and runs this file in it (.github/workflows/ci.yml, job `voice-image`),
on the processor and without a network, so nothing is downloaded. Without the
packages, in the API's image, `test_telephony_voice.py` covers the refusal and
the loaders' own check for the file they name (`speech_engines.model_file`).
"""

import pytest

pytest.importorskip("faster_whisper")
pytest.importorskip("piper")

from api.config import settings
from api.telephony import watchdog as voice_watchdog
from api.telephony.adapters import speech_engines, tts_piper


@pytest.fixture
def no_model_files(monkeypatch, tmp_path):
    # What the loaders check themselves is there, empty, so the packages' own
    # lookups are what refuses.
    (tmp_path / "whisper").mkdir()
    (tmp_path / "piper").mkdir()
    (tmp_path / "piper" / f"{tts_piper.VOICE}.onnx").touch()
    monkeypatch.setattr(speech_engines, "MODELS", tmp_path)
    monkeypatch.setattr(settings, "voice_ear_device", "cpu")
    monkeypatch.setattr(settings, "voice_mouth_voice", "")


@pytest.mark.parametrize("load", [speech_engines._whisper, speech_engines._piper])
def test_an_engine_without_its_model_files_is_not_loaded(no_model_files, load):
    # For the files: a broken loader (a renamed keyword) raises otherwise.
    with pytest.raises(FileNotFoundError):
        load(voice_watchdog.Watchdog())
