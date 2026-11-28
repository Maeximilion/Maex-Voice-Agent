"""Voice latency end to end, by stage (#550, docs/22 "Measurement and release gate").

    python -m evals.latency --run-dir RUN [--calls 1,2,3] [--rounds 2] [--turns 6]
        [--sha SHA]

Runs in the image of the service `voice` (api/Dockerfile, target voice) with the
model files of the speech kit (`SPEECH_MODELS`), a database server
(`DATABASE_URL`, a scratch database is made on it and dropped) and the model of
`LLM_BASE_URL`. Synthetic only: the caller lines of a few eval cases, spoken by
a Piper voice that is not the agent's; no recording, no real call.

Every simulated call is the product's own path: a `CallSession` with the ear
and the mouth as the service loads them (`adapters/speech_engines.py`: Whisper
behind the segmenting ear, Piper), the `CallHandler` with the model, the tools
and the eval menu. The caller streams 20 ms frames on real time, line noise
between its sentences (`voice.telephone_line`), and answers once the agent's
audio has played out and the line was quiet for `GAP` seconds. The line plays
like the media channel (docs/20 §11, 14.10.2026): queued audio in order on
real time, each mark reported when its audio is through.

Per turn, the critical path from the end of the caller's speech to the first
audible frame back, as consecutive time stamps (`attribute`), so no span is
counted twice and the stages add up to the total:

    turn_end  end of speech until the ear hands the utterance to the engine
              (the silence detector waits 0.7 s by design, stt_whisper.py)
    stt_wait  until the engine takes it (one engine per process, in line)
    stt       the transcription
    handoff   until the core's turn begins (the session's queue)
    core      until the core asks for the first sentence; the model's hops and
              the tools inside it are listed apart, the rest is Python and DB
    tts       the first sentence's audio (the session queues a whole sentence)
    queue     until the line has it
    line      until its first audible frame (audio queued before it, quiet at
              its start)

Not modelled: the hop from Asterisk to the telephone (network, jitter buffer
of the caller's phone, the router). Writes `latency.json` and `latency.md`.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import platform
import statistics
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path
from typing import Any

SAMPLE_RATE = 16000
FRAME = 640  # 20 ms of 16 bit audio
FRAME_SECONDS = FRAME / 2 / SAMPLE_RATE
# A frame of the agent's audio is audible from this level on (about -40 dBFS).
AUDIBLE_RMS = 330.0
# The caller's speech ends at its last sample above this (about -30 dBFS).
SPEECH_PEAK = 1000
# The caller answers this long after the agent's audio played out.
GAP = 1.5
# A turn without an answer within this long counts as failed.
ANSWER_WAIT = 25.0
# Quiet of the line around each caller sentence.
PAD = 0.2

CHAIN = (
    "stt_req",
    "stt_start",
    "stt_end",
    "core_start",
    "tts_req",
    "tts_end",
    "play",
    "audible",
)
STAGES = ("turn_end", "stt_wait", "stt", "handoff", "core", "tts", "queue", "line")
# Calm scenarios: no clock, switches, sold-out dishes or known customers of
# their own, so every call runs on one tenant at one instant.
SCENARIOS = (
    "abholung_0001_zwei_positionen_mit_option",
    "abholung_0058_two_dishes_in_the_first_sentence_number_and_name",
    "abholung_0076_readback_total_speakable",
    "lieferung_0005_two_dishes_by_name",
    "reservierung_0001_tisch_fuer_vier",
    "reservierung_0036_eight_guests_are_booked_by_the_agent",
)
CASE_KEYS_NOT_PLAYED = ("now", "switches", "sold_out", "notices", "customers")


@dataclass
class Rec:
    """The time stamps of one call, on the monotonic clock."""

    call: str
    events: list[tuple[str, float, float, str]] = field(default_factory=list)

    def add(
        self, kind: str, t0: float, t1: float | None = None, info: str = ""
    ) -> None:
        self.events.append((kind, t0, t0 if t1 is None else t1, info))


_local = threading.local()


def _rec() -> Rec | None:
    rec: Rec | None = getattr(_local, "rec", None)
    return rec


# --- attribution (pure, tested) -------------------------------------------------------


def _overlap(intervals: Iterable[tuple[float, float]], lo: float, hi: float) -> float:
    return sum(max(0.0, min(b, hi) - max(a, lo)) for a, b in intervals)


def attribute(
    events: list[tuple[str, float, float, str]], speech_end: float, until: float
) -> dict[str, Any]:
    """One turn's critical path: each mark of `CHAIN` is the first of its kind
    at or after the one before, from `speech_end` and before `until` (the next
    sentence of the caller). The stages are the differences, so they add up to
    `total`. A missing mark ends the turn as failed at that mark."""
    marks: dict[str, float] = {}
    at = speech_end
    for kind in CHAIN:
        hit = min(
            (t0 for k, t0, _, _ in events if k == kind and at <= t0 < until),
            default=None,
        )
        if hit is None:
            return {"failed": kind}
        marks[kind] = at = hit
    points = [speech_end, *marks.values()]
    stages = {s: points[i + 1] - points[i] for i, s in enumerate(STAGES)}
    lo, hi = marks["core_start"], marks["tts_req"]
    hops = [(a, b) for k, a, b, _ in events if k == "llm" and a < hi and b > lo]
    tools = [(a, b) for k, a, b, _ in events if k == "tool" and a < hi and b > lo]
    llm = _overlap(hops, lo, hi)
    tool = _overlap(tools, lo, hi)
    cpu = next(
        (float(info) for k, a, _, info in events if k == "core_end" and a >= lo), None
    )
    first_piece = min(
        (t0 for k, t0, _, _ in events if k == "tts_first" and t0 >= hi), default=None
    )
    return {
        "total": marks["audible"] - speech_end,
        **stages,
        "core_llm": llm,
        "core_llm_hops": len(hops),
        "core_tools": tool,
        "core_tool_calls": len(tools),
        "core_rest": stages["core"] - llm - tool,
        "core_cpu": cpu,
        "tts_first_piece": None if first_piece is None else first_piece - hi,
    }


def quantiles(values: list[float]) -> dict[str, float | int | None]:
    """p50 and p95 (inclusive method, so a small sample stays inside its range)."""
    if not values:
        return {"n": 0, "p50": None, "p95": None, "max": None}
    p95 = (
        statistics.quantiles(values, n=20, method="inclusive")[18]
        if len(values) > 1
        else values[0]
    )
    return {
        "n": len(values),
        "p50": round(statistics.median(values), 3),
        "p95": round(p95, 3),
        "max": round(max(values), 3),
    }


def summarize(turns: list[dict[str, Any]]) -> dict[str, Any]:
    """Per stage p50/p95 over the turns that reached audio, and the failures."""
    done = [t for t in turns if "total" in t]
    keys = ("total", *STAGES, "core_llm", "core_tools", "core_rest", "tts_first_piece")
    out: dict[str, Any] = {
        "turns": len(turns),
        "failed": {
            kind: sum(1 for t in turns if t.get("failed") == kind)
            for kind in sorted({t["failed"] for t in turns if "failed" in t})
        },
    }
    for key in keys:
        out[key] = quantiles([t[key] for t in done if t.get(key) is not None])
    hops = [t["core_llm_hops"] for t in done]
    out["llm_hops_mean"] = round(statistics.mean(hops), 2) if hops else None
    cpu = [t["core_cpu"] for t in done if t.get("core_cpu") is not None]
    out["core_cpu"] = quantiles(cpu)
    return out


# --- the simulated line and the wrappers ----------------------------------------------


def first_audible(audio: bytes) -> float | None:
    """Seconds into `audio` of its first audible 20 ms frame."""
    import numpy as np

    samples = np.frombuffer(audio[: len(audio) - len(audio) % 2], dtype=np.int16)
    step = FRAME // 2
    for i in range(0, len(samples), step):
        frame = samples[i : i + step].astype(np.float64)
        if frame.size and math.sqrt(float(np.mean(frame * frame))) >= AUDIBLE_RMS:
            return i / SAMPLE_RATE
    return None


class SimLine:
    """`speech.Line` as the media channel plays: in order, on real time."""

    def __init__(self, rec: Rec) -> None:
        self.rec = rec
        self.session: Any = None
        self.until = 0.0
        self.ended: str | None = None
        self._timers: list[threading.Timer] = []
        self._lock = threading.Lock()

    def answer(self) -> None:
        self.rec.add("answer", time.monotonic())

    def play(self, audio: bytes, mark: int) -> None:
        now = time.monotonic()
        with self._lock:
            start = max(now, self.until)
            self.until = start + len(audio) / 2 / SAMPLE_RATE
            done = self.until
        self.rec.add("play", now)
        offset = first_audible(audio)
        if offset is not None:
            self.rec.add("audible", start + offset)
        timer = threading.Timer(done - now, self.session.played, args=(mark,))
        timer.daemon = True
        timer.start()
        self._timers.append(timer)

    def flush(self) -> None:
        for timer in self._timers:
            timer.cancel()
        with self._lock:
            self.until = time.monotonic()
        self.rec.add("flush", time.monotonic())

    def _end(self, how: str) -> None:
        self.ended = how
        self.rec.add(how, time.monotonic())
        # The router reports the leg gone once Asterisk ended it.
        timer = threading.Timer(0.1, self.session.closed)
        timer.daemon = True
        timer.start()

    def transfer(self, target: str) -> None:
        self._end("transfer")

    def hangup(self) -> None:
        self._end("hangup")

    def record_team_leg(self) -> None: ...

    def audible_after(self, at: float) -> bool:
        now = time.monotonic()
        return any(k == "audible" and at < t0 <= now for k, t0, _, _ in self.rec.events)


class TimedMouth:
    def __init__(self, mouth: Any, rec: Rec) -> None:
        self._mouth, self._rec = mouth, rec

    def speak(self, text: str) -> Iterator[bytes]:
        self._rec.add("tts_req", time.monotonic())
        first = True
        for piece in self._mouth.speak(text):
            if first:
                self._rec.add("tts_first", time.monotonic())
                first = False
            yield piece
        self._rec.add("tts_end", time.monotonic())


class TimedLLM:
    def __init__(self, inner: Any, rec: Rec | None) -> None:
        self._inner, self._rec = inner, rec

    def next_turn(self, *args: Any, **kwargs: Any) -> Any:
        started = time.monotonic()
        try:
            return self._inner.next_turn(*args, **kwargs)
        finally:
            if self._rec is not None:
                self._rec.add("llm", started, time.monotonic())

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class TimedEvents:
    """The handler, with the core's turn timed and its thread's CPU counted."""

    def __init__(self, handler: Any) -> None:
        self._handler = handler

    def on_user_turn(self, session_id: str, text: str) -> None:
        rec = _rec()
        started, cpu = time.monotonic(), time.thread_time()
        if rec is not None:
            rec.add("core_start", started, info=text)
        try:
            self._handler.on_user_turn(session_id, text)
        finally:
            if rec is not None:
                rec.add(
                    "core_end", time.monotonic(), info=f"{time.thread_time() - cpu}"
                )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._handler, name)


def timed_engine(transcribe: Callable[..., str]) -> Callable[..., str]:
    """The engine itself, inside the process's one turn of it."""

    def run(self: Any, pcm: bytes, terms: list[str]) -> str:
        started = time.monotonic()
        try:
            return transcribe(self, pcm, terms)
        finally:
            rec = _rec()
            if rec is not None:
                rec.add("stt_start", started)
                rec.add("stt_end", time.monotonic())

    return run


def timed_ear(ear: Any, rec: Rec) -> Any:
    """The ear's request to the engine, before it waits for its turn."""
    transcribe = ear._transcribe

    def requested(pcm: bytes, terms: list[str]) -> str:
        _local.rec = rec
        rec.add("stt_req", time.monotonic())
        result: str = transcribe(pcm, terms)
        return result

    ear._transcribe = requested
    return ear


def timed_dispatch(dispatch: Callable[..., Any]) -> Callable[..., Any]:
    def run(*args: Any, **kwargs: Any) -> Any:
        started = time.monotonic()
        try:
            return dispatch(*args, **kwargs)
        finally:
            rec = _rec()
            if rec is not None:
                rec.add("tool", started, time.monotonic(), str(args[3]))

    return run


# --- the caller -----------------------------------------------------------------------


@dataclass
class Utterance:
    text: str
    audio: bytes
    # Seconds into `audio` where the speech ends.
    speech_end: float


def utterance(text: str, raw: bytes) -> Utterance:
    import numpy as np

    from evals.voice import telephone_line

    samples = np.frombuffer(raw, dtype=np.int16)
    loud = np.flatnonzero(np.abs(samples.astype(np.int32)) >= SPEECH_PEAK)
    last = int(loud[-1]) if loud.size else len(samples)
    return Utterance(text, telephone_line(raw, PAD, PAD), PAD + last / SAMPLE_RATE)


class Caller:
    """Streams the caller's leg on real time and answers the agent."""

    def __init__(self, session: Any, line: SimLine, rec: Rec, noise: bytes) -> None:
        self.session, self.line, self.rec, self.noise = session, line, rec, noise
        self._next = 0.0
        self._noise_at = 0

    def _send(self, frame: bytes) -> None:
        delay = self._next - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        self.session.audio(frame)
        self._next += FRAME_SECONDS

    def _quiet_until(self, ready: Callable[[], bool], seconds: float) -> bool:
        deadline = time.monotonic() + seconds
        while not self.line.ended and time.monotonic() < deadline:
            if ready():
                return True
            start = self._noise_at % (len(self.noise) - FRAME)
            self._noise_at = start + FRAME
            self._send(self.noise[start : start + FRAME])
        return False

    def _answered(self, after: float) -> Callable[[], bool]:
        return lambda: (
            self.line.audible_after(after) and time.monotonic() > self.line.until + GAP
        )

    def run(self, lines: list[Utterance]) -> None:
        self._next = time.monotonic()
        last = -math.inf
        for said in lines:
            answered = self._quiet_until(self._answered(last), ANSWER_WAIT)
            if not answered and self.line.ended:
                break
            # The frame sent at `self._next` holds the audio that ends then.
            began = self._next - FRAME_SECONDS
            last = began + said.speech_end
            self.rec.add("speech_end", last, info=said.text)
            for start in range(0, len(said.audio) - FRAME + 1, FRAME):
                self._send(said.audio[start : start + FRAME])
        self._quiet_until(self._answered(last), ANSWER_WAIT)
        if not self.line.ended:
            self.session.closed()


# --- the run --------------------------------------------------------------------------


def turns_of(rec: Rec) -> list[dict[str, Any]]:
    ends = sorted(t0 for k, t0, _, _ in rec.events if k == "speech_end")
    texts = [
        info
        for k, _, _, info in sorted(rec.events, key=lambda e: e[1])
        if k == "speech_end"
    ]
    out = []
    for i, end in enumerate(ends):
        until = ends[i + 1] if i + 1 < len(ends) else math.inf
        turn = attribute(rec.events, end, until)
        turn.update(call=rec.call, turn=i + 1, said=texts[i])
        out.append(turn)
    return out


def sampler(stop: threading.Event, out: list[dict[str, float]]) -> None:
    """Process CPU in cores, resident memory, and the graphics card, twice a second."""
    last_cpu, last = sum(os.times()[:2]), time.monotonic()
    while not stop.wait(0.5):
        cpu, now = sum(os.times()[:2]), time.monotonic()
        row = {"t": now, "cores": (cpu - last_cpu) / (now - last)}
        last_cpu, last = cpu, now
        try:
            status = Path("/proc/self/status").read_text()
            row["rss_mb"] = int(status.split("VmRSS:")[1].split()[0]) / 1024
        except (OSError, IndexError, ValueError):
            pass
        out.append(row)


def gpu_sample() -> str:
    try:
        return subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,utilization.gpu,memory.used,memory.total,driver_version",
                "--format=csv,noheader",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def environment(sha: str) -> dict[str, Any]:
    from api.config import settings

    versions = {}
    for name in (
        "faster-whisper",
        "ctranslate2",
        "piper-tts",
        "onnxruntime",
        "torch",
        "httpx",
    ):
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            versions[name] = None
    cpu = ""
    with contextlib.suppress(OSError, StopIteration):
        cpu = next(
            line.split(":", 1)[1].strip()
            for line in Path("/proc/cpuinfo").read_text().splitlines()
            if line.startswith("model name")
        )
    return {
        "sha": sha,
        "python": platform.python_version(),
        "cpu": cpu,
        "cores": os.cpu_count(),
        "gpu": gpu_sample(),
        "versions": versions,
        "llm_model": settings.llm_model,
        "llm_reasoning_effort": settings.llm_reasoning_effort,
        "ear_device": settings.voice_ear_device,
        "mouth_voice": settings.voice_mouth_voice or "default",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--calls", default="1,2,3", help="simultaneous calls per level")
    parser.add_argument("--rounds", type=int, default=2, help="rounds per level")
    parser.add_argument("--turns", type=int, default=6, help="caller lines per call")
    parser.add_argument("--sha", default=os.environ.get("GIT_SHA", "unknown"))
    args = parser.parse_args(argv)

    from piper import PiperVoice
    from sqlalchemy import create_engine, update
    from sqlalchemy.orm import Session

    from api.agent import loop as agent_loop
    from api.agent.messages_llm import client_from_settings
    from api.config import settings
    from api.db import bounded_sessions
    from api.models import ServiceConfig
    from api.telephony import watchdog as voice_watchdog
    from api.telephony.adapters import speech_engines, stt_whisper
    from api.telephony.adapters.tts_piper import PiperMouth
    from api.telephony.app import DB_TIMEOUT_MS
    from api.telephony.handler import CallHandler
    from api.telephony.session import CallSession, Lines
    from evals.runner import CASES, DEFAULT_NOW, TENANT, _prepare, load_cases, menu_plan
    from evals.scratch_db import create_scratch_db, drop_scratch_db, migrate
    from evals.voice import ANSWER, CALLER_VOICE, Steady, pcm, telephone_line
    from sim.replay import customer_lines

    levels = [int(n) for n in args.calls.split(",")]
    models = Path(os.environ.get("SPEECH_MODELS", "/models"))
    speech_engines.MODELS = models
    settings.voice_ear_device = os.environ.get(
        "SPEECH_DEVICE", settings.voice_ear_device
    )

    cases = {c["id"]: c for c in load_cases(CASES, [])}
    scenarios = []
    for name in SCENARIOS:
        case = next(c for key, c in cases.items() if name.startswith(key))
        assert not any(k in case for k in CASE_KEYS_NOT_PLAYED), case["id"]
        # A bare "Ja" is under the ear's 256 ms of speech, no utterance at all
        # (evals/voice.py ANSWER).
        said = [
            ANSWER if s.strip(" .!").lower() == "ja" else s
            for s in customer_lines(case)
            if not s.isdecimal()
        ][: args.turns]
        scenarios.append((case["id"], said))

    loaded = time.monotonic()
    stt_whisper.Whisper.transcribe = timed_engine(stt_whisper.Whisper.transcribe)  # type: ignore[method-assign]
    agent_loop.dispatch = timed_dispatch(agent_loop.dispatch)  # type: ignore[attr-defined]
    watchdog = voice_watchdog.Watchdog()
    watchdog.start()
    make_ear = speech_engines._whisper(watchdog)
    make_mouth = speech_engines._piper(watchdog)
    caller_voice = PiperMouth(
        Steady(PiperVoice.load(models / "piper" / f"{CALLER_VOICE}.onnx"))
    )
    spoken = {
        line: utterance(line, pcm(caller_voice, line)[0])
        for _, said in scenarios
        for line in said
    }
    noise = telephone_line(b"", 30.0, 0.0)
    load_seconds = time.monotonic() - loaded
    print(
        f"engines loaded, {len(spoken)} caller lines spoken in {load_seconds:.1f} s",
        flush=True,
    )

    url = create_scratch_db(prefix="maex_latency")
    report: dict[str, Any] = {"environment": environment(args.sha), "levels": {}}
    try:
        migrate(url)
        with Session(create_engine(url)) as db:
            tenant = _prepare(db, DEFAULT_NOW, menu_plan(), name=TENANT)
            db.execute(
                update(ServiceConfig)
                .where(ServiceConfig.tenant_id == tenant.id)
                .values(call_mode="primary", max_concurrent_calls=max(levels))
            )
            db.commit()
            tenant_id = tenant.id
        lines = Lines()

        def llm(_db: Any) -> Any:
            return TimedLLM(client_from_settings(), _rec())

        handler = CallHandler(
            lines,
            tenant_id=tenant_id,
            session_factory=bounded_sessions(DB_TIMEOUT_MS, url=url),
            llm_factory=llm,
            now=DEFAULT_NOW,
        )
        events = TimedEvents(handler)
        first = True
        for level in levels:
            recs: list[Rec] = []
            samples: list[dict[str, float]] = []
            stop = threading.Event()
            watcher = threading.Thread(
                target=sampler, args=(stop, samples), daemon=True
            )
            watcher.start()
            gpu: list[str] = []
            started = time.monotonic()
            for r in range(args.rounds):
                workers = []
                for k in range(level):
                    case_id, said = scenarios[(r * level + k) % len(scenarios)]
                    rec = Rec(f"L{level}-R{r + 1}-C{k + 1} {case_id}")
                    recs.append(rec)
                    line = SimLine(rec)
                    session = CallSession(
                        f"latency-{level}-{r}-{k}",
                        line=line,
                        ear=timed_ear(make_ear(), rec),
                        mouth=TimedMouth(make_mouth(), rec),
                        events=events,
                        silence_seconds=settings.voice_silence_seconds,
                        caller_id=f"+491700000{level}{r}{k:02d}",
                    )
                    line.session = session
                    lines.add(session)

                    def work(s: Any = session, rc: Rec = rec) -> None:
                        _local.rec = rc
                        s.start()
                        s.run()

                    worker = threading.Thread(target=work, daemon=True)
                    caller = Caller(session, line, rec, noise)
                    talker = threading.Thread(
                        target=caller.run,
                        args=([spoken[s] for s in said],),
                        daemon=True,
                    )
                    workers += [worker, talker]
                    worker.start()
                    talker.start()
                    print(f"  {rec.call}", flush=True)
                time.sleep(3.0)
                gpu.append(gpu_sample())
                for w in workers:
                    w.join(timeout=600)
                for k in range(level):
                    lines.remove(f"latency-{level}-{r}-{k}")
            stop.set()
            turns = [t for rec in recs for t in turns_of(rec)]
            for t in turns:
                t["cold"] = (
                    first and t["turn"] == 1 and t["call"].startswith(f"L{level}-R1-C1")
                )
            first = False
            report["levels"][str(level)] = {
                "seconds": round(time.monotonic() - started, 1),
                "calls": len(recs),
                "all": summarize(turns),
                "first_turns": summarize([t for t in turns if t["turn"] == 1]),
                "later_turns": summarize([t for t in turns if t["turn"] > 1]),
                "cold": [t for t in turns if t["cold"]],
                "cores_mean": round(statistics.mean(s["cores"] for s in samples), 2)
                if samples
                else None,
                "cores_max": round(max(s["cores"] for s in samples), 2)
                if samples
                else None,
                "rss_mb_max": round(max(s.get("rss_mb", 0.0) for s in samples))
                if samples
                else None,
                "gpu": gpu,
                "outcomes": [
                    next(
                        (k for k, *_ in rec.events if k in ("hangup", "transfer")),
                        "caller hung up",
                    )
                    for rec in recs
                ],
                "turns": turns,
            }
            print(markdown_level(level, report["levels"][str(level)]), flush=True)
    finally:
        drop_scratch_db(url)
    args.run_dir.mkdir(parents=True, exist_ok=True)
    (args.run_dir / "latency.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    (args.run_dir / "latency.md").write_text(markdown(report), encoding="utf-8")
    print(markdown(report))
    return 0


def markdown_level(level: int, data: dict[str, Any]) -> str:
    s = data["all"]
    cells = " | ".join(
        f"{s[k]['p50']} / {s[k]['p95']}" if s[k]["n"] else "-"
        for k in ("total", *STAGES, "core_llm", "core_tools", "core_rest")
    )
    return (
        f"| {level} | {s['turns']} | {sum(s['failed'].values())} | {cells} | "
        f"{s['llm_hops_mean']} | {data['cores_mean']} / {data['cores_max']} |"
    )


def markdown(report: dict[str, Any]) -> str:
    env = report["environment"]
    head = " | ".join(("total", *STAGES, "llm", "tools", "rest"))
    rows = [
        f"Commit `{env['sha']}`, {env['cpu']} ({env['cores']} threads), {env['gpu']}, "
        f"Python {env['python']}, model `{env['llm_model']}` "
        f"(reasoning `{env['llm_reasoning_effort'] or 'default'}`), ear on {env['ear_device']}, "
        f"versions {env['versions']}.",
        "",
        "Seconds, p50 / p95, from the end of the caller's speech:",
        "",
        f"| calls | turns | failed | {head} | hops | cores mean / max |",
        "|" + "---|" * (len(STAGES) + 9),
    ]
    for level, data in report["levels"].items():
        rows.append(markdown_level(int(level), data))
    rows.append("")
    for level, data in report["levels"].items():
        first, later = data["first_turns"]["total"], data["later_turns"]["total"]
        rows.append(
            f"- {level} call(s): first turns {first['p50']} / {first['p95']} (n {first['n']}), "
            f"later turns {later['p50']} / {later['p95']} (n {later['n']}); "
            f"failed {data['all']['failed'] or 'none'}; outcomes {data['outcomes']}; "
            f"cold {[round(t.get('total', 0), 3) for t in data['cold']]}"
        )
    return "\n".join(rows) + "\n"


if __name__ == "__main__":
    sys.exit(main())
