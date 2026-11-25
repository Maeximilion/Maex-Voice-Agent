"""Measure the floor of the ear (#678): how many windows of speech short answers
and line noise reach, against `MIN_SPEECH_SECONDS`.

Runs in the image of the service `voice`, which has the detector, the engines
and numpy, with the repository mounted:

    docker run --rm --gpus all -v "$PWD":/app -v RUN_DIR:/run-dir:ro \\
      --entrypoint python maex-voice-agent-voice -m scripts.ear_floor \\
      --run-dir /run-dir [--whisper] [--device cpu]

RUN_DIR is the speech kit's run folder (`deploy/speech/run`, with `models/`).
Short answers are spoken by every Piper voice there and sent through the
telephone line of `speech_bench.on_the_line`; clicks and breaths are
synthesised. Nothing is stored, only counts are printed as markdown.

On a test call the same numbers come from the log: `VOICE_EAR_MEASURE=true`
makes the service log one line per utterance (`stt_whisper.log_measure`);
`--from-log FILE` (a call of short answers) and `--noise-log FILE` (a call of
clicks and breaths) turn those lines into a table and the verdict. A click that
logs no line was never counted by the detector: below any floor.
"""

import argparse
import re
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

SAMPLE_RATE = 16000
ANSWERS = ["Ja.", "Nein.", "Zwei.", "Drei.", "Jo.", "Okay.", "Genau.", "Stimmt."]
SEEDS = (0, 1, 2)
# Quiet before the sound (the detector's state depends on it, docs/20 §12) and
# after it (longer than the turn end, so the utterance is finished).
BEFORE, AFTER = 2.0, 1.5
LOG_LINE = re.compile(r"ear measure: windows=(\d+) floor=(\d+) below=(\w+) heard=(.*)")


def clicks_and_breaths() -> dict[str, Any]:
    """Noises of a line as 16-bit samples: bursts of broadband noise of 2 to
    80 ms (a click, a crackle), two clicks 200 ms apart, and breaths of 300 and
    600 ms (band noise under a slow envelope). ponytail: synthetic, real line
    noise comes from the test calls."""
    import numpy as np

    rng = np.random.default_rng(7)

    def burst(ms: int, peak: float) -> Any:
        n = SAMPLE_RATE * ms // 1000
        return rng.normal(0, peak, n) * np.hanning(n + 2)[1:-1]

    def breath(ms: int) -> Any:
        n = SAMPLE_RATE * ms // 1000
        noise = np.convolve(rng.normal(0, 1500, n), np.ones(8) / 8, mode="same")
        return noise * np.hanning(n)

    gap = np.zeros(SAMPLE_RATE // 5)
    noises = {f"click {ms} ms": burst(ms, 9000) for ms in (2, 10, 30, 80)}
    noises["two clicks 30 ms, 200 ms apart"] = np.concatenate(
        [burst(30, 9000), gap, burst(30, 9000)]
    )
    noises.update({f"breath {ms} ms": breath(ms) for ms in (300, 600)})

    # Voiced noise, the likeliest to look like speech to the detector: a buzz
    # of 110 Hz with its overtones (a hum, a hum on the line).
    def buzz(ms: int) -> Any:
        t = np.arange(SAMPLE_RATE * ms // 1000) / SAMPLE_RATE
        tone = sum(np.sin(2 * np.pi * 110 * k * t) / k for k in range(1, 12))
        return 6000 * tone * np.hanning(len(t))

    noises.update({f"buzz {ms} ms": buzz(ms) for ms in (100, 200, 300, 500)})
    return {k: v.clip(-32768, 32767).astype(np.int16) for k, v in noises.items()}


def count(
    pcm: bytes, rate: int, seed: int, probability: Any, transcribe: Any
) -> tuple[int, str | None]:
    """Windows of speech the ear counts in `pcm` on the line, and what the
    engine heard (None: no engine, or the detector never began an utterance)."""
    from api.telephony.adapters.stt_whisper import Measure, SegmentingEar
    from api.telephony.speech_bench import on_the_line

    line = (on_the_line(pcm, rate, BEFORE, AFTER, seed) * 32767).astype("int16")
    seen: list[Measure] = []
    ear = SegmentingEar(transcribe, probability, seen.append)
    ear.open(lambda heard: None)
    audio = line.tobytes()
    frame = 640  # 20 ms, what the media channel sends
    for start in range(0, len(audio), frame):
        ear.feed(audio[start : start + frame], (start + frame) / 2 / SAMPLE_RATE)
    ear._frames.put(None)  # let the worker take everything fed, then stop
    assert ear.worker is not None
    ear.worker.join(60)
    # An utterance is one Measure; two clicks apart can be two of them.
    return (max((m.windows for m in seen), default=0), seen[-1].heard if seen else None)


def measure(
    run: Path, whisper: bool, device: str
) -> list[tuple[str, str, int, str | None]]:
    """(group, case, windows, heard) for every answer x voice x seed and every
    noise x seed."""
    from piper import PiperVoice

    from api.telephony.adapters.stt_whisper import Whisper, silero

    transcribe = lambda pcm, terms: ""  # noqa: E731
    if whisper:
        transcribe = Whisper(
            "large-v3-turbo",
            run / "models" / "whisper",
            device=device,
            compute_type="float16" if device == "cuda" else "int8",
        ).transcribe
    rows: list[tuple[str, str, int, str | None]] = []
    for onnx in sorted((run / "models" / "piper").glob("*.onnx")):
        voice = PiperVoice.load(str(onnx))
        for answer in ANSWERS:
            pcm = b"".join(c.audio_int16_bytes for c in voice.synthesize(answer))
            rate = voice.config.sample_rate
            for seed in SEEDS:
                n, heard = count(pcm, rate, seed, silero(), transcribe)
                rows.append(("answer", f"{answer} {onnx.stem[6:]}", n, heard))
    # A short piece of a spoken word, as a dropout or a cut line leaves it.
    voice = PiperVoice.load(str(sorted((run / "models" / "piper").glob("*.onnx"))[0]))
    word = b"".join(c.audio_int16_bytes for c in voice.synthesize("Nein."))
    rate = voice.config.sample_rate
    cut = {f"cut word {ms} ms": word[: 2 * rate * ms // 1000] for ms in (60, 120, 180)}
    for name, pcm in cut.items():
        for seed in SEEDS:
            n, heard = count(pcm, rate, seed, silero(), transcribe)
            rows.append(("noise", name, n, heard))
    for name, samples in clicks_and_breaths().items():
        for seed in SEEDS:
            n, heard = count(samples.tobytes(), SAMPLE_RATE, seed, silero(), transcribe)
            rows.append(("noise", name, n, heard))
    return rows


def table(rows: list[tuple[str, str, int, str | None]], floor: int) -> str:
    """Markdown: one line per answer word or noise, windows min/median/max,
    runs below the floor, and the decision rule's verdict."""
    groups: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))
    heard: dict[str, list[str]] = defaultdict(list)
    for group, case, windows, text in rows:
        name = case.split(" ")[0] if group == "answer" else case
        groups[group][name].append(windows)
        if text:
            heard[name].append(text)
    out = [
        f"Floor now: {floor} windows of 32 ms.",
        "",
        "| Group | Case | Runs | Windows min / median / max | Below the floor | Heard |",
        "|---|---|---|---|---|---|",
    ]
    for group in ("answer", "noise"):
        for name, counts in groups[group].items():
            below = sum(c < floor for c in counts)
            texts = ", ".join(sorted(set(heard[name]))) or "-"
            out.append(
                f"| {group} | {name} | {len(counts)} | {min(counts)} / "
                f"{statistics.median(counts):g} / {max(counts)} | {below} | {texts} |"
            )
    voices: dict[str, list[int]] = defaultdict(list)
    for group, case, windows, _ in rows:
        if group == "answer":
            voices[case.split(" ", 1)[1]].append(windows)
    out += [
        "",
        "| Voice | Runs | Windows min / median / max | Below the floor |",
        "|---|---|---|---|",
    ]
    for voice, counts in voices.items():
        out.append(
            f"| {voice} | {len(counts)} | {min(counts)} / "
            f"{statistics.median(counts):g} / {max(counts)} | {sum(c < floor for c in counts)} |"
        )
    noise = [c for counts in groups["noise"].values() for c in counts]
    answers = [c for counts in groups["answer"].values() for c in counts]
    out += ["", "0 windows: the detector never began an utterance, so no final at all."]
    if noise and answers:
        out += ["", verdict(max(noise), min(answers))]
    return "\n".join(out)


def summary(
    answers: list[tuple[str, str, int, str | None]],
    noises: list[tuple[str, str, int, str | None]],
) -> str:
    """Lowest answer and highest noise of the test calls, and the verdict."""
    if not answers or not noises:
        return "Both logs are needed for a verdict: short answers and noises."
    return verdict(max(n for _, _, n, _ in noises), min(n for _, _, n, _ in answers))


def verdict(noise_max: int, answer_min: int) -> str:
    """The decision rule: a floor F separates when every noise stays below it
    (F > noise_max) and every answer reaches it (F <= answer_min)."""
    if noise_max < answer_min:
        return (
            f"Highest noise {noise_max} windows, lowest answer {answer_min}: any "
            f"floor from {noise_max + 1} to {answer_min} windows separates them "
            f"(smallest: {noise_max + 1})."
        )
    return (
        f"Highest noise {noise_max} windows, lowest answer {answer_min}: no floor "
        "separates them; the floor stays and the text of the short finals decides."
    )


def from_log(path: Path, group: str) -> list[tuple[str, str, int, str | None]]:
    """The lines of a test call's log, every line one utterance, all of one
    group (`answer` or `noise`) as the test call made them."""
    rows: list[tuple[str, str, int, str | None]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if found := LOG_LINE.search(line):
            windows, _, _, text = found.groups()
            rows.append(
                (group, f"{group} {len(rows) + 1}", int(windows), text.strip("'\""))
            )
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.ear_floor", description=__doc__
    )
    parser.add_argument(
        "--run-dir", type=Path, help="speech kit run folder with models/"
    )
    parser.add_argument("--whisper", action="store_true", help="also ask Whisper")
    parser.add_argument("--device", choices=["cuda", "cpu"], default="cuda")
    parser.add_argument(
        "--from-log", type=Path, help="log of a test call with short answers"
    )
    parser.add_argument(
        "--noise-log", type=Path, help="log of a test call with clicks and breaths"
    )
    args = parser.parse_args(argv)
    from api.telephony.adapters.stt_whisper import MIN_SPEECH_SECONDS, WINDOW

    floor = round(MIN_SPEECH_SECONDS / (WINDOW / SAMPLE_RATE))
    if args.from_log or args.noise_log:
        answers = from_log(args.from_log, "answer") if args.from_log else []
        noises = from_log(args.noise_log, "noise") if args.noise_log else []
        print(log_table(answers + noises, floor))
        print()
        print(summary(answers, noises))
        return 0
    if not args.run_dir:
        parser.error("--run-dir or --from-log")
    print(table(measure(args.run_dir, args.whisper, args.device), floor))
    return 0


def log_table(rows: list[tuple[str, str, int, str | None]], floor: int) -> str:
    """One line per utterance of a test call, in the order said."""
    out = ["| Utterance | Windows | Below the floor | Heard |", "|---|---|---|---|"]
    out += [
        f"| {case} | {n} | {n < floor} | {text or '-'} |" for _, case, n, text in rows
    ]
    return "\n".join([f"Floor now: {floor} windows of 32 ms.", "", *out])


if __name__ == "__main__":
    sys.exit(main())
