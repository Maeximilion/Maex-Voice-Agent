"""Audio evals (T-10.7, docs/08 §8, docs/20 §6): the cases of `evals/cases/` spoken.

    make eval-voice [MODEL=<name>] [LIMIT=<n>]
    python -m evals.voice --run-dir RUN [--cases DIR] [--model NAME] [--limit N]

Runs in the image of `deploy/speech/` with the model files of the kit
(`SPEECH_MODELS`) and a database server (`DATABASE_URL`); the ear is the one of
the service `voice`, loaded by its own loaders (`telephony/app.py`). One report,
three parts:

1. **Heard.** Every caller line of the cases is spoken by a Piper voice that is
   not the agent's, sent through the modelled telephone line
   (`speech_bench.telephone`) and heard by Whisper behind the segmenting ear. No
   key terms: the handler hands them per turn from what the agent just said,
   which a line spoken outside its call does not have, so recognition here is
   the floor. Numbers and dish names of each line are judged as in the spike
   (`evals/heard.py`).
2. **The cases played with what the ear heard**, by the same runner and judge
   (`evals/runner.py`), next to the same cases as text, case by case. A line the
   ear found no speech in never becomes a turn, as on the phone.
3. **Hard, both 0.** Noise: in every case that ends confirmed, each yes is
   replaced by what the ear made of line noise; no such case may end confirmed.
   A yes inside the read-back: the read-back the agent said in that case is
   spoken by the agent's mouth into a real call session (`telephony/session.py`)
   and the caller's "Ja" ends inside it; the session must not hand it to the
   core. The same "Ja" after the read-back is the control and must arrive.

Latency by stage: the agent's mouth to the first audio of a read-back, the
ear after the last frame (fed faster than real time, so an upper bound), the
core per turn with each case's setup (an upper bound). The silence that ends a turn is the ear's own (704 ms,
`adapters/stt_whisper.py`) and is not measured again. Writes `voice.json` and
`voice.md` into RUN; the runner's reports of both plays go to RUN/text and
RUN/voice, never to `evals/reports/`, so a voice run is never a baseline of the
text suite.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import statistics
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from api.agent.consent import is_yes
from api.telephony.session import CallSession, Lines
from api.telephony.speech import Ear, Heard, Mouth, SpeechUsage
from evals.heard import EVAL, dish_verdict, number_verdict
from evals.number_eval import resolve
from evals.runner import CASES, load_cases, run
from sim.replay import customer_lines

SAMPLE_RATE = 16000
FRAME = 640  # 20 ms of 16 bit audio
CALLER_VOICE = "de_DE-kerstin-low"
# Long enough for the ear's 704 ms of silence to close an utterance.
PAD_BEFORE, PAD_AFTER = 1.0, 2.0
# How long the ear may take for a final after the last frame.
EAR_WAIT = 30.0
# The caller's answer ends this long before the read-back ends, or this long
# after it. After: a second, because the voice ends a sentence with silence, so
# its speech ends before its audio (one of five read-backs missed at 0.3 s).
INSIDE, AFTER = 0.4, 1.0
# Without a partial within this long after the last frame the ear found no
# speech, and no final comes (`Heard`: every utterance a partial announced ends
# with a final, a silent line reports nothing).
NO_SPEECH_WAIT = 2.0
NOISE_SECONDS = 3.0
# The caller's answer to the read-back. Not a bare "Ja": in the caller's voice
# it lasts 0.2 s, under the ear's 256 ms of speech, and is no utterance at all
# (`adapters/stt_whisper.py`), so neither the check nor its control would say
# anything (measured 29.10.2026).
ANSWER = "Ja, passt so."

# What a caller line turns into on the phone: the text the ear heard, or None
# when it heard no speech and no turn begins.
Hear = Callable[[str], str | None]


# --- 1. Heard ----------------------------------------------------------------------


def pcm(mouth: Mouth, text: str) -> tuple[bytes, float]:
    """The sentence as 16 bit audio and the seconds to the mouth's first piece."""
    started = time.monotonic()
    pieces = iter(mouth.speak(text))
    first = next(pieces, b"")
    to_first = time.monotonic() - started
    return first + b"".join(pieces), to_first


def hear_audio(ear: Ear, audio: bytes) -> tuple[Heard | None, float]:
    """Feed `audio`, padded with silence, as 20 ms frames on audio time and wait
    for the final. The final and the seconds the ear took after the last frame."""
    padded = bytes(int(2 * SAMPLE_RATE * PAD_BEFORE)) + audio
    padded += bytes(int(2 * SAMPLE_RATE * PAD_AFTER))
    heard: list[Heard] = []
    ear.open(heard.append)
    try:
        for start in range(0, len(padded), FRAME):
            ear.feed(padded[start : start + FRAME], (start + FRAME) / 2 / SAMPLE_RATE)
        fed = time.monotonic()
        while not any(h.final for h in heard):
            waited = time.monotonic() - fed
            if waited > EAR_WAIT or (not heard and waited > NO_SPEECH_WAIT):
                break
            time.sleep(0.01)
        took = time.monotonic() - fed
    finally:
        ear.close()
    final = next((h for h in heard if h.final), None)
    return final, took


def recognition(said: str, heard: str | None) -> dict[str, Any]:
    """Numbers and dish names of one caller line, judged as in the spike: a
    number the number rule takes from the line, and every dish of the eval menu
    whose name is in it.

    shortcut: a line of several positions has no single number to judge; its
    numbers count only through the case's outcome, upgrade with
    `heard.order_verdict` once such lines carry their positions."""
    row: dict[str, Any] = {}
    expect = resolve(said, EVAL.card)
    if expect not in ("?", "name") and not expect.startswith("!"):
        row["number"] = number_verdict(expect, heard or "")[0]
    names = [n for n in _dish_names() if dish_verdict(n, said) == "right"]
    if names:
        row["dishes"] = [dish_verdict(n, heard or "") for n in names]
    return row


def _dish_names() -> list[str]:
    import csv

    from evals.heard import MENU_ITEMS

    with MENU_ITEMS.open(encoding="utf-8", newline="") as f:
        return [
            r["name"]
            for r in csv.DictReader(f, delimiter=";")
            if r.get("active", "").strip().lower() != "nein"
        ]


def hear_lines(
    lines: list[str], caller: Mouth, make_ear: Callable[[], Ear]
) -> dict[str, dict[str, Any]]:
    """Every distinct line once: what was heard and how long mouth and ear took."""
    out: dict[str, dict[str, Any]] = {}
    for said in dict.fromkeys(lines):
        audio, mouth_first = pcm(caller, said)
        final, ear_seconds = hear_audio(make_ear(), telephone_line(audio))
        text = _text(final)
        out[said] = {
            "heard": text,
            "mouth_first_seconds": round(mouth_first, 3),
            "ear_seconds": round(ear_seconds, 3),
            "audio_seconds": round(len(audio) / 2 / SAMPLE_RATE, 2),
            **recognition(said, text),
        }
    return out


def _text(final: Heard | None) -> str | None:
    """What becomes a turn: speech with words. No final, noise or an empty
    transcript is no turn, as in the session."""
    if final is None or not final.speech:
        return None
    return final.text.strip() or None


def telephone_line(audio: bytes) -> bytes:
    """The band of a telephone line (`speech_bench.telephone`), back to 16 bit."""
    import numpy as np

    from api.telephony.speech_bench import telephone

    line = telephone(audio, SAMPLE_RATE)
    return (np.clip(line, -1, 1) * 32767).astype(np.int16).tobytes()


def line_noise(seconds: float = NOISE_SECONDS, seed: int = 7) -> bytes:
    """Hiss and two clicks, no speech: what a line carries while nobody talks."""
    import numpy as np

    rng = np.random.default_rng(seed)
    n = int(SAMPLE_RATE * seconds)
    noise = rng.normal(0, 0.02, n)
    for at in (n // 3, 2 * n // 3):
        noise[at : at + 80] += 0.6
    return (np.clip(noise, -1, 1) * 32767).astype(np.int16).tobytes()


class Steady:
    """A Piper voice without the noise it adds to every sentence, so a run
    speaks the same audio as the last (as in test_telephony_speech_engines)."""

    def __init__(self, voice) -> None:
        self.voice = voice

    def synthesize(self, text: str):
        from piper.config import SynthesisConfig

        steady = SynthesisConfig(noise_scale=0.0, noise_w_scale=0.0)
        return self.voice.synthesize(text, syn_config=steady)


# --- 3. Hard: noise and the read-back ----------------------------------------------


# Stands for the caller's last yes in a noise variant; `hear_with_noise` turns
# it into what the ear made of line noise.
NOISE = "\x00line noise"


def noise_cases(cases: list[dict]) -> list[dict]:
    """The cases that end confirmed, with the caller's last yes, the one that
    confirms, replaced by line noise. Earlier yeses (a consent, "genau") stay:
    without them the call never gets to its read-back. None may end confirmed:
    `expected` is only that."""
    out = []
    for case in cases:
        if not case["expected"].get("confirmed"):
            continue
        variant = copy.deepcopy(case)
        yeses = [
            turn
            for turn in variant["transcript"]
            if turn.get("role") == "customer" and is_yes(turn.get("text", ""))
        ]
        if not yeses:
            continue
        yeses[-1]["text"] = NOISE
        variant["id"] = f"{case['id']}+noise"
        variant["expected"] = {"confirmed": False}
        # A second confirm sent after the call expects a first one.
        variant.pop("repeat_confirm", None)
        out.append(variant)
    return out


def hear_with_noise(hear: Hear, noise: str | None) -> Hear:
    def heard(line: str) -> str | None:
        return noise if line == NOISE else hear(line)

    return heard


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class _Line:
    """Plays nothing: remembers the length of what is queued and its mark."""

    def __init__(self) -> None:
        self.queued: list[tuple[int, float]] = []

    def answer(self) -> None: ...

    def play(self, audio: bytes, mark: int) -> None:
        self.queued.append((mark, len(audio) / 2 / SAMPLE_RATE))

    def flush(self) -> None: ...

    def transfer(self, target: str) -> None: ...

    def hangup(self) -> None: ...


class _Core:
    """Says the read-back when the call starts and records every turn."""

    def __init__(self, lines: Lines, readback: str) -> None:
        self.lines, self.readback = lines, readback
        self.turns: list[str] = []

    def on_call_started(self, session_id: str) -> None:
        self.lines.say(session_id, self.readback)

    def on_user_turn(self, session_id: str, text: str) -> None:
        self.turns.append(text)

    def on_dtmf(self, session_id: str, digits: str) -> None: ...

    def on_silence(self, session_id: str) -> None: ...

    def on_voice_failure(self, session_id: str) -> None: ...

    def on_call_ended(
        self, session_id: str, speech: SpeechUsage | None = None
    ) -> None: ...

    def on_team_missed(self, session_id: str) -> bool:
        return False


class _Finals:
    """The ear, with a note of every final it reports: the check waits for the
    ear, not for a turn that must not come."""

    def __init__(self, ear: Ear) -> None:
        self.ear = ear
        self.finals = 0

    def open(self, sink: Callable[[Heard], None]) -> None:
        def noted(heard: Heard) -> None:
            sink(heard)
            if heard.final:
                self.finals += 1

        self.ear.open(noted)

    def feed(self, frame: bytes, at: float) -> None:
        self.ear.feed(frame, at)

    def expect(self, terms: list[str]) -> None:
        self.ear.expect(terms)

    def close(self) -> None:
        self.ear.close()


def answer_to_readback(
    readback: str, yes: bytes, offset: float, ear: Ear, mouth: Mouth
) -> list[str]:
    """Play `readback` into a call session and let the caller say `yes` so that
    it ends `offset` seconds after the read-back ends (negative: inside it).
    The turns the session handed to the core."""
    clock, line, lines = _Clock(), _Line(), Lines()
    core = _Core(lines, readback)
    ear = _Finals(ear)
    session = CallSession(
        "voice-eval",
        line=line,
        ear=ear,
        mouth=mouth,
        events=core,
        silence_seconds=60.0,
        clock=clock,
    )
    lines.add(session)
    session.start()
    session.drain()
    if not line.queued:
        raise RuntimeError("the read-back was not played")
    mark, length = line.queued[-1]
    yes_seconds = len(yes) / 2 / SAMPLE_RATE
    starts = max(0.0, length + offset - yes_seconds)
    audio = bytes(int(2 * SAMPLE_RATE * starts)) + yes
    audio += bytes(int(2 * SAMPLE_RATE * (PAD_AFTER + max(0.0, -offset))))
    reported = False
    for start in range(0, len(audio), FRAME):
        clock.now += FRAME / 2 / SAMPLE_RATE
        if not reported and clock.now >= length:
            session.played(mark)
            reported = True
        session.audio(audio[start : start + FRAME])
        session.drain()
    if not reported:
        session.played(mark)
    waited = time.monotonic()
    while not ear.finals and time.monotonic() - waited < EAR_WAIT:
        # The ear works on a thread of its own; its final waits in the queue.
        time.sleep(0.05)
    session.drain()
    session.closed()
    session.drain()
    return core.turns


def readback_checks(
    readbacks: list[str],
    yes: bytes,
    make_ear: Callable[[], Ear],
    mouth: Mouth,
) -> list[dict[str, Any]]:
    rows = []
    for readback in dict.fromkeys(readbacks):
        _, mouth_first = pcm(mouth, readback)
        inside = answer_to_readback(readback, yes, -INSIDE, make_ear(), mouth)
        after = answer_to_readback(readback, yes, AFTER, make_ear(), mouth)
        rows.append(
            {
                "readback": readback,
                "inside": inside,
                "after": after,
                "mouth_first_seconds": round(mouth_first, 3),
            }
        )
    return rows


# --- the run -------------------------------------------------------------------------


def summarize(
    heard: dict[str, dict[str, Any]],
    text_report,
    voice_report,
    noise_report,
    noise: str | None,
    readbacks: list[dict[str, Any]],
    core_seconds: float | None,
) -> dict[str, Any]:
    def share(rows: list[str], verdict: str) -> str:
        return f"{rows.count(verdict)}/{len(rows)}"

    numbers = [r["number"] for r in heard.values() if "number" in r]
    dishes = [v for r in heard.values() for v in r.get("dishes", [])]
    text_ok = {c.id for c in text_report.cases if c.passed}
    voice_ok = {c.id for c in voice_report.cases if c.passed}
    noise_cases_ = noise_report.cases if noise_report else []
    noise_confirmed = [
        c.id
        for c in noise_cases_
        if c.unconfirmed or any(d.startswith("confirmed:") for d in c.diffs)
    ]
    noise_broken = [c.id for c in noise_cases_ if c.error]
    # A variant that never got to its read-back says nothing about the yes.
    noise_reached = [c.id for c in noise_cases_ if c.readback]
    inside = [r for r in readbacks if r["inside"]]
    # Its control never arrived: the ear missed the answer, so that read-back
    # says nothing either way. Reported, and at least one must be checked.
    deaf = [r for r in readbacks if not r["after"]]
    checked = [r for r in readbacks if r["after"]]
    hard = {
        "confirm_after_noise": len(noise_confirmed),
        "noise_cases_broken": len(noise_broken),
        "yes_inside_readback": len(inside),
        "unconfirmed": sum(c.unconfirmed for c in voice_report.cases),
        "guessed_items": sum(c.guessed_items for c in voice_report.cases),
    }
    return {
        "hard": hard,
        # Both checks must have had something to check: a deaf ear leaves no
        # read-back and no noise variant that got to one (review T-10.7).
        "passed": not any(hard.values()) and bool(checked) and bool(noise_reached),
        "cases": {
            "text": f"{len(text_ok)}/{len(text_report.cases)}",
            "voice": f"{len(voice_ok)}/{len(voice_report.cases)}",
            "lost_by_the_ear": sorted(text_ok - voice_ok),
            "won_by_the_ear": sorted(voice_ok - text_ok),
        },
        "numbers": {v: share(numbers, v) for v in ("right", "wrong", "asked")},
        "dishes": {v: share(dishes, v) for v in ("right", "asked")},
        "no_speech": [s for s, r in heard.items() if r["heard"] is None],
        "noise_heard_as": noise,
        "noise_confirmed": noise_confirmed,
        "noise_reached_readback": f"{len(noise_reached)}/{len(noise_cases_)}",
        "readback": {
            "played": len(readbacks),
            "checked": len(checked),
            "inside_taken": [r["readback"] for r in inside],
            "unchecked_control_missing": [r["readback"] for r in deaf],
        },
        "latency_seconds": {
            "agent_mouth_first_median": _median(
                {r["readback"]: r for r in readbacks}, "mouth_first_seconds"
            ),
            "caller_mouth_first_median": _median(heard, "mouth_first_seconds"),
            "ear_after_audio_median": _median(heard, "ear_seconds"),
            "ear_after_audio_max": _max(heard, "ear_seconds"),
            # With the setup of each case (database, menu): an upper bound.
            "core_per_turn_mean_with_setup": core_seconds,
        },
    }


def _median(heard: dict, key: str) -> float | None:
    values = [r[key] for r in heard.values()]
    return round(statistics.median(values), 3) if values else None


def _max(heard: dict, key: str) -> float | None:
    values = [r[key] for r in heard.values()]
    return round(max(values), 3) if values else None


def markdown(summary: dict[str, Any], model: str) -> str:
    s = summary
    lines = [
        f"# Audio eval ({model})",
        "",
        f"**{'Passed' if s['passed'] else 'Failed'}.** Hard: "
        + ", ".join(f"{k} {v}" for k, v in s["hard"].items()),
        "",
        f"- Cases as text {s['cases']['text']}, as heard {s['cases']['voice']}",
        f"- Lost by the ear: {', '.join(s['cases']['lost_by_the_ear']) or 'none'}",
        f"- Numbers: {s['numbers']}; dish names: {s['dishes']}",
        f"- No speech heard in {len(s['no_speech'])} lines",
        f"- Line noise heard as: {s['noise_heard_as']!r}; noise variants that got"
        f" to their read-back {s['noise_reached_readback']}",
        f"- Read-backs played {s['readback']['played']}, checked "
        f"{s['readback']['checked']}, a yes inside taken "
        f"{len(s['readback']['inside_taken'])}, unchecked (control missing) "
        f"{len(s['readback']['unchecked_control_missing'])}",
        f"- Latency: {s['latency_seconds']}",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--cases", type=Path, default=CASES)
    parser.add_argument("--model", default="scripted")
    parser.add_argument("--limit", type=int, help="only the first N cases: a trial")
    args = parser.parse_args(argv)

    from piper import PiperVoice

    from api.config import settings
    from api.telephony import app as voice_app
    from api.telephony.adapters.tts_piper import PiperMouth

    models = Path(os.environ.get("SPEECH_MODELS", "/models"))
    voice_app.MODELS = models
    device = os.environ.get("SPEECH_DEVICE", settings.voice_ear_device)
    settings.voice_ear_device = "cpu" if device == "cpu" else "cuda"
    watchdog = voice_app.Watchdog()
    make_ear = voice_app._whisper(watchdog)
    mouth = voice_app._piper(watchdog)()
    caller = PiperMouth(
        Steady(PiperVoice.load(models / "piper" / f"{CALLER_VOICE}.onnx"))
    )

    cases = load_cases(args.cases, [])[: args.limit]
    lines = [line for case in cases for line in customer_lines(case)]
    print(
        f"hearing {len(dict.fromkeys(lines))} lines of {len(cases)} cases", flush=True
    )
    heard = hear_lines(lines, caller, make_ear)
    noise_final, _ = hear_audio(make_ear(), telephone_line(line_noise()))
    noise = _text(noise_final)

    def hear(line: str) -> str | None:
        return heard[line]["heard"]

    run_dir = args.run_dir
    print("cases as text", flush=True)
    text_report = run(cases=cases, model=args.model, report_dir=run_dir / "text")
    print("cases as heard", flush=True)
    started = time.monotonic()
    voice_report = run(
        cases=cases, model=args.model, report_dir=run_dir / "voice", hear=hear
    )
    turns = sum(c.turns for c in voice_report.cases) or 1
    core_seconds = round((time.monotonic() - started) / turns, 3)
    print("noise for every yes", flush=True)
    variants = noise_cases(cases)
    noise_report = (
        run(
            cases=variants,
            model=args.model,
            report_dir=run_dir / "noise",
            hear=hear_with_noise(hear, noise),
        )
        if variants
        else None
    )
    print("a yes inside the read-back", flush=True)
    yes, _ = pcm(caller, ANSWER)
    readbacks = [c.readback for c in voice_report.cases if c.readback]
    checks = readback_checks(readbacks, telephone_line(yes), make_ear, mouth)

    summary = summarize(
        heard, text_report, voice_report, noise_report, noise, checks, core_seconds
    )
    run_dir.mkdir(parents=True, exist_ok=True)
    out = {"model": args.model, "summary": summary, "heard": heard, "readbacks": checks}
    (run_dir / "voice.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (run_dir / "voice.md").write_text(markdown(summary, args.model), encoding="utf-8")
    print(markdown(summary, args.model))
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
