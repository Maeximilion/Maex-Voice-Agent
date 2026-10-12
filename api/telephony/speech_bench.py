"""Bench of the spike T-10.3: open speech engines, heard through a telephone line.

    python -m api.telephony.speech_bench fetch   RUN_DIR
    python -m api.telephony.speech_bench measure RUN_DIR

Runs inside the image of `deploy/speech/`, started by `speech_spike.py`, which
writes `plan.json` into RUN_DIR first. `fetch` downloads the model files of the
engines into RUN_DIR/models and is the only step that needs the network.
`measure` speaks every sentence of the plan with every voice, sends it through
the band of a telephone line, lets every ear transcribe it and writes
`heard.json`: what each ear heard, how long ear and mouth took, and where a
silence detector would have ended the turn. It judges nothing (`evals/heard.py`
does) and knows nothing of the menu: sentences and key terms come with the plan.

The engine packages are imported where they are used, so the module itself
needs numpy only.
"""

import audioop  # standard library up to Python 3.12; the image pins that version
import json
import subprocess
import sys
import time
import wave
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np

from api.telephony.speech_spike import (
    MENU_MOUNT,
    key_terms_for,
    menu_errors,
    terms_that_fit,
)

LINE_RATE = 8000
EAR_RATE = 16000
BAND_HZ = (300, 3400)
_TAPS = 255

# What an ear makes of an utterance, given the key terms for it.
Ear = Callable[[np.ndarray, list[str]], str]
# The key terms of a window an ear takes: none for an ear without key terms,
# for the others what fits its prompt.
Fit = Callable[[list[str]], list[str]]


def telephone(pcm: bytes, rate: int) -> np.ndarray:
    """Speech as the ear gets it from Asterisk: the band of a telephone line,
    8 kHz A-law, and back up to the 16 kHz of the media channel (docs/20 §10).

    ponytail: band and codec only. No noise, no lost packets, no handset
    microphone, and a plain interpolation where Asterisk resamples properly.
    Real line audio is recorded with T-10.7.
    """
    samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    samples = np.convolve(samples, _band(rate), mode="same")
    line = _resample(samples, rate, LINE_RATE).clip(-32768, 32767).astype(np.int16)
    line = audioop.alaw2lin(audioop.lin2alaw(line.tobytes(), 2), 2)
    heard = _resample(np.frombuffer(line, dtype=np.int16), LINE_RATE, EAR_RATE)
    # The interpolation mirrors the speech into the band above 4 kHz.
    heard = np.convolve(heard, _band(EAR_RATE), mode="same")
    return (heard / 32768).astype(np.float32)


def _band(rate: int) -> np.ndarray:
    """Windowed-sinc band pass for BAND_HZ at the given sample rate."""
    n = np.arange(_TAPS) - _TAPS // 2
    low, high = (2 * hz / rate for hz in BAND_HZ)
    return (high * np.sinc(high * n) - low * np.sinc(low * n)) * np.hamming(_TAPS)


def _resample(samples: np.ndarray, rate: int, to_rate: int) -> np.ndarray:
    at = np.arange(int(len(samples) * to_rate / rate)) * (rate / to_rate)
    return np.interp(at, np.arange(len(samples)), samples)


def fetch(run: Path, plan: dict[str, Any]) -> None:
    models = run / "models"
    download = [sys.executable, "-m", "piper.download_voices"]
    subprocess.run(
        [*download, "--download-dir", str(models / "piper"), *plan["voices"]],
        check=True,
    )
    for spec in plan["ears"]:
        print("fetch:", spec["model"], flush=True)
        if spec["engine"] == "whisper":
            from faster_whisper import download_model

            download_model(spec["model"], cache_dir=str(models / "whisper"))
        else:
            from huggingface_hub import snapshot_download

            # Into a plain folder: the hub's own cache links each file to a
            # blob elsewhere, and the ONNX runtime refuses weights outside
            # the model's folder.
            snapshot_download(
                spec["repo"],
                local_dir=models / spec["model"],
                allow_patterns=spec["files"],
            )


def load_ear(spec: dict[str, Any], models: Path) -> tuple[Ear, Fit]:
    """The ear, and what it keeps of a window of key terms. The terms are cut
    before the ear hears, so the run can say what each utterance really got."""
    if spec["engine"] == "whisper":
        from faster_whisper import WhisperModel

        model = WhisperModel(
            spec["model"],
            device=spec["device"],
            compute_type=spec["compute"],
            download_root=str(models / "whisper"),
            local_files_only=True,
        )

        # The engine keeps fewer than half its prompt for key terms, counted
        # with a leading blank (faster-whisper `get_prompt`), and cuts a longer
        # list without a word: cut here, at a whole term.
        def tokens(text: str) -> int:
            encoded = model.hf_tokenizer.encode(" " + text, add_special_tokens=False)
            return len(encoded.ids)

        def fit(terms: list[str]) -> list[str]:
            if not spec.get("key_terms"):
                return []
            return terms_that_fit(tokens, terms, model.max_length // 2)

        def hear(audio: np.ndarray, terms: list[str]) -> str:
            segments, _ = model.transcribe(
                audio,
                language="de",
                condition_on_previous_text=False,
                hotwords=", ".join(terms) or None,
            )
            return " ".join(segment.text.strip() for segment in segments)

        return hear, fit
    if spec["engine"] == "parakeet":
        import onnx_asr

        parakeet = onnx_asr.load_model(spec["model"], models / spec["model"])
        return (
            lambda audio, terms: parakeet.recognize(audio, sample_rate=EAR_RATE),
            lambda terms: [],
        )
    raise ValueError(f"unknown ear engine {spec['engine']!r}")


def speak(voice: Any, text: str) -> tuple[bytes, int, float, float]:
    """Audio of one sentence, its sample rate, and the seconds until the first
    piece of audio and until the last."""
    started = time.monotonic()
    first = 0.0
    pieces: list[bytes] = []
    rate = 0
    for chunk in voice.synthesize(text):
        if not pieces:
            first = time.monotonic() - started
        pieces.append(chunk.audio_int16_bytes)
        rate = chunk.sample_rate
    return b"".join(pieces), rate, first, time.monotonic() - started


def gpu_mib() -> int | None:
    """Graphics memory in use on the whole card, or None without a card."""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        return int(out.split()[0])
    except (OSError, subprocess.CalledProcessError, ValueError, IndexError):
        return None


def measure(run: Path, plan: dict[str, Any]) -> dict[str, Any]:
    from faster_whisper.vad import VadOptions, get_speech_timestamps
    from piper import PiperVoice

    models = run / "models"
    (run / "samples").mkdir(exist_ok=True)
    mouths: list[dict[str, Any]] = []
    spoken: dict[tuple[str, str], np.ndarray] = {}
    for name in plan["voices"]:
        print("mouth:", name, flush=True)
        voice = PiperVoice.load(models / "piper" / f"{name}.onnx")
        speak(voice, "Guten Tag.")  # the first sentence also loads the model
        for n, sentence in enumerate(plan["agent"], start=1):
            pcm, rate, first, total = speak(voice, sentence)
            mouths.append(
                {
                    "voice": name,
                    "first_audio_seconds": round(first, 3),
                    "seconds": round(total, 3),
                    "audio_seconds": round(len(pcm) / 2 / rate, 2),
                }
            )
            _write_wav(run / "samples" / f"{name}-{n}.wav", telephone(pcm, rate))
        for row in plan["sentences"]:
            pcm, rate, _, _ = speak(voice, row["say"])
            spoken[(row["id"], name)] = telephone(pcm, rate)

    ears: dict[str, dict[str, Any]] = {}
    heard: list[dict[str, Any]] = []
    for spec in plan["ears"]:
        print("ear:", spec["id"], flush=True)
        rows: list[dict[str, Any]] = []
        try:
            before = gpu_mib() if spec.get("device") == "cuda" else None
            started = time.monotonic()
            hear, fit = load_ear(spec, models)
            hear(next(iter(spoken.values())), [])  # warm up, not timed below
            info: dict[str, Any] = {
                "load_seconds": round(time.monotonic() - started, 1)
            }
            names = plan["key_terms"]
            after = gpu_mib() if before is not None else None
            if before is not None and after is not None:
                info["gpu_mib"] = after - before
            # An ear with a sample is only timed: every n-th utterance, so
            # all voices and both kinds of sentence are in it.
            every = max(1, len(spoken) // spec.get("sample", len(spoken)))
            for (sentence_id, name), audio in list(spoken.items())[::every]:
                started = time.monotonic()
                # A window of the names per utterance, never the menu (hard
                # rule 6): sentence "d7" is dish 7, "n7" and "c7" the seventh
                # of theirs. Cut to what the ear takes before it hears.
                terms = fit(key_terms_for(names, int(sentence_id[1:])))
                text = hear(audio, terms)
                rows.append(
                    {
                        "id": sentence_id,
                        "voice": name,
                        "ear": spec["id"],
                        "text": text,
                        # Only for an ear that takes key terms: how many it got.
                        **({"key_terms": len(terms)} if spec.get("key_terms") else {}),
                        "seconds": round(time.monotonic() - started, 3),
                        "audio_seconds": round(len(audio) / EAR_RATE, 2),
                    }
                )
            # Both hold the model: with the cut still alive, the next ear
            # would load next to this one (Codex PR #268).
            del hear, fit
        except Exception as exc:  # noqa: BLE001 - whatever an engine raises
            # An engine that does not start or breaks is a result of the spike.
            ears[spec["id"]] = {"error": f"{type(exc).__name__}: {exc}"[:300]}
            continue
        ears[spec["id"]] = info
        heard += rows

    print("turn end: silence detector", flush=True)
    silence = np.zeros(EAR_RATE, dtype=np.float32)
    turn_end: list[dict[str, Any]] = []
    for (sentence_id, name), audio in spoken.items():
        padded = np.concatenate([silence, audio, silence])
        for ms in plan["silences_ms"]:
            options = VadOptions(min_silence_duration_ms=ms, speech_pad_ms=0)
            parts = get_speech_timestamps(padded, options)
            turn_end.append(
                {
                    "id": sentence_id,
                    "voice": name,
                    "silence_ms": ms,
                    "parts": len(parts),
                }
            )
    return {"ears": ears, "heard": heard, "mouths": mouths, "turn_end": turn_end}


def _write_wav(path: Path, audio: np.ndarray) -> None:
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(EAR_RATE)
        out.writeframes((audio * 32767).astype(np.int16).tobytes())


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[0] not in ("check", "fetch", "measure"):
        print(__doc__, file=sys.stderr)
        return 2
    if argv[0] == "check":
        # The menu given with --menu, by the rules of the import.
        errors = menu_errors(Path(MENU_MOUNT))
        for error in errors:
            print("menu:", error, file=sys.stderr)
        return 1 if errors else 0
    run = Path(argv[1])
    plan = json.loads((run / "plan.json").read_text(encoding="utf-8"))
    if argv[0] == "fetch":
        fetch(run, plan)
    else:
        result = measure(run, plan)
        (run / "heard.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
