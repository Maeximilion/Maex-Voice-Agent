"""Text-to-speech with Piper, the mouth of the test phase (docs/20 §6, T-10.5).

Piper speaks at the rate of its voice (22,050 Hz for `thorsten`), the line takes
16 kHz (docs/20 §10), so every piece is converted on the way out. Which voice
is Maxi's to hear (D4); the file is a setting. The package is imported where it
is used: it lives in the image of the voice service, not in the API's.
"""

import warnings
from collections.abc import Iterable
from pathlib import Path
from typing import Any

# ponytail: standard library up to Python 3.12, which every image of the
# project pins, and deprecated there; `audioop-lts` from PyPI is the drop-in
# for a later Python.
with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)
    import audioop

SAMPLE_RATE = 16000


class PiperMouth:
    """Implements `speech.Mouth` with a loaded Piper voice."""

    def __init__(self, voice: Any, sample_rate: int = SAMPLE_RATE):
        self._voice = voice
        self._rate = sample_rate

    def speak(self, text: str) -> Iterable[bytes]:
        state = None
        for chunk in self._voice.synthesize(text):
            # The state carries the conversion over from piece to piece, so
            # the joins make no click.
            audio, state = audioop.ratecv(
                chunk.audio_int16_bytes, 2, 1, chunk.sample_rate, self._rate, state
            )
            yield audio


def piper(voice: Path, sample_rate: int = SAMPLE_RATE) -> PiperMouth:
    """The voice from its `.onnx` file (its `.onnx.json` next to it), loaded
    once per process: the mouth keeps no state between sentences."""
    from piper import PiperVoice

    return PiperMouth(PiperVoice.load(str(voice)), sample_rate)
