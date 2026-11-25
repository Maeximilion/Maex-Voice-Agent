"""The speech engines of the service `voice` by their name in the settings.

`telephony/app.py` picks an ear and a mouth from `EARS` and `MOUTHS` by
`VOICE_EAR` and `VOICE_MOUTH` and knows no engine (docs/11 §telephony, Codex on
PR #298). Each entry loads its engine once per process, watched, runs it once,
and returns what makes the ear or mouth of one call. An engine that loads may
still fail on its first run, when the graphics card's libraries are set up:
refused at start then, not in a caller's first sentence. The first run also
takes longer (the ear 0.4 s instead of 0.1 on the workbench, 15.10.2026).
Ultra review on PR #298.

The engines' packages are imported in their loaders: they are in the image of
`voice` only (api/Dockerfile, target `voice`).
"""

from collections.abc import Callable
from pathlib import Path

from api.config import settings
from api.telephony.adapters.fake_speech import (
    DrillEar,
    FakeEar,
    FakeMouth,
    LoadEar,
    LoadMouth,
)
from api.telephony.speech import Ear, Mouth
from api.telephony.watchdog import ENGINE_LOAD_SECONDS, Watchdog, WatchedMouth

# Where the model files are inside the container: the folder of the speech kit,
# mounted read-only from deploy/speech/run/models (docker-compose.yml).
MODELS = Path("/models")
# The ear for orders by the run of 13.10.2026 (docs/20 §11).
EAR_MODEL = "large-v3-turbo"


def model_file(path: Path) -> Path:
    """`path`, which a loader needs, or `FileNotFoundError` naming it: checked
    before the engine's package is imported, so a missing mount or voice reads
    as such at start, in every image."""
    if not path.exists():
        raise FileNotFoundError(f"no model file at {path}")
    return path


def _whisper(watchdog: Watchdog) -> Callable[[], Ear]:
    models = model_file(MODELS / "whisper")
    from api.telephony.adapters.stt_whisper import (
        SAMPLE_RATE,
        WINDOW,
        SegmentingEar,
        Whisper,
        log_measure,
        silero,
    )

    gpu = settings.voice_ear_device == "cuda"
    with watchdog.watch("ear", ENGINE_LOAD_SECONDS):
        whisper = Whisper(
            EAR_MODEL,
            models,
            device=settings.voice_ear_device,
            compute_type="float16" if gpu else "int8",
        )
    if gpu:
        # The first run loads the graphics card's libraries from disk, which
        # takes longer than a run: watched as a load (own review on PR #310). On the
        # processor there are none, and the run takes 4.5 s of every start
        # (ultra review on PR #310).
        with watchdog.watch("ear", ENGINE_LOAD_SECONDS):
            whisper.transcribe(bytes(2 * SAMPLE_RATE), [])  # a second of silence
    transcribe = watchdog.watched(
        "ear", whisper.transcribe, None if gpu else ENGINE_LOAD_SECONDS
    )
    # The detector too, on one of its own: its model is loaded here, not in
    # the first call, and watched like the engines' loads. Each call gets a
    # fresh one, it keeps state from window to window (Codex on PR #310).
    with watchdog.watch("detector", ENGINE_LOAD_SECONDS):
        silero()(bytes(2 * WINDOW))
    measure = log_measure if settings.voice_ear_measure else None
    return lambda: SegmentingEar(
        transcribe, watchdog.watched("detector", silero()), measure
    )


def _piper(watchdog: Watchdog) -> Callable[[], Mouth]:
    from api.telephony.adapters.tts_piper import VOICE, piper

    voice = model_file(MODELS / "piper" / f"{settings.voice_mouth_voice or VOICE}.onnx")
    with watchdog.watch("mouth", ENGINE_LOAD_SECONDS):
        loaded = piper(voice)
    mouth = WatchedMouth(loaded, watchdog)
    # Empty without an error is a failure too, as in the session
    # (`CallSession._synthesize`) (Codex on PR #310).
    if not b"".join(mouth.speak("Guten Tag.")):
        raise RuntimeError("the mouth made no audio")
    # One for every call: it keeps nothing from one sentence to the next.
    return lambda: mouth


EARS: dict[str, Callable[[Watchdog], Callable[[], Ear]]] = {
    "fake": lambda _: FakeEar,
    "drill": lambda _: DrillEar,
    "load": lambda _: LoadEar,
    "whisper": _whisper,
}
MOUTHS: dict[str, Callable[[Watchdog], Callable[[], Mouth]]] = {
    "fake": lambda _: FakeMouth,
    "load": lambda _: LoadMouth,
    "piper": _piper,
}
