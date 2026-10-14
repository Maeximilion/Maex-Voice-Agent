"""Speech-to-text with Whisper, the ear of the test phase (docs/20 §6, T-10.5).

Whisper transcribes a finished piece of audio. It reports neither that the
caller is speaking nor where an utterance ends, and the call session needs
both (`speech.Heard`). `SegmentingEar` adds them with a silence detector: it
judges the caller's audio in windows of 32 ms, reports a partial with an empty
text while there is speech, and hands the utterance to the engine once it is
followed by `TURN_END_SECONDS` of silence. Engine and detector are passed in,
so the segmenting is tested without either, and another engine without an end
of utterance of its own can use it as well.

`Whisper` loads the model once per process and makes one ear per call. Its
packages (faster-whisper, which brings the detector, and numpy) are imported
where they are used: they live in the image of the voice service, not in the
API's. Audio is 16 kHz, as the media channel delivers it (docs/20 §10).
"""

import queue
import threading
from collections import deque
from collections.abc import Callable
from pathlib import Path

from api.core.logging import get_logger
from api.telephony.speech import Heard
from api.telephony.speech_spike import terms_that_fit

logger = get_logger(__name__)

SAMPLE_RATE = 16000
# Samples the detector judges at once: 32 ms.
WINDOW = 512
_WINDOW_BYTES = WINDOW * 2
_WINDOW_SECONDS = WINDOW / SAMPLE_RATE
# Silence that ends an utterance. 500 ms cut one spoken order in ten in two,
# 700 ms one in thirty-five (docs/20 §11, run of 13.10.2026).
TURN_END_SECONDS = 0.7
# Speech shorter than this is a click or a breath, reported as no speech: the
# engine is not asked, it would make words of it.
MIN_SPEECH_SECONDS = 0.25
# A partial at least this often while the caller speaks (the session wants one
# a second, `speech.Heard`).
PARTIAL_SECONDS = 0.5
# Audio kept before the first and after the last window of speech, so the
# engine hears the first and the last word whole.
PAD_SECONDS = 0.3
# From this speech probability a window is speech, below `SILENCE` it is
# silence, in between it continues what was before. The detector's defaults,
# with which the turn end was measured.
SPEECH = 0.5
SILENCE = 0.35

# The engine: the audio of one utterance and the key terms, to its text.
Transcribe = Callable[[bytes, list[str]], str]
# The detector of one call: the speech probability of the next window.
Probability = Callable[[bytes], float]


def _windows(seconds: float) -> int:
    return round(seconds / _WINDOW_SECONDS)


class SegmentingEar:
    """Implements `speech.Ear` for an engine that only transcribes.

    `feed` hands the frame to a thread of the ear and returns; detector and
    engine run there, one utterance after another. An error of either is
    raised by the next `feed`, which the session takes as a failure of the
    ear. An engine that hangs is the session's to notice: it waits for the
    final no longer than `session.UTTERANCE_SECONDS` after the last partial.
    """

    def __init__(self, transcribe: Transcribe, probability: Probability):
        self._transcribe = transcribe
        self._probability = probability
        self._frames: queue.SimpleQueue[tuple[bytes, float] | None] = (
            queue.SimpleQueue()
        )
        self._terms: list[str] = []
        self._error: Exception | None = None
        self._sink: Callable[[Heard], None] = lambda heard: None
        self.worker: threading.Thread | None = None
        # Only the worker touches what follows.
        self._pending = b""
        # The last windows before speech began, as the start of the next
        # utterance.
        self._before: deque[bytes] = deque(maxlen=_windows(PAD_SECONDS))
        # The windows of the utterance that is being said, None outside one;
        # where its speech began and ends in them; when its last speech ended;
        # windows of silence since; the length at the last partial.
        self._audio: list[bytes] | None = None
        self._first = 0
        self._end = 0
        self._ended_at = 0.0
        self._quiet = 0
        self._partial = 0

    # -- speech.Ear -------------------------------------------------------------------

    def open(self, sink: Callable[[Heard], None]) -> None:
        self._sink = sink
        self.worker = threading.Thread(target=self._work, name="ear", daemon=True)
        self.worker.start()

    def feed(self, frame: bytes, at: float) -> None:
        if self._error is not None:
            raise RuntimeError("speech-to-text failed") from self._error
        self._frames.put((frame, at))

    def expect(self, terms: list[str]) -> None:
        # One assignment: the worker reads either the old list or the new one.
        self._terms = list(terms)

    def close(self) -> None:
        # What is still being said when the line is gone is nobody's turn.
        self._frames.put(None)

    # -- the worker ---------------------------------------------------------------------

    def _work(self) -> None:
        while (item := self._frames.get()) is not None:
            try:
                self._take(*item)
            except Exception as exc:
                logger.exception("the ear failed")
                self._error = exc
                return

    def _take(self, frame: bytes, at: float) -> None:
        self._pending += frame
        while len(self._pending) >= _WINDOW_BYTES:
            window = self._pending[:_WINDOW_BYTES]
            self._pending = self._pending[_WINDOW_BYTES:]
            # `at` is when the end of the frame arrived. A window that ends
            # earlier in it ended that much earlier: a "Ja" at the start of a
            # frame that reaches past the end of a read-back was said inside
            # it (Codex PR #292, hard rule 3).
            self._judge(window, at - len(self._pending) / 2 / SAMPLE_RATE)

    def _judge(self, window: bytes, at: float) -> None:
        """One window, `at` being when its audio ended. Durations are counted
        in audio, not in arrival times: frames can come in bursts."""
        probability = self._probability(window)
        if self._audio is None:
            if probability < SPEECH:
                self._before.append(window)
                return
            self._audio = [*self._before, window]
            self._before.clear()
            self._first = len(self._audio) - 1
            self._partial = len(self._audio)
            self._mark_speech(at)
            self._sink(Heard("", final=False, ended_at=at))
            return
        self._audio.append(window)
        if probability >= SPEECH or (probability >= SILENCE and not self._quiet):
            self._mark_speech(at)
        else:
            self._quiet += 1
        if self._quiet >= _windows(TURN_END_SECONDS):
            self._finish()
        elif not self._quiet and len(self._audio) - self._partial >= _windows(
            PARTIAL_SECONDS
        ):
            self._partial = len(self._audio)
            self._sink(Heard("", final=False, ended_at=at))

    def _mark_speech(self, at: float) -> None:
        assert self._audio is not None
        self._quiet = 0
        self._end = len(self._audio)
        self._ended_at = at

    def _finish(self) -> None:
        assert self._audio is not None
        pad = _windows(PAD_SECONDS)
        audio = b"".join(self._audio[: self._end + pad])
        self._before.extend(self._audio[self._end :])
        speech = self._end - self._first >= _windows(MIN_SPEECH_SECONDS)
        self._audio = None
        if not speech:
            self._sink(Heard("", ended_at=self._ended_at, speech=False))
            return
        text = self._transcribe(audio, self._terms)
        self._sink(Heard(text.strip(), ended_at=self._ended_at))


class Whisper:
    """The engine, loaded once per process from `models` without a download;
    `ear()` makes the ear of one call. Whisper large-v3-turbo on the graphics
    card is the ear for orders (docs/20 §11, run of 13.10.2026)."""

    def __init__(
        self,
        model: str,
        models: Path,
        *,
        device: str = "cuda",
        compute_type: str = "float16",
    ):
        from faster_whisper import WhisperModel

        self._model = WhisperModel(
            model,
            device=device,
            compute_type=compute_type,
            download_root=str(models),
            local_files_only=True,
        )

    def ear(self) -> SegmentingEar:
        return SegmentingEar(self.transcribe, silero())

    def transcribe(self, pcm: bytes, terms: list[str]) -> str:
        import numpy as np

        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768
        segments, _ = self._model.transcribe(
            audio,
            language="de",
            condition_on_previous_text=False,
            hotwords=", ".join(self._fit(terms)) or None,
        )
        return " ".join(segment.text.strip() for segment in segments)

    def _fit(self, terms: list[str]) -> list[str]:
        # The engine keeps fewer than half its prompt for key terms and cuts a
        # longer list in the middle of a term (as in `speech_bench.load_ear`).
        tokenizer = self._model.hf_tokenizer

        def tokens(text: str) -> int:
            return len(tokenizer.encode(" " + text, add_special_tokens=False).ids)

        return terms_that_fit(tokens, terms, self._model.max_length // 2)


def silero() -> Probability:
    """The silence detector that ships with faster-whisper, for one call.

    It carries its state from window to window: judged one window at a time
    without it, the probabilities differ from those of the whole recording by
    up to 0.9 (checked on 14.10.2026), so the state is kept here. That reaches
    into the model's session, which `deploy/speech/` pins with the package."""
    import numpy as np
    from faster_whisper.vad import get_vad_model

    model = get_vad_model()
    h = np.zeros((1, 1, 128), dtype=np.float32)
    c = np.zeros((1, 1, 128), dtype=np.float32)
    context = np.zeros(64, dtype=np.float32)

    def probability(window: bytes) -> float:
        nonlocal h, c, context
        audio = np.frombuffer(window, dtype=np.int16).astype(np.float32) / 32768
        batch = np.concatenate([context, audio])[None, :]
        out, h, c = model.session.run(None, {"input": batch, "h": h, "c": c})
        context = audio[-64:]
        return float(out.reshape(-1)[-1])

    return probability
