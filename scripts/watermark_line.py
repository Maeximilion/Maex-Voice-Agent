"""Does an audio watermark survive the phone path? (#637, Art. 50(2) AI Act)

Marks synthetic speech at 16 kHz, the rate the mouth hands the line
(`tts_piper.SAMPLE_RATE`), sends the marked and the unmarked sentence through
the stages of the repository's own line model (`speech_bench.telephone` and
`on_the_line`) and tries to find the mark at the far end. Prints one markdown
table per watermark: detection per stage, false positives on unmarked speech
through the same stages, and the time the mark takes per second of audio.

    python3 -m scripts.watermark_line --speech DIR [--methods spread,audioseal]
    python3 -m scripts.watermark_line --piper RUN_DIR [--write DIR]
    python3 -m scripts.watermark_line --detect FAR_END.wav [...]

`--speech` takes a folder of mono 16-bit WAV files of synthetic speech (never
real recordings, CLAUDE.md section 8); `--piper` speaks the sentences below
with every Piper voice in the speech kit's run folder (`models/piper`).
`--write` keeps each marked sentence as a WAV file to play on a test call;
`--detect` only looks for the mark in recordings of such a call. Needs
numpy; the neural watermarks need their packages (torch, `audioseal`,
`resemble-perth`, `wavmark`), which are not in any requirements file and are
imported where they are used. The codec stages need `ffmpeg` with libgsm and
libopus; without it they are left out and the table says so.

ponytail: a modelled line, like the ear measurements of docs/20 §10. The real
SIP leg (router, carrier, the caller's mobile network) is measured by playing
a file of `--write` on a test call and running `--detect` on the far end's
recording (docs/20 §10, "Handover: #637").
"""

import argparse
import shutil
import subprocess
import sys
import time
import wave
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

RATE = 16000
SENTENCES = [
    "Ich wiederhole: zweimal die Nummer 23 und einmal die 45, Abholung um halb acht.",
    "Ihr Tisch für vier Personen ist am Freitag um 20 Uhr reserviert.",
    "Hier ist der digitale Assistent des Restaurants. Wie kann ich helfen?",
    "Die Lieferung in Ihre Straße kostet drei Euro fünfzig.",
    "Ja, gerne.",
]
SEEDS = (0, 1, 2)  # the seeds of scripts/ear_floor.py
FRAME = RATE // 50  # 20 ms, one RTP packet
# Spread spectrum: chips at 8 kHz, one period of 0.256 s, folded over the
# sentence and correlated at every lag, so the far end needs no sync.
PERIOD = 2048
WSR_DB = -20.0  # mark to speech per 20 ms frame, in the band of the line
DRIFTS_PPM = range(-150, 151, 10)  # two clocks on the path never agree
SPREAD_THRESHOLD = 6.0  # z score; unmarked speech stayed under 5.7 (900 tries)
# One period of the spread mark in whole 20 ms frames, plus the fastest drift:
# a shorter file cannot carry the mark at all (#637).
SHORTEST = 0.27
# What each neural watermark needs, for the message when it does not load.
PACKAGES = {
    "audioseal": "torch audioseal",
    "perth": 'torch resemble-perth "setuptools<81"',
    "wavmark": "torch wavmark",
}


class Mark(Protocol):
    name: str
    threshold: float

    def embed(self, x: Any) -> Any: ...

    def detect(self, y: Any) -> tuple[float, float | None]:
        """Detection score and, for a mark with a payload, its bit accuracy."""
        ...


def pcm16(x: Any) -> bytes:
    import numpy as np

    return bytes((np.clip(x, -1, 1) * 32767).astype(np.int16).tobytes())


def lose(x: Any, share: float, seed: int) -> Any:
    """Packet loss without concealment: whole 20 ms frames set to zero, at
    least one per sentence."""
    import numpy as np

    y = x.copy()
    rng = np.random.default_rng(seed)
    n = len(y) // FRAME
    gone = rng.random(n) < share
    if n and not gone.any():
        gone[rng.integers(n)] = True
    for i in np.flatnonzero(gone):
        y[i * FRAME : (i + 1) * FRAME] = 0
    return y


def drift(x: Any, ppm: float) -> Any:
    """The far end's clock running `ppm` fast."""
    import numpy as np

    d = ppm * 1e-6
    return np.interp(np.arange(int(len(x) / (1 + d))) * (1 + d), np.arange(len(x)), x)


def codec(x: Any, args: list[str], fmt: str) -> Any:
    """Through a speech codec at 8 kHz and back to 16 kHz with ffmpeg: the
    caller's mobile leg recodes the call (AMR or EVS there; not in a stock
    ffmpeg, so GSM full rate and Opus stand in)."""
    import numpy as np

    line = np.interp(np.arange(len(x) // 2) * 2.0, np.arange(len(x)), x)
    raw = ["-f", "s16le", "-ar", "8000", "-ac", "1"]
    run = ["ffmpeg", "-loglevel", "error"]
    encoded = subprocess.run(
        [*run, *raw, "-i", "-", *args, "-f", fmt, "-"],
        input=pcm16(line),
        capture_output=True,
        check=True,
    ).stdout
    decoded = subprocess.run(
        [*run, "-f", fmt, "-i", "-", *raw, "-"],
        input=encoded,
        capture_output=True,
        check=True,
    ).stdout
    y = np.frombuffer(decoded, dtype=np.int16).astype(np.float32) / 32768
    return np.interp(np.arange(2 * len(y)) / 2, np.arange(len(y)), y)


def stages(x: Any, codecs: bool) -> dict[str, Any]:
    """The far end's audio at 16 kHz after each stage of the path, cumulative.
    Packet loss, offset, drift and codecs come on top of the noisy line."""
    import numpy as np

    from api.telephony import speech_bench as bench

    out: dict[str, Any] = {"clean": x}
    band = np.convolve(x * 32768, bench._band(RATE), mode="same")
    out["band 300-3400 Hz"] = band / 32768
    up = bench._resample(bench._resample(band, RATE, bench.LINE_RATE), 8000, RATE)
    out["+ 8 kHz"] = np.convolve(up, bench._band(RATE), mode="same") / 32768
    out["+ A-law"] = bench.telephone(pcm16(x), RATE)
    for seed in SEEDS:
        out[f"+ line noise, seed {seed}"] = bench.on_the_line(pcm16(x), RATE, seed=seed)
    noisy = out["+ line noise, seed 0"]
    for share in (0.01, 0.03):
        out[f"noisy + {share:.0%} packets lost"] = lose(noisy, share, seed=7)
    late = bench.on_the_line(pcm16(x), RATE, before=0.37, seed=0)
    out["noisy + 0.37 s offset"] = late
    for ppm in (20, 100):
        out[f"noisy + offset + {ppm} ppm drift"] = drift(late, ppm)
    if codecs:
        out["noisy + GSM-FR 13 kbit/s"] = codec(noisy, ["-c:a", "libgsm"], "gsm")
        opus = ["-c:a", "libopus", "-b:a", "12k", "-application", "voip"]
        out["noisy + Opus 12 kbit/s"] = codec(noisy, opus, "ogg")
    return out


class Spread:
    """Classic spread spectrum, a zero-bit mark (present or not): a keyed
    pseudo-random pattern in the band of the line under the speech envelope.
    No package, the textbook method (Cox et al. 1997, Kirovski and Malvar 2003)."""

    name = "spread"
    threshold = SPREAD_THRESHOLD

    def __init__(self, key: int = 637) -> None:
        import numpy as np

        chips = np.random.default_rng(key).choice([-1.0, 1.0], PERIOD)
        spectrum = np.fft.rfft(chips) * _in_band(PERIOD, 8000)
        line = np.fft.irfft(spectrum, PERIOD)
        wide = np.zeros(PERIOD + 1, dtype=complex)
        wide[: len(spectrum)] = spectrum * 2
        mouth = np.fft.irfft(wide, 2 * PERIOD)
        self._line, self._mouth = line / line.std(), mouth / mouth.std()

    def embed(self, x: Any) -> Any:
        import numpy as np

        from api.telephony.speech_bench import _band

        band = np.convolve(x, _band(RATE), mode="same")
        n = -(-len(x) // FRAME)
        rms = np.array([band[i * FRAME : (i + 1) * FRAME].std() for i in range(n)])
        gain = np.repeat(rms, FRAME)[: len(x)] * 10 ** (WSR_DB / 20)
        gain = np.convolve(gain, np.ones(FRAME) / FRAME, mode="same")
        return x + gain * np.resize(self._mouth, len(x))

    def detect(self, y: Any) -> tuple[float, float | None]:
        return max(self._z(y, ppm * 1e-6) for ppm in DRIFTS_PPM), None

    def _z(self, y: Any, d: float) -> float:
        """z score of the best circular lag at one clock drift."""
        import numpy as np

        at = np.arange(int(len(y) / 2 / (1 + d))) * 2.0 * (1 + d)
        line = np.interp(at, np.arange(len(y)), y)
        n = len(line) // 160
        frames = line[: n * 160].reshape(n, 160)
        rms = frames.std(axis=1, keepdims=True)
        flat = np.where(rms > 1e-4, frames / np.maximum(rms, 1e-9), 0).ravel()
        folds = len(flat) // PERIOD
        if folds == 0:
            return 0.0
        folded = flat[: folds * PERIOD].reshape(folds, PERIOD).sum(axis=0)
        spectrum = np.fft.rfft(folded) * _in_band(PERIOD, 8000)
        spectrum /= np.maximum(np.abs(spectrum), 1e-9)  # whitened: speech is loud
        corr = np.fft.irfft(spectrum * np.conj(np.fft.rfft(self._line)), PERIOD)
        return float((corr.max() - corr.mean()) / corr.std())


def _in_band(n: int, rate: int) -> Any:
    import numpy as np

    f = np.fft.rfftfreq(n, 1 / rate)
    return ((f >= 300) & (f <= 3400)).astype(float)


class AudioSealMark:
    """Meta's AudioSeal, 16 bits; its weights come from Hugging Face."""

    name = "audioseal"
    threshold = 0.5

    def __init__(self) -> None:
        import torch
        from audioseal import AudioSeal

        self._torch = torch
        self._gen = AudioSeal.load_generator("audioseal_wm_16bits")
        self._det = AudioSeal.load_detector("audioseal_detector_16bits")
        seeded = torch.Generator().manual_seed(637)
        self._msg = torch.randint(0, 2, (1, 16), generator=seeded)

    def _tensor(self, x: Any) -> Any:
        import numpy as np

        return self._torch.from_numpy(np.asarray(x, dtype=np.float32)).reshape(1, 1, -1)

    def embed(self, x: Any) -> Any:
        t = self._tensor(x)
        with self._torch.no_grad():
            marked = t + self._gen.get_watermark(t, RATE, message=self._msg)
        return marked.reshape(-1).numpy()

    def detect(self, y: Any) -> tuple[float, float | None]:
        with self._torch.no_grad():
            score, msg = self._det.detect_watermark(self._tensor(y), RATE)
        return float(score), float((msg.round() == self._msg).float().mean())


class PerthMark:
    """Resemble AI's PerTh implicit watermarker; its weights ship in the wheel.
    Works at 32 kHz and below 2 kHz; the score is the mean of its per-frame
    detections. Needs `pkg_resources` (setuptools below 81)."""

    name = "perth"
    threshold = 0.5

    def __init__(self) -> None:
        import perth

        self._wm = perth.PerthImplicitWatermarker(device="cpu")

    def embed(self, x: Any) -> Any:
        import numpy as np

        marked = self._wm.apply_watermark(
            np.asarray(x, dtype=np.float32), sample_rate=RATE
        )
        return np.asarray(marked, dtype=np.float32)[: len(x)]

    def detect(self, y: Any) -> tuple[float, float | None]:
        import numpy as np

        frames = self._wm.get_watermark(
            np.asarray(y, dtype=np.float32), sample_rate=RATE, round=False
        )
        return float(np.mean(frames)), None


class WavMarkMark:
    """WavMark, 16 bits in each second; its weights come from Hugging Face."""

    name = "wavmark"
    threshold = 0.5

    def __init__(self) -> None:
        import numpy as np
        import wavmark

        self._wavmark = wavmark
        self._model = wavmark.load_model().to("cpu")
        self._payload = np.random.default_rng(637).integers(0, 2, 16)

    def embed(self, x: Any) -> Any:
        marked, _ = self._wavmark.encode_watermark(
            self._model, x, self._payload, show_progress=False
        )
        return marked

    def detect(self, y: Any) -> tuple[float, float | None]:
        import numpy as np

        got, _ = self._wavmark.decode_watermark(self._model, y, show_progress=False)
        if got is None:
            return 0.0, None
        return 1.0, float((np.asarray(got) == self._payload).mean())


METHODS: dict[str, Callable[[], Mark]] = {
    "spread": Spread,
    "audioseal": AudioSealMark,
    "perth": PerthMark,
    "wavmark": WavMarkMark,
}


def read_wav(path: Path) -> Any:
    """A mono 16-bit file as float samples at 16 kHz; any other rate is
    resampled, any other format refused with what to do about it."""
    import numpy as np

    from api.telephony.speech_bench import _resample

    convert = f"convert it: ffmpeg -i {path.name} -ac 1 -c:a pcm_s16le out.wav"
    try:
        with wave.open(str(path)) as w:
            if w.getnchannels() != 1 or w.getsampwidth() != 2:
                raise ValueError(f"{path.name}: mono 16-bit WAV expected, {convert}")
            rate = w.getframerate()
            pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    except (wave.Error, EOFError) as error:
        raise ValueError(
            f"{path.name}: not a PCM WAV file ({error}), {convert}"
        ) from error
    x = pcm.astype(np.float32) / 32768
    return x if rate == RATE else _resample(x, rate, RATE)


def piper_speech(run: Path) -> dict[str, Any]:
    """Every sentence spoken by every voice of the kit, at the mouth's 16 kHz."""
    import numpy as np
    from piper import PiperVoice

    from api.telephony.speech_bench import _resample, speak

    spoken = {}
    for onnx in sorted((run / "models" / "piper").glob("*.onnx")):
        voice = PiperVoice.load(str(onnx))
        for n, sentence in enumerate(SENTENCES, start=1):
            pcm, rate, _, _ = speak(voice, sentence)
            x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768
            spoken[f"{onnx.stem}-{n}"] = _resample(x, rate, RATE)
    return spoken


def measure(mark: Mark, speech: dict[str, Any], codecs: bool) -> dict[str, Any]:
    """Per stage the marked scores, the unmarked scores and the bit accuracies;
    the time to mark per second of audio, warm."""
    rows: dict[str, dict[str, list[float]]] = {}
    seconds = audio = 0.0
    mark.embed(next(iter(speech.values())))  # loads and warms the model
    for x in speech.values():
        started = time.perf_counter()
        marked = mark.embed(x)
        seconds += time.perf_counter() - started
        audio += len(x) / RATE
        for kind, signal in (("marked", marked), ("unmarked", x)):
            for stage, y in stages(signal, codecs).items():
                score, bits = mark.detect(y)
                row = rows.setdefault(stage, {"marked": [], "unmarked": [], "bits": []})
                row[kind].append(score)
                if kind == "marked" and bits is not None:
                    row["bits"].append(bits)
    return {"ms_per_second": 1000 * seconds / audio, "rows": rows}


def table(mark: Mark, result: dict[str, Any], codecs: bool) -> str:
    t = mark.threshold
    lines = [
        f"**{mark.name}**: {result['ms_per_second']:.1f} ms to mark one second of "
        f"audio on this machine; found where the score is above {t}.",
        "",
        "| Stage | Marked found | Marked score, min | Unmarked found | "
        "Unmarked score, max | Bits right |",
        "|---|---|---|---|---|---|",
    ]
    for stage, row in result["rows"].items():
        marked, unmarked, bits = row["marked"], row["unmarked"], row["bits"]
        hit = sum(s > t for s in marked)
        false = sum(s > t for s in unmarked)
        right = f"{sum(bits) / len(bits):.1%}" if bits else "-"
        lines.append(
            f"| {stage} | {hit} of {len(marked)} | {min(marked):.2f} | "
            f"{false} of {len(unmarked)} | {max(unmarked):.2f} | {right} |"
        )
    if not codecs:
        lines += ["", "Codec stages left out: no ffmpeg on this machine."]
    return "\n".join(lines)


def write_wav(path: Path, x: Any, rate: int = RATE) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm16(x))


def detect_files(mark: Mark, files: list[Path]) -> str:
    """Scores of recordings from the far end of a real call (`--detect`)."""
    lines = [f"**{mark.name}**, found where the score is above {mark.threshold}:", ""]
    for path in files:
        y = read_wav(path)
        if len(y) < SHORTEST * RATE:
            lines.append(
                f"- {path.name}: too short to carry the mark "
                f"({len(y) / RATE:.2f} s, at least {SHORTEST} s)"
            )
            continue
        score, bits = mark.detect(y)
        found = "found" if score > mark.threshold else "not found"
        right = "" if bits is None else f", bits right {bits:.1%}"
        lines.append(f"- {path.name}: {found}, score {score:.2f}{right}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--speech", type=Path, help="folder of synthetic WAV files")
    source.add_argument("--piper", type=Path, help="speech kit run folder")
    source.add_argument(
        "--detect", type=Path, nargs="+", help="far-end recordings, detect only"
    )
    parser.add_argument("--methods", default="spread,audioseal,perth,wavmark")
    parser.add_argument(
        "--write", type=Path, help="also write each marked sentence here, to play"
    )
    args = parser.parse_args(argv)
    unknown = set(args.methods.split(",")) - set(METHODS)
    if unknown:
        parser.error(f"unknown methods {sorted(unknown)}, known: {', '.join(METHODS)}")
    try:
        import numpy  # noqa: F401
    except ModuleNotFoundError:
        print("needs numpy, not in the api image: pip install numpy", file=sys.stderr)
        return 2
    speech: dict[str, Any] = {}
    try:
        if args.speech:
            for p in sorted(args.speech.glob("*.wav")):
                x = read_wav(p)
                if len(x) < SHORTEST * RATE:
                    print(
                        f"{p.name}: too short to carry the mark, left out",
                        file=sys.stderr,
                    )
                    continue
                speech[p.stem] = x
        elif args.piper:
            speech = piper_speech(args.piper)
        else:
            for p in args.detect:
                read_wav(p)  # refuses a wrong file before any model loads
    except (ValueError, OSError) as error:  # a wrong or missing file
        print(error, file=sys.stderr)
        return 2
    except ModuleNotFoundError as error:
        print(f"--piper needs the package piper-tts ({error})", file=sys.stderr)
        return 2
    if not speech and not args.detect:
        print("no speech found", file=sys.stderr)
        return 1
    codecs = shutil.which("ffmpeg") is not None
    if speech:
        seconds = sum(len(x) for x in speech.values()) / RATE
        print(f"{len(speech)} sentences, {seconds:.0f} s\n")
    for name in args.methods.split(","):
        try:
            mark = METHODS[name]()
        except Exception as error:  # noqa: BLE001 - a missing model is a result too
            print(f"**{name}**: did not load ({type(error).__name__}: {error})")
            if isinstance(error, ModuleNotFoundError) and name in PACKAGES:
                print(f"in a throwaway venv: pip install {PACKAGES[name]}")
            print()
            continue
        if args.detect:
            print(detect_files(mark, args.detect), end="\n\n")
            continue
        if args.write:
            args.write.mkdir(parents=True, exist_ok=True)
            for stem, x in speech.items():
                write_wav(args.write / f"{stem}-{name}.wav", mark.embed(x))
        print(table(mark, measure(mark, speech, codecs), codecs), end="\n\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
