"""An ear or mouth without its model files is not loaded, so `voice` does not
start (T-1.11): the dialplan rings the team meanwhile.

Runs where the engines' packages are, in the image of `voice`. CI builds that
image and runs this file in it (.github/workflows/ci.yml, job `voice-image`),
on the processor and without a network, so nothing is downloaded. Without the
packages, in the API's image, `test_telephony_voice.py` covers the refusal.
"""

import pytest

pytest.importorskip("faster_whisper")
pytest.importorskip("piper")

from api.config import settings
from api.telephony import app as voice_app


@pytest.fixture
def no_model_files(monkeypatch, tmp_path):
    monkeypatch.setattr(voice_app, "MODELS", tmp_path)
    monkeypatch.setattr(settings, "voice_ear_device", "cpu")


@pytest.mark.parametrize("load", [voice_app._whisper, voice_app._piper])
def test_an_engine_without_its_model_files_is_not_loaded(no_model_files, load):
    # For the files: a broken loader (a renamed keyword) raises otherwise.
    with pytest.raises(FileNotFoundError):
        load(voice_app.Watchdog())
