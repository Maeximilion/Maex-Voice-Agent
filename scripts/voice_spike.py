"""Kit of the spike T-10.1, step 1: call audio over the media channel, on the workbench.

    python3 -m scripts.voice_spike [--dry-run] [--keep] [--run-dir DIR]

    --dry-run   check the environment and print every step, change nothing
    --keep      leave the three containers running afterwards (stop them with
                `docker compose -p voice-spike down`)
    --run-dir   where rendered configs, recordings and logs go
                (default deploy/asterisk/run, ignored by git)

Starts a test PBX, a scripted softphone and the probe (docs/20 §10) as containers
on their own network, places the test calls and judges them from the probe's logs
(`api/telephony/probe.py`) and the softphone's recordings: audio in both
directions, the caller number, key events, playback that is paced, stopped by a
key and finished before a hangup, three calls at once. Prints one line per point
and writes `report.json` into the run directory.

Needs Docker and Python 3.12, nothing else on the host; no sudo. Invented numbers
and test tones only, no host port is published. The softphone's password is made
new on every run and lives in the run directory only.

Exit codes: 0 every point passed, 1 a point failed or a step broke, 2 wrong call
or Docker not usable.
"""

import argparse
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
import wave
from pathlib import Path
from typing import Any

from api.telephony.audio import dominant, tone, tone_span
from api.telephony.probe import CALLER_TONES, PLANS, STOP_KEY

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = REPO_ROOT / "deploy" / "asterisk"
COMPOSE_FILE = CONFIG_DIR / "docker-compose.spike.yml"
PASSWORD_MARK = "@SOFTPHONE_PASSWORD@"
# The softphone's line is a telephone line: 8 kHz.
LINE_RATE = 8000
CALL_TIMEOUT_SECONDS = 45
# Tolerances of the judgement, all in seconds.
PACING_TOLERANCE = 0.3
STOP_LIMIT = 0.5
HANGUP_LIMIT = 10.0

Check = tuple[str, bool, str]


class SpikeError(Exception):
    """A step that broke, with the message for the user."""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m scripts.voice_spike", description=__doc__
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--run-dir", type=Path, default=CONFIG_DIR / "run")
    args = parser.parse_args(argv)
    run_dir = args.run_dir.resolve()

    problem = docker_problem()
    if problem:
        print(f"voice_spike: {problem}", file=sys.stderr)
        return 2
    stack = Stack(run_dir, dry_run=args.dry_run)
    try:
        step("Render configs and test tones into", run_dir)
        if not args.dry_run:
            render(run_dir)
        step("Start the test PBX, the softphone and the probe")
        stack.compose("up", "-d", "--build", "--wait")
        checks: list[Check] = []
        for title, extens, judge in SCENARIOS:
            step(f"Call: {title} ({', '.join(extens)})")
            if args.dry_run:
                continue
            checks += judge(place(stack, extens))
    except SpikeError as exc:
        print(f"voice_spike: {exc}", file=sys.stderr)
        return 1
    finally:
        if not args.dry_run:
            (run_dir / "pbx.log").write_text(
                stack.compose("logs", "--no-color", "pbx", "softphone")
            )
        if not args.keep:
            step("Stop the containers")
            stack.compose("down", "--volumes")
    if args.dry_run:
        return 0
    return report(checks, run_dir)


def step(*words: object) -> None:
    print("\n==", *words)


def docker_problem() -> str | None:
    if shutil.which("docker") is None:
        return "docker is not installed"
    for command in (["docker", "info"], ["docker", "compose", "version"]):
        if subprocess.run(command, capture_output=True, check=False).returncode != 0:
            return f"`{' '.join(command)}` fails: is Docker running?"
    return None


class Stack:
    """The Compose project of the spike."""

    def __init__(self, run_dir: Path, *, dry_run: bool) -> None:
        self.run_dir = run_dir
        self.dry_run = dry_run
        self._env = {
            **os.environ,
            "SPIKE_RUN": str(run_dir),
            "SPIKE_UID": str(os.getuid()),
            "SPIKE_GID": str(os.getgid()),
        }

    def compose(self, *args: str) -> str:
        command = ["docker", "compose", "-f", str(COMPOSE_FILE), *args]
        if self.dry_run:
            print("   [dry-run]", " ".join(command))
            return ""
        done = subprocess.run(
            command, env=self._env, capture_output=True, text=True, check=False
        )
        if done.returncode != 0:
            raise SpikeError(
                f"`{' '.join(command)}` failed:\n{done.stderr.strip()[-2000:]}"
            )
        return done.stdout

    def softphone(self, cli_command: str) -> str:
        return self.compose("exec", "-T", "softphone", "asterisk", "-rx", cli_command)


def render(run_dir: Path) -> None:
    """Configs with a fresh password, the caller's tones, empty result folders."""
    password = secrets.token_hex(16)
    for name in ("pbx", "softphone"):
        target = run_dir / name
        shutil.rmtree(target, ignore_errors=True)
        target.mkdir(parents=True)
        for source in sorted((CONFIG_DIR / name).iterdir()):
            rendered = target / source.name
            rendered.touch(mode=0o600)
            rendered.write_text(source.read_text().replace(PASSWORD_MARK, password))
    for name in ("tones", "rec", "probe"):
        shutil.rmtree(run_dir / name, ignore_errors=True)
        (run_dir / name).mkdir(parents=True)
    for exten, freq in CALLER_TONES.items():
        with wave.open(str(run_dir / "tones" / f"caller-{exten}.wav"), "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(LINE_RATE)
            out.writeframes(tone(freq, 3, LINE_RATE))


def place(stack: Stack, extens: tuple[str, ...]) -> dict[str, dict[str, Any]]:
    """Place one call per extension at once, wait for all to end, collect per
    extension: the probe's log, what the caller heard, how long the call took."""
    for folder in ("rec", "probe"):
        for old in (stack.run_dir / folder).iterdir():
            old.unlink()
    started = time.monotonic()
    for exten in extens:
        stack.softphone(
            f"channel originate Local/{exten}@dialout extension {exten}@script"
        )
    while True:
        time.sleep(0.5)
        logs = {e: read_log(stack.run_dir / "probe", e) for e in extens}
        on_the_line = not stack.softphone("core show channels count").startswith(
            "0 active channels"
        )
        if not on_the_line and all(
            log and log[-1].get("closed") for log in logs.values()
        ):
            break
        if time.monotonic() - started > CALL_TIMEOUT_SECONDS:
            raise SpikeError(f"calls to {', '.join(extens)} did not end; see pbx.log")
    seconds = time.monotonic() - started
    # Saved so a later scenario does not delete what this one produced.
    keep = stack.run_dir / "calls" / "-".join(extens)
    shutil.rmtree(keep, ignore_errors=True)
    shutil.copytree(stack.run_dir / "probe", keep)
    return {
        e: {
            "log": logs[e],
            "heard": read_wav(stack.run_dir / "rec" / f"{e}-heard.wav"),
            "seconds": seconds,
        }
        for e in extens
    }


def read_log(folder: Path, exten: str) -> list[dict[str, Any]]:
    files = sorted(folder.glob(f"{exten}-*.jsonl"))
    if len(files) != 1:
        return []
    return [
        json.loads(line) for line in files[0].read_text(encoding="utf-8").splitlines()
    ]


def read_wav(path: Path) -> bytes:
    if not path.exists():
        return b""
    with wave.open(str(path)) as recording:
        return recording.readframes(recording.getnframes())


def events(
    log: list[dict[str, Any]], direction: str, key: str, name: str
) -> list[dict[str, Any]]:
    """The control frames named `name` in a call log, with the probe's time `t`."""
    found = []
    for record in log:
        if record["dir"] == direction and "text" in record:
            frame = json.loads(record["text"])
            if frame.get(key) == name:
                found.append({**frame, "t": record["t"]})
    return found


def sent_at(log: list[dict[str, Any]]) -> float:
    return next(r["t"] for r in log if r["dir"] == "out" and "bytes" in r)


def judge_two_way(calls: dict[str, dict[str, Any]]) -> list[Check]:
    checks: list[Check] = []
    probe_tones = [plan.tone_hz for plan in PLANS.values()]
    for exten, call in calls.items():
        log, plan, label = (
            call["log"],
            PLANS[exten],
            f"{exten}" + (" (parallel)" if len(calls) > 1 else ""),
        )
        start = (events(log, "in", "event", "MEDIA_START") or [{}])[0]
        summary = log[-1]
        number = f"+4972215550{exten}"
        heard = dominant(call["heard"], probe_tones, LINE_RATE)
        span = tone_span(call["heard"], plan.tone_hz, LINE_RATE)
        drained = events(log, "in", "event", "QUEUE_DRAINED")
        spoken = drained[0]["t"] - sent_at(log) if drained else None
        checks += [
            (
                f"{label}: media channel connects",
                start.get("format") == "slin16"
                and start.get("optimal_frame_size") == 640,
                f"format {start.get('format')}, frame {start.get('optimal_frame_size')} bytes, "
                f"{summary['frame_interval_ms']} ms between frames",
            ),
            (
                f"{label}: caller number arrives",
                log[0]["caller"] == number,
                str(log[0]["caller"]),
            ),
            (
                f"{label}: audio to the caller",
                heard == plan.tone_hz and span is not None,
                f"caller heard {heard} Hz for {span[1] - span[0]:.2f} s"
                if span
                else f"heard {heard}",
            ),
            (
                f"{label}: audio from the caller",
                summary["heard_hz"] == CALLER_TONES[exten]
                and summary["audio_in_seconds"] >= 2.5,
                f"probe heard {summary['heard_hz']} Hz in {summary['audio_in_seconds']} s of audio",
            ),
            (
                f"{label}: key event",
                summary["digits"] == exten[-1],
                f"digits {summary['digits']!r}",
            ),
            (
                f"{label}: playback is paced",
                spoken is not None and abs(spoken - plan.seconds) <= PACING_TOLERANCE,
                f"{plan.seconds} s of audio sent at once, queue empty after {spoken} s",
            ),
        ]
    if len(calls) > 1:
        begins = [call["log"][0]["wall"] for call in calls.values()]
        ends = [
            call["log"][0]["wall"] + call["log"][-1]["t"] for call in calls.values()
        ]
        together = min(ends) - max(begins)
        checks.append(
            (
                f"{len(calls)} calls at once",
                together > 3,
                f"all on the line together for {together:.1f} s",
            )
        )
    return checks


def judge_stop(calls: dict[str, dict[str, Any]]) -> list[Check]:
    (call,) = calls.values()
    log, plan = call["log"], PLANS["610"]
    flush = events(log, "out", "command", "FLUSH_MEDIA")
    status = events(log, "in", "event", "STATUS")
    span = tone_span(call["heard"], plan.tone_hz, LINE_RATE)
    if not flush or span is None:
        return [
            (
                "610: a key stops the playback",
                False,
                f"flush sent: {bool(flush)}, tone heard: {span}",
            )
        ]
    # How much longer the caller heard the tone than we let it play.
    overrun = (span[1] - span[0]) - (flush[0]["t"] - sent_at(log))
    return [
        (
            "610: a key stops the playback",
            log[-1]["digits"] == STOP_KEY and abs(overrun) <= STOP_LIMIT,
            f"{plan.seconds} s queued, caller heard {span[1] - span[0]:.2f} s, "
            f"stopped {overrun * 1000:.0f} ms after the flush",
        ),
        (
            "610: the queue is empty after the flush",
            bool(status) and status[0].get("queue_length") == 0,
            f"queue_length {status[0].get('queue_length') if status else 'no STATUS'}",
        ),
    ]


def judge_hangup(calls: dict[str, dict[str, Any]]) -> list[Check]:
    (call,) = calls.values()
    log, plan = call["log"], PLANS["611"]
    drained = events(log, "in", "event", "QUEUE_DRAINED")
    hangup = events(log, "out", "command", "HANGUP")
    span = tone_span(call["heard"], plan.tone_hz, LINE_RATE)
    heard = span[1] - span[0] if span else 0.0
    return [
        (
            "611: we hang up after everything was spoken",
            bool(drained and hangup)
            and heard >= plan.seconds - 0.05
            and call["seconds"] < HANGUP_LIMIT,
            f"caller heard {heard:.2f} of {plan.seconds} s, call over after {call['seconds']:.1f} s "
            "(the caller would have waited 20 s)",
        )
    ]


SCENARIOS = (
    ("one call, both directions", ("601",), judge_two_way),
    ("three calls at once", ("601", "602", "603"), judge_two_way),
    ("a key stops the playback", ("610",), judge_stop),
    ("we hang up", ("611",), judge_hangup),
)


def report(checks: list[Check], run_dir: Path) -> int:
    step("Result")
    for name, passed, detail in checks:
        print(f"   {'pass' if passed else 'FAIL'}  {name}: {detail}")
    failed = [name for name, passed, _ in checks if not passed]
    (run_dir / "report.json").write_text(
        json.dumps(
            [{"point": n, "passed": p, "detail": d} for n, p, d in checks], indent=2
        )
        + "\n"
    )
    print(
        f"\n{len(checks) - len(failed)} of {len(checks)} points passed. Report: {run_dir / 'report.json'}"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
