"""Load test of one server of D3 (#841, docs/13 "Load test"): how many customer
stacks with three calls at once it carries.

    python3 -m scripts.voice_load [--max-stacks 6] [--calls 3] [--turns 4]
        [--model-delay 0.25] [--cpus 4] [--memory 8g] [--run-dir DIR]
        [--keep] [--down] [--purge] [--dry-run]

On the workbench the kit starts the test server first: one container of
deploy/loadtest/Dockerfile, an Ubuntu with systemd, journald and a Docker
Engine of its own, limited to `--cpus` processors and `--memory` without swap,
the size of the VPS of D3. Everything else runs inside it, as on the server:

1. A clean slate: the containers, volumes and networks of an earlier run go,
   the images and the build cache stay.
2. The operator stack in /srv/maex/ops (deploy/docker-compose.operator.yml) and
   the line (deploy/loadtest/docker-compose.line.yml): the router every stack's
   Asterisk registers at, with the callers and the team telephones, and the
   stand-in for the hosted model (`telephony/load_model.py`).
3. Stacks one by one, each by scripts/new_stack.py as a customer is set up
   (key `load<n>`), then for the test: the voice profile with the stand-in
   ear and mouth (`VOICE_EAR`/`VOICE_MOUTH` `load`, ENV `test` for `voice`
   only, deploy/loadtest/docker-compose.load.yml), the line, the eval menu,
   mode `primary`.
4. Steps: 1 stack with 1 call, 1 with `--calls`, then one stack more per step
   with `--calls` each, until the budget breaks, the memory runs out or
   `--max-stacks` is reached. A call speaks `--turns` times (a tone the load
   ear takes for a sentence of a pickup order) and the router records both
   directions apart.

Measured per step: the turn, from the end of the caller's speech to the first
audio back, read from the router's recordings (the stand-ins take 1.25 s of
it, `STAND_IN_SECONDS`); audio missing inside an answer (late or dropped
frames); the tool calls from each stack's call log; processor and memory of
the whole server (its cgroup, the host's own share included) and per service;
team rings, restarts and memory kills. A step is inside the budget when every
turn got its answer, the median turn is at most `--turn-budget` (docs/20 §11:
1.5 s with one tool hop), the 95th percentile of the tool calls at most 300 ms
(CLAUDE.md §7), no answer lost audio, no team telephone rang, nothing was
restarted or killed and the memory stayed below the limit.

Writes `report.json` and `table.md` into the run directory (default
deploy/loadtest/run, ignored by git) and prints the table. The workbench is
faster per core than a VPS's vCore: the result is an upper bound until the
same run on the server itself (`--inside` there, as the deploy user, from a
checkout).

`--keep` leaves the test server running, `--down` stops one left behind,
`--purge` also deletes its image cache. Invented numbers and names only, no
host port is published, every secret is made by new_stack.py inside the test
server and stays there; only the router's rendered configuration holds the
stacks' line passwords, in the run directory.

Needs Docker and Python 3.12 on the workbench, not root. Exit codes: 0 the run
finished (the table says where the limit is), 1 a step broke, 2 wrong call or
Docker not usable.
"""

import argparse
import itertools
import json
import os
import re
import secrets
import shutil
import statistics
import string
import subprocess
import sys
import threading
import time
import wave
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from api.telephony.audio import _sounding
from api.telephony.spike import docker_problem, step, usable_run_dir

REPO_ROOT = Path(__file__).resolve().parents[2]
KIT_DIR = REPO_ROOT / "deploy" / "loadtest"
RUN_MARKER = ".voice-load-run"
SERVER = "maex-loadtest-server"
SERVER_IMAGE = "maex-loadtest-server"
# The test server's Docker data: kept between runs, so images are built once.
SERVER_VOLUMES = {
    "maex-loadtest-docker": "/var/lib/docker",
    "maex-loadtest-containerd": "/var/lib/containerd",
}
ROOT = Path("/srv/maex")
# Inside the test server the run directory is here.
LOAD_DIR = Path("/load")
LINE_PROJECT = "load-line"
# Run as root in the test server before each run: it is the kit's own.
WIPE = (
    "docker ps -aq | xargs -r docker rm -f >/dev/null; "
    "docker volume prune -af >/dev/null; docker network prune -f >/dev/null; "
    "rm -rf /srv/maex/* /srv/maex/.[!.]*"
)
ROUTER = f"{LINE_PROJECT}-router-1"
MODEL = f"{LINE_PROJECT}-model-1"
DOMAIN = "load{n}.test"
# Invented numbers: stack n's AI telephone, its team, the callers of the router.
VOICE_NUMBER = "+497221557{n}00"
TEAM_PHONE = "+497221558{n}00"
MAX_STACKS = 9  # the router's extensions take one digit for the stack
# The caller's side (deploy/loadtest/router/extensions.conf).
SAID_HZ = 440
HEARD_HZ = 1000  # LoadMouth.FREQ_HZ
RECORDING_RATE = 8000
WINDOW = 0.02
# What the stand-ins take of a turn with one tool hop: the silence that ends it
# and the ear's final (LoadEar), two model requests, the mouth (LoadMouth).
EAR_SECONDS = 0.5 + 0.1
MOUTH_SECONDS = 0.15
# A gap inside an answer longer than this ends it; shorter ones are lost audio.
ANSWER_GAP = 0.3
TOOL_BUDGET_MS = 300.0
# How long the caller speaks and listens in a turn (router/extensions.conf).
SPEAK_SECONDS = 1.5
LISTEN_SECONDS = 5.0
SAMPLE_SECONDS = 1.0
CALL_SLACK_SECONDS = 40.0
REGISTER_SECONDS = 90.0
STACK_UP_SECONDS = 900

Check = tuple[str, bool, str]


class LoadError(Exception):
    """A step that broke, with the message for the user."""


# -- the workbench side -----------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m scripts.voice_load", description=__doc__
    )
    parser.add_argument("--max-stacks", type=int, default=6)
    parser.add_argument("--calls", type=int, default=3)
    parser.add_argument("--turns", type=int, default=4)
    parser.add_argument("--model-delay", type=float, default=0.25)
    parser.add_argument("--turn-budget", type=float, default=1.5)
    parser.add_argument("--greeting", type=float, default=10.0)
    parser.add_argument("--cpus", default="4")
    parser.add_argument("--memory", default="8g")
    parser.add_argument("--run-dir", type=Path, default=KIT_DIR / "run")
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--down", action="store_true")
    parser.add_argument("--purge", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--inside",
        action="store_true",
        help="run on this host as the test server (the VPS itself, as the deploy user)",
    )
    args = parser.parse_args(argv)
    if not 1 <= args.max_stacks <= MAX_STACKS:
        parser.error(f"--max-stacks: 1 to {MAX_STACKS}")
    if not 1 <= args.calls <= 99 or not 1 <= args.turns <= 20:
        parser.error("--calls: 1 to 99, --turns: 1 to 20")
    if os.getuid() == 0:
        print("voice_load: run it as an unprivileged user", file=sys.stderr)
        return 2
    problem = docker_problem()
    if problem:
        print(f"voice_load: {problem}", file=sys.stderr)
        return 2
    try:
        if args.inside:
            return Inside(args).run()
        return on_workbench(args)
    except LoadError as exc:
        print(f"voice_load: {exc}", file=sys.stderr)
        return 1


def on_workbench(args: argparse.Namespace) -> int:
    if args.down or args.purge:
        return 0 if stop_server(purge=args.purge) else 1
    run_dir = args.run_dir.resolve()
    if not usable_run_dir(run_dir, RUN_MARKER):
        print(
            f"voice_load: {run_dir} is not a run directory of this kit: name an "
            "empty or a new one",
            file=sys.stderr,
        )
        return 2
    if args.dry_run:
        print(" ".join(server_command(run_dir, args.cpus, args.memory)))
        return 0
    dirty = sh(["git", "-C", str(REPO_ROOT), "status", "--porcelain"]).strip()
    if dirty:
        print(
            "voice_load: uncommitted changes: the stacks are clones and run the "
            "last commit of this branch",
            file=sys.stderr,
        )
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / RUN_MARKER).touch()
    step("Build and start the test server", f"({args.cpus} processors, {args.memory})")
    stop_server(purge=False)
    # A client configuration of its own: the base image is public, and the
    # workbench's credential helper is a Windows program that fails whenever
    # WSL cannot start Windows programs (seen 22.11.2026).
    config = run_dir / "docker-config"
    config.mkdir(exist_ok=True)
    (config / "config.json").write_text("{}\n")
    sh(
        [
            *("docker", "--config", str(config), "build", "-q", "-t", SERVER_IMAGE),
            *("--build-arg", f"DEPLOY_UID={os.getuid()}"),
            *("--build-arg", f"DEPLOY_GID={os.getgid()}"),
            *("-f", str(KIT_DIR / "Dockerfile"), str(REPO_ROOT / "deploy")),
        ]
    )
    sh(server_command(run_dir, args.cpus, args.memory))
    wait_for(
        lambda: (
            inside_server(["systemctl", "is-active", "docker"], check=False) == "active"
        ),
        60,
        "the test server's Docker Engine did not start",
    )
    # The server's Docker data is kept between runs, and the containers of the
    # last one start again with it (restart: always), with their folders gone.
    inside_server(["sh", "-c", WIPE])
    inner = [
        *("docker", "exec", "-u", "deploy", "-w", str(REPO_ROOT)),
        *("-e", "PYTHONUNBUFFERED=1", SERVER),
        *("python3", "-m", "scripts.voice_load", "--inside"),
        *("--max-stacks", str(args.max_stacks), "--calls", str(args.calls)),
        *("--turns", str(args.turns), "--model-delay", str(args.model_delay)),
        *("--turn-budget", str(args.turn_budget), "--greeting", str(args.greeting)),
        *("--cpus", args.cpus, "--memory", args.memory),
        *("--run-dir", str(LOAD_DIR)),
    ]
    try:
        code = subprocess.run(inner, check=False).returncode
    finally:
        if not args.keep:
            stop_server(purge=False)
    return code


def server_command(run_dir: Path, cpus: str, memory: str) -> list[str]:
    """The test server: privileged for its own Docker Engine, its own cgroup
    namespace for systemd, the checkout read-only at its own path (a worktree's
    git folder too, so new_stack.py can clone it), the run directory at /load."""
    mounts = [f"{REPO_ROOT}:{REPO_ROOT}:ro", f"{run_dir}:{LOAD_DIR}"]
    common = Path(
        sh(["git", "-C", str(REPO_ROOT), "rev-parse", "--git-common-dir"]).strip()
    )
    common = (REPO_ROOT / common).resolve()
    if not common.is_relative_to(REPO_ROOT):
        mounts.append(f"{common}:{common}:ro")
    mounts += [f"{name}:{path}" for name, path in SERVER_VOLUMES.items()]
    return [
        *("docker", "run", "-d", "--name", SERVER, "--hostname", "loadtest"),
        *("--privileged", "--cgroupns=private"),
        *("--cpus", cpus, "--memory", memory, "--memory-swap", memory),
        *(item for mount in mounts for item in ("-v", mount)),
        SERVER_IMAGE,
    ]


def stop_server(*, purge: bool) -> bool:
    done = subprocess.run(
        ["docker", "rm", "-f", SERVER], capture_output=True, text=True, check=False
    )
    ok = done.returncode == 0 or "No such container" in done.stderr
    if purge:
        for name in SERVER_VOLUMES:
            ok = (
                subprocess.run(
                    ["docker", "volume", "rm", "-f", name],
                    capture_output=True,
                    check=False,
                ).returncode
                == 0
                and ok
            )
    return ok


def inside_server(command: list[str], *, check: bool = True) -> str:
    return sh(["docker", "exec", SERVER, *command], check=check).strip()


# -- the test server's side -------------------------------------------------------


@dataclass
class Stack:
    n: int
    folder: Path
    password: str = field(repr=False)

    @property
    def key(self) -> str:
        return f"load{self.n}"


@dataclass
class CallResult:
    exten: str
    turns: list[float | None]
    lost_audio: float


@dataclass
class StepResult:
    name: str
    stacks: int
    calls: int
    turns: list[float]
    missing_turns: int
    lost_audio: float
    tool_ms: list[float]
    team_rings: int
    cpu_percent_mean: float
    cpu_percent_peak: float
    memory_peak_mb: float
    memory_limit_mb: float
    services: dict[str, dict[str, float]]
    restarts: list[str]
    checks: list[Check] = field(default_factory=list)

    @property
    def inside(self) -> bool:
        return all(ok for _, ok, _ in self.checks)


class Inside:
    """The run on the test server itself."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.load: Path = args.run_dir
        self.stacks: list[Stack] = []
        self.results: list[StepResult] = []

    def run(self) -> int:
        self.load.mkdir(parents=True, exist_ok=True)
        step("Clear what an earlier run left on the test server")
        clean_slate(self.load)
        step("Start the operator stack and the line")
        operator_stack()
        self.render_router()
        compose_line("up", "-d", "--wait", env=self.line_env())
        for name, stacks, calls in plan(self.args.max_stacks, self.args.calls):
            while len(self.stacks) < stacks:
                n = len(self.stacks) + 1
                step(f"Set up stack load{n} with scripts/new_stack.py")
                try:
                    self.add_stack(n)
                except LoadError as exc:
                    if not oom_kills():
                        raise
                    # Running out of memory while a stack starts is a result.
                    print(f"voice_load: stack load{n} ran out of memory: {exc}")
                    self.finish(f"stack load{n} ran out of memory while it started")
                    return 0
            step(f"Step {name}: {stacks} stack(s), {calls} call(s) each")
            result = self.step(name, self.stacks[:stacks], calls)
            self.results.append(result)
            print(table([result]))
            if not result.inside:
                self.finish(None)
                return 0
        self.finish(None)
        return 0

    def line_env(self) -> dict[str, str]:
        return {
            **os.environ,
            "LOAD_RUN": str(self.load),
            "LOAD_SRC": str(REPO_ROOT),
            "LOAD_UID": str(os.getuid()),
            "LOAD_GID": str(os.getgid()),
            "LOAD_MODEL_DELAY": str(self.args.model_delay),
        }

    def render_router(self) -> None:
        # Written over in place: the running router has the folder mounted, and
        # a new folder would stay unseen behind the deleted one.
        target = self.load / "router"
        target.mkdir(parents=True, exist_ok=True)
        for name in ("asterisk.conf", "logger.conf", "modules.conf"):
            shutil.copy(REPO_ROOT / "deploy" / "asterisk" / "conf" / name, target)
        source = KIT_DIR / "router"
        pjsip = (source / "pjsip.conf").read_text()
        (target / "pjsip.conf").touch(mode=0o600)
        (target / "pjsip.conf").write_text(router_pjsip(pjsip, self.stacks))
        a = self.args
        (target / "extensions.conf").write_text(
            router_dialplan(
                (source / "extensions.conf").read_text(),
                turns=a.turns,
                speak=SPEAK_SECONDS,
                listen=LISTEN_SECONDS,
                greeting=a.greeting,
            )
        )
        (self.load / "recordings").mkdir(exist_ok=True)

    def add_stack(self, n: int) -> None:
        key = f"load{n}"
        customers = self.load / "customers"
        customers.mkdir(exist_ok=True)
        spec = customers / f"{key}.json"
        spec.write_text(json.dumps(customer(n), indent=2))
        log = self.load / f"new_stack-{key}.log"
        with log.open("w") as out:
            # stdout carries the setup link and the push passwords: not kept.
            done = subprocess.run(
                [
                    *("python3", str(REPO_ROOT / "scripts" / "new_stack.py")),
                    *(str(spec), "--domain", DOMAIN.format(n=n), "--root", str(ROOT)),
                ],
                stdout=subprocess.DEVNULL,
                stderr=out,
                check=False,
                timeout=STACK_UP_SECONDS,
            )
        if done.returncode != 0:
            raise LoadError(f"new_stack.py failed, see {log.name}")
        folder = ROOT / key
        env_path = folder / ".env"
        env = read_env(env_path.read_text())
        set_env(
            env_path,
            {
                "COMPOSE_FILE": env["COMPOSE_FILE"]
                + ":deploy/loadtest/docker-compose.load.yml",
                "COMPOSE_PROFILES": "voice",
                "VOICE_EAR": "load",
                "VOICE_MOUTH": "load",
                "VOICE_NUMBER": VOICE_NUMBER.format(n=n),
                "ACCESS_HOST": "router",
                "ACCESS_USER": f"ai{n}",
                "LLM_BASE_URL": "http://model:8080/v1",
                "LLM_MODEL": "load",
            },
        )
        stack = Stack(n, folder, env["ACCESS_PASSWORD"])
        in_stack(
            stack,
            *("compose", "run", "--rm", "-T", "-v", f"{REPO_ROOT}/evals/menu:/menu:ro"),
            *("api", "python", "-m", "scripts.import_menu", "/menu"),
        )
        sql(stack, "update service_config set call_mode = 'primary'")
        # The telephone's containers and their network, not started: Asterisk
        # looks the router up once, when it starts (deploy/asterisk/render.sh).
        in_stack(
            stack, "compose", "up", "--no-start", "--build", "voice", "asterisk",
            timeout=STACK_UP_SECONDS,
        )  # fmt: skip
        network = f"maex-{key}_telephony"
        for container, alias in ((ROUTER, "router"), (MODEL, "model")):
            sh(["docker", "network", "connect", "--alias", alias, network, container])
        self.stacks.append(stack)
        self.render_router()
        # `pjsip reload` is no command in Asterisk 22 (and exits 0).
        router_cli("module reload res_pjsip.so")
        router_cli("dialplan reload")
        in_stack(
            stack, "compose", "up", "-d", "--wait", "voice", "asterisk",
            timeout=STACK_UP_SECONDS,
        )  # fmt: skip
        wait_for(
            lambda: (
                f"ai{n}/" in router_cli("pjsip show contacts")
                and "Avail" in router_cli(f"pjsip show aor ai{n}")
            ),
            REGISTER_SECONDS,
            f"the AI telephone of {key} did not register at the router",
        )

    def step(self, name: str, stacks: list[Stack], calls: int) -> StepResult:
        a = self.args
        folder = self.load / "recordings" / name
        folder.mkdir(parents=True, exist_ok=True)
        router_cli(f"dialplan set global STEP {name}")
        started = datetime.now(UTC)
        since = time.time()
        sampler = Sampler()
        sampler.start()
        extens = [f"{s.n}{c:02d}" for s in stacks for c in range(1, calls + 1)]
        period = SPEAK_SECONDS + LISTEN_SECONDS
        for i, exten in enumerate(extens):
            # Spread over one turn, as calls of different callers are.
            time.sleep(period / len(extens) if i else 0)
            router_cli(
                f"channel originate Local/{exten}@dialout extension {exten}@caller"
            )
        limit = a.greeting + a.turns * period + CALL_SLACK_SECONDS
        wait_for(
            lambda: all(
                f"LOAD {e} caller leg ended" in self.router_log(since) for e in extens
            ),
            limit,
            "calls still running at the end of the step",
            fail=False,
        )
        router_cli("channel request hangup all")
        usage = sampler.stop()
        log = self.router_log(since)
        results = [judge_call(exten, folder, a.turns) for exten in extens]
        turns = [t for r in results for t in r.turns if t is not None]
        missing = sum(1 for r in results for t in r.turns if t is None)
        tool_ms = [ms for s in stacks for ms in tool_durations(s, started)]
        result = StepResult(
            name=name,
            stacks=len(stacks),
            calls=len(extens),
            turns=turns,
            missing_turns=missing,
            lost_audio=round(sum(r.lost_audio for r in results), 2),
            tool_ms=tool_ms,
            team_rings=log.count("team rings at"),
            restarts=restarts(),
            **usage,
        )
        result.checks = judge_step(result, a.turn_budget)
        return result

    def router_log(self, since: float) -> str:
        return sh(["docker", "logs", "--since", str(int(since) - 1), ROUTER], both=True)

    def finish(self, stopped: str | None) -> None:
        limit = limit_line(self.results, stopped)
        report = {
            "when": datetime.now(UTC).isoformat(timespec="seconds"),
            "server": {"cpus": self.args.cpus, "memory": self.args.memory},
            "stand_in_seconds": stand_in_seconds(self.args.model_delay),
            "stopped": stopped,
            "steps": [summary(r) for r in self.results],
            "limit": limit,
        }
        (self.load / "report.json").write_text(json.dumps(report, indent=2) + "\n")
        text = table(self.results) + "\n\n" + limit + "\n"
        (self.load / "table.md").write_text(text)
        step("Result")
        print(text)


def plan(max_stacks: int, calls: int) -> list[tuple[str, int, int]]:
    """Name, stacks and calls per stack of each step."""
    steps = [("s1-c1", 1, 1)]
    if calls > 1:
        steps.append((f"s1-c{calls}", 1, calls))
    steps += [(f"s{n}-c{calls}", n, calls) for n in range(2, max_stacks + 1)]
    return steps


def customer(n: int) -> dict[str, Any]:
    """A customer file (deploy/customer.example.json) open all day, every day."""
    days = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    return {
        "key": f"load{n}",
        "name": f"Lasttest {n}",
        "timezone": "Europe/Berlin",
        "holiday_region": "BW",
        "privacy_notice_address": "example.com/datenschutz",
        "team_phone": TEAM_PHONE.format(n=n),
        "hours": [
            {
                "days": days,
                "services": ["dinein", "pickup", "delivery"],
                "opens": "00:00",
                "closes": "23:59",
            }
        ],
        "seats": [{"days": days, "start": "00:00", "end": "23:59", "guests": 40}],
    }


def router_pjsip(header: str, stacks: list[Stack]) -> str:
    blocks = [header.rstrip("\n")]
    for s in stacks:
        password = s.password.replace(";", r"\;")
        blocks.append(
            f"\n[ai{s.n}]\ntype=endpoint\ncontext=from-ai\ndisallow=all\nallow=alaw\n"
            f"auth=ai{s.n}\naors=ai{s.n}\ndirect_media=no\ndtmf_mode=rfc4733\n"
            f"\n[ai{s.n}]\ntype=auth\nauth_type=userpass\nusername=ai{s.n}\n"
            f"password={password}\n"
            f"\n[ai{s.n}]\ntype=aor\nmax_contacts=1\nremove_existing=yes\n"
            "qualify_frequency=10"
        )
    return "\n".join(blocks) + "\n"


def router_dialplan(
    text: str, *, turns: int, speak: float, listen: float, greeting: float
) -> str:
    for mark, value in (
        ("@TURNS@", turns),
        ("@SPEAK@", speak),
        ("@LISTEN@", listen),
        ("@GREETING@", greeting),
    ):
        text = text.replace(mark, str(value))
    return text


def stand_in_seconds(model_delay: float) -> float:
    return round(EAR_SECONDS + 2 * model_delay + MOUTH_SECONDS, 3)


# -- reading a call back ----------------------------------------------------------


def spans(windows: list[int], rate: int, gap: float) -> list[tuple[float, float]]:
    """The window starts of `_sounding`, in samples, as (start, end) seconds of
    each stretch, stretches apart by more than `gap` counted as two."""
    step_s = WINDOW
    out: list[tuple[float, float]] = []
    for w in windows:
        t = w / rate
        if out and t - out[-1][1] <= gap:
            out[-1] = (out[-1][0], t + step_s)
        else:
            out.append((t, t + step_s))
    return out


def turn_times(
    said: list[tuple[float, float]],
    heard: list[tuple[float, float]],
    turns: int,
    listen: float = LISTEN_SECONDS,
) -> list[float | None]:
    """For each of the caller's last `turns` stretches: seconds from its end to
    the first answer that starts after it, before the caller speaks again and
    within `listen`; None when none came."""
    out: list[float | None] = []
    last = said[-turns:] if turns else []
    for i, (_, end) in enumerate(last):
        later = min(last[i + 1][0] if i + 1 < len(last) else float("inf"), end + listen)
        answer = next((s for s, _ in heard if end <= s < later), None)
        out.append(None if answer is None else round(answer - end, 3))
    while len(out) < turns:
        out.insert(0, None)
    return out


def lost_seconds(windows: list[int], rate: int) -> float:
    """Audio missing inside the answers: the gaps up to `ANSWER_GAP` between
    sounding windows, which a voice played without a hitch does not have."""
    lost = 0.0
    for before, after in itertools.pairwise(windows):
        gap = (after - before) / rate - WINDOW
        if WINDOW / 2 < gap <= ANSWER_GAP:
            lost += gap
    return round(lost, 3)


def read_pcm(path: Path) -> tuple[bytes, int]:
    with wave.open(str(path), "rb") as w:
        return w.readframes(w.getnframes()), w.getframerate()


def judge_call(exten: str, folder: Path, turns: int) -> CallResult:
    try:
        said_pcm, said_rate = read_pcm(folder / f"{exten}-said.wav")
        heard_pcm, heard_rate = read_pcm(folder / f"{exten}-heard.wav")
    except (OSError, wave.Error, EOFError):
        return CallResult(exten, [None] * turns, 0.0)
    said = spans(_sounding(said_pcm, SAID_HZ, said_rate), said_rate, ANSWER_GAP)
    heard_windows = _sounding(heard_pcm, HEARD_HZ, heard_rate)
    heard = spans(heard_windows, heard_rate, ANSWER_GAP)
    return CallResult(
        exten, turn_times(said, heard, turns), lost_seconds(heard_windows, heard_rate)
    )


# -- what the server and the stacks report ----------------------------------------


class Sampler(threading.Thread):
    """Processor and memory of the whole test server (its cgroup) and of every
    container, once a second, from the cgroup files: `docker stats` would cost
    more than what it measures."""

    def __init__(self, root: Path = Path("/sys/fs/cgroup")) -> None:
        super().__init__(daemon=True)
        self.root = root
        self.stopped = threading.Event()
        self.containers = containers()
        self.cpu: list[float] = []
        self.memory: list[int] = []
        # The peak memory of each container, by its id.
        self.service_memory: dict[str, int] = {}
        self._start_usec: dict[str, int] = {}

    def scope(self, cid: str) -> Path:
        return self.root / "system.slice" / f"docker-{cid}.scope"

    def run(self) -> None:
        self._start_usec = {c: usec(self.scope(c)) for c in self.containers}
        self.began = time.monotonic()
        last_t, last_usec = self.began, usec(self.root)
        while not self.stopped.wait(SAMPLE_SECONDS):
            now, used = time.monotonic(), usec(self.root)
            self.cpu.append(100 * (used - last_usec) / 1e6 / (now - last_t))
            last_t, last_usec = now, used
            self.memory.append(memory(self.root))
            for cid in self.containers:
                self.service_memory[cid] = max(
                    self.service_memory.get(cid, 0), memory(self.scope(cid))
                )

    def stop(self) -> dict[str, Any]:
        self.stopped.set()
        self.join()
        seconds = max(time.monotonic() - self.began, 1e-9)
        services: dict[str, dict[str, float]] = {}
        for cid, name in self.containers.items():
            service = service_of(name)
            used = usec(self.scope(cid)) - self._start_usec.get(cid, 0)
            entry = services.setdefault(service, {"cpu_percent": 0.0, "memory_mb": 0.0})
            entry["cpu_percent"] += 100 * used / 1e6 / seconds
            # Each container's peak in the step, summed per service.
            entry["memory_mb"] += self.service_memory.get(cid, 0) / 2**20
        limit = read_int(self.root / "memory.max")
        return {
            "cpu_percent_mean": round(statistics.fmean(self.cpu), 1)
            if self.cpu
            else 0.0,
            "cpu_percent_peak": round(max(self.cpu, default=0.0), 1),
            "memory_peak_mb": round(max(self.memory, default=0) / 2**20),
            "memory_limit_mb": round(limit / 2**20) if limit else 0.0,
            "services": {
                k: {m: round(v, 1) for m, v in e.items()}
                for k, e in sorted(services.items())
            },
        }


def usec(cgroup: Path) -> int:
    try:
        for line in (cgroup / "cpu.stat").read_text().splitlines():
            if line.startswith("usage_usec "):
                return int(line.split()[1])
    except OSError:
        pass
    return 0


def memory(cgroup: Path) -> int:
    return read_int(cgroup / "memory.current")


def read_int(path: Path) -> int:
    try:
        text = path.read_text().strip()
    except OSError:
        return 0
    return int(text) if text.isdigit() else 0


def oom_kills(root: Path = Path("/sys/fs/cgroup")) -> int:
    """Processes the kernel killed for memory inside the test server."""
    try:
        events = (root / "memory.events").read_text().split()
    except OSError:
        return 0
    pairs = dict(zip(events[::2], events[1::2], strict=False))
    return int(pairs.get("oom_kill", "0"))


def containers() -> dict[str, str]:
    listed = sh(["docker", "ps", "--no-trunc", "--format", "{{.ID}} {{.Names}}"])
    return dict(line.split(" ", 1) for line in listed.splitlines() if " " in line)


def service_of(name: str) -> str:
    """maex-load3-voice-1 is `voice`, maex-ops-dashboard-1 `ops dashboard`,
    load-line-router-1 `line router`, maex-caddy-caddy-1 `caddy`."""
    match = re.fullmatch(r"(maex-load\d+|maex-ops|maex-caddy|load-line)-(.+)-\d+", name)
    if not match:
        return name
    project, service = match.groups()
    prefix = {"maex-ops": "ops ", "load-line": "line "}.get(project, "")
    return "caddy" if project == "maex-caddy" else prefix + service


def tool_durations(stack: Stack, since: datetime) -> list[float]:
    rows = sql(
        stack,
        "select t->>'duration_ms' from calls c, jsonb_array_elements(c.tool_calls) t "
        f"where c.started_at >= '{since.isoformat()}'",
    )
    return [float(x) for x in rows.split() if re.fullmatch(r"[0-9.]+", x)]


def restarts() -> list[str]:
    """Containers restarted or killed for memory since they were made."""
    ids = sh(["docker", "ps", "-aq"]).split()
    if not ids:
        return []
    shown = sh(
        [
            *("docker", "inspect", "--format"),
            "{{.Name}} {{.RestartCount}} {{.State.OOMKilled}} {{.State.Status}}",
            *ids,
        ]
    )
    out = []
    for line in shown.splitlines():
        name, count, killed, status = line.split()
        if count != "0" or killed == "true" or status in {"restarting", "dead"}:
            out.append(f"{name.lstrip('/')} restarts={count} oom={killed} {status}")
    return out


# -- judging ----------------------------------------------------------------------


def percentile(values: list[float], share: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(share * (len(ordered) - 1)))]


def judge_step(r: StepResult, turn_budget: float) -> list[Check]:
    median = statistics.median(r.turns) if r.turns else None
    tool_p95 = percentile(r.tool_ms, 0.95)
    memory_left = r.memory_limit_mb - r.memory_peak_mb
    return [
        (
            "every turn answered",
            r.missing_turns == 0,
            f"{r.missing_turns} without answer",
        ),
        (
            "turn time",
            median is not None and median <= turn_budget,
            f"median {fmt(median)} s, budget {turn_budget} s",
        ),
        (
            "tool calls",
            tool_p95 is None or tool_p95 <= TOOL_BUDGET_MS,
            f"95th percentile {fmt(tool_p95)} ms of {len(r.tool_ms)}",
        ),
        ("audio steady", r.lost_audio == 0, f"{r.lost_audio} s missing in answers"),
        ("no team ring", r.team_rings == 0, f"{r.team_rings} rings"),
        ("nothing restarted", not r.restarts, "; ".join(r.restarts) or "none"),
        (
            "memory",
            not r.memory_limit_mb or memory_left > 0.05 * r.memory_limit_mb,
            f"peak {r.memory_peak_mb:.0f} of {r.memory_limit_mb:.0f} MB",
        ),
    ]


def fmt(value: float | None) -> str:
    return "none" if value is None else f"{value:.2f}" if value < 10 else f"{value:.0f}"


def summary(r: StepResult) -> dict[str, Any]:
    data = asdict(r)
    data["turn_median"] = statistics.median(r.turns) if r.turns else None
    data["turn_p95"] = percentile(r.turns, 0.95)
    data["tool_p95_ms"] = percentile(r.tool_ms, 0.95)
    data["inside_budget"] = r.inside
    data["checks"] = [{"name": n, "ok": ok, "detail": d} for n, ok, d in r.checks]
    return data


def table(results: list[StepResult]) -> str:
    head = (
        "| Step | Stacks x calls | Turn median / p95 (s) | Tool p95 (ms) "
        "| CPU mean / peak (% of 1 core) | RAM peak (MB) | Inside budget |\n"
        "|---|---|---|---|---|---|---|"
    )
    rows = []
    for r in results:
        failed = ", ".join(n for n, ok, _ in r.checks if not ok)
        rows.append(
            f"| {r.name} | {r.stacks} x {r.calls // max(r.stacks, 1)} "
            f"| {fmt(statistics.median(r.turns) if r.turns else None)} / "
            f"{fmt(percentile(r.turns, 0.95))} | {fmt(percentile(r.tool_ms, 0.95))} "
            f"| {r.cpu_percent_mean:.0f} / {r.cpu_percent_peak:.0f} "
            f"| {r.memory_peak_mb:.0f} of {r.memory_limit_mb:.0f} "
            f"| {'yes' if r.inside else 'no: ' + failed} |"
        )
    return "\n".join([head, *rows])


def limit_line(results: list[StepResult], stopped: str | None) -> str:
    good = [r for r in results if r.inside]
    best = max((r.stacks for r in good), default=0)
    failed = next((r for r in results if not r.inside), None)
    if failed:
        first = next(n for n, ok, _ in failed.checks if not ok)
        hit = f"first limit hit at {failed.name}: {first}"
    elif stopped:
        hit = f"stopped: {stopped}"
    else:
        hit = "no limit hit up to the last step"
    return f"Largest number of stacks with all calls inside the budget: {best}; {hit}."


# -- small helpers ----------------------------------------------------------------


def sh(
    command: list[str], *, check: bool = True, both: bool = False, timeout: float = 600
) -> str:
    done = subprocess.run(
        command,
        capture_output=True,
        text=True,
        errors="replace",
        check=False,
        timeout=timeout,
    )
    if check and done.returncode != 0:
        raise LoadError(f"`{' '.join(command)}` failed:\n{done.stderr.strip()[-2000:]}")
    return done.stdout + (done.stderr if both else "")


def in_stack(stack: Stack, *args: str, timeout: float = 600) -> str:
    """`docker <args>` in the stack's folder, where its .env names the project."""
    done = subprocess.run(
        ["docker", *args],
        cwd=stack.folder,
        capture_output=True,
        text=True,
        errors="replace",
        check=False,
        timeout=timeout,
    )
    if done.returncode != 0:
        raise LoadError(
            f"`docker {' '.join(args)}` in {stack.folder} failed:\n"
            f"{done.stderr.strip()[-2000:]}"
        )
    return done.stdout


def sql(stack: Stack, statement: str) -> str:
    env = read_env((stack.folder / ".env").read_text())
    return in_stack(
        stack,
        *("compose", "exec", "-T", "db", "psql", "-U", env["POSTGRES_USER"]),
        *("-d", env["POSTGRES_DB"], "-tAc", statement),
    ).strip()


def router_cli(command: str) -> str:
    return sh(["docker", "exec", ROUTER, "asterisk", "-rx", command])


def compose_line(*args: str, env: dict[str, str]) -> None:
    done = subprocess.run(
        [
            *("docker", "compose", "-p", LINE_PROJECT),
            *("-f", str(KIT_DIR / "docker-compose.line.yml"), *args),
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if done.returncode != 0:
        raise LoadError(f"the line did not start:\n{done.stderr.strip()[-2000:]}")


def read_env(text: str) -> dict[str, str]:
    """Active lines of a .env as new_stack.py writes them."""
    values = {}
    for line in text.splitlines():
        match = re.match(r"^([A-Z0-9_]+)=(.*)$", line)
        if match:
            name, raw = match.groups()
            if len(raw) > 1 and raw[0] == raw[-1] and raw[0] in "\"'":
                raw = raw[1:-1].replace("$$", "$")
            values[name] = raw
    return values


def set_env(path: Path, values: dict[str, str]) -> None:
    """Sets each value on its active line, else appends it; the file keeps
    its mode (600)."""
    lines = path.read_text().splitlines()
    for name, value in values.items():
        line = f'{name}="{value}"'
        hit = next((i for i, x in enumerate(lines) if x.startswith(f"{name}=")), None)
        if hit is None:
            lines.append(line)
        else:
            lines[hit] = line
    path.write_text("\n".join(lines) + "\n")


def wait_for(ready: Any, seconds: float, message: str, *, fail: bool = True) -> bool:
    end = time.monotonic() + seconds
    while True:
        try:
            if ready():
                return True
        except LoadError:
            pass
        if time.monotonic() > end:
            if fail:
                raise LoadError(message)
            print(f"voice_load: {message}")
            return False
        time.sleep(1)


def clean_slate(load_dir: Path) -> None:
    """What an earlier run of this kit left: its stacks, the operator stack,
    the shared Caddy and the line, with their volumes and folders. A host whose
    /srv/maex this kit did not make is refused: a server with customers on it
    is no place for a load test."""
    marker = ROOT / RUN_MARKER
    if any(ROOT.iterdir()) and not marker.exists():
        raise LoadError(
            f"{ROOT} holds stacks this kit did not make: run the load test on an "
            "empty server only"
        )
    listed = sh(
        [
            "docker",
            "ps",
            "-a",
            "--format",
            '{{.ID}} {{.Label "com.docker.compose.project"}}',
        ]
    )
    ours = [
        cid
        for cid, _, project in (line.partition(" ") for line in listed.splitlines())
        if re.fullmatch(r"maex-load\d+|maex-ops|maex-caddy|" + LINE_PROJECT, project)
    ]
    if ours:
        sh(["docker", "rm", "-f", *ours])
    stacks = [f"maex-load{n}" for n in range(1, MAX_STACKS + 1)]
    for project in ("maex-ops", "maex-caddy", LINE_PROJECT, *stacks):
        label = f"label=com.docker.compose.project={project}"
        for kind in ("volume", "network"):
            sh(["docker", kind, "prune", "-f", *(["-a"] if kind == "volume" else []),
                "--filter", label])  # fmt: skip
    for path in ROOT.iterdir():
        if path != marker:
            sh(["rm", "-rf", str(path)])
    marker.touch()
    for name in ("recordings", "customers", "router"):
        if (load_dir / name).exists():
            shutil.rmtree(load_dir / name)
    for name in ("report.json", "table.md"):
        (load_dir / name).unlink(missing_ok=True)


def operator_stack() -> None:
    """The operator stack as docs/13 §8 sets it up, with fresh secrets."""
    ops = ROOT / "ops"
    sh(["git", "clone", "--quiet", str(REPO_ROOT), str(ops)])
    owner, app, proxy = (secrets.token_hex(32) for _ in range(3))
    alphabet = string.ascii_lowercase + string.digits
    push = "".join(secrets.choice(alphabet) for _ in range(29))
    env = ops / ".env"
    env.touch(mode=0o600)
    env.write_text(
        "\n".join(
            [
                "COMPOSE_FILE=deploy/docker-compose.operator.yml:networks.json",
                "POSTGRES_USER=maex_owner",
                f"POSTGRES_PASSWORD={owner}",
                "POSTGRES_DB=maex_operator",
                f"DATABASE_URL=postgresql+psycopg://maex_ops:{app}@db:5432/maex_operator",
                "OWNER_DATABASE_URL="
                f"postgresql+psycopg://maex_owner:{owner}@db:5432/maex_operator",
                "BACKUP_DOCKER_NETWORK=maex-ops_backend",
                f"OPERATOR_PROXY_SECRET={proxy}",
                "OPERATOR_ORIGIN=https://ops.load.test",
                "NTFY_BASE_URL=https://push.ops.load.test",
                # ntfy's token form, tk_ and 29 characters. Without users the
                # push server accepts nobody, which a load test does not need.
                f"OPERATOR_PUSH_TOKEN=tk_{push}",
            ]
        )
        + "\n"
    )
    (ops / "networks.json").write_text("{}\n")
    for command in (
        ["docker", "compose", "up", "-d", "--wait", "db"],
        ["bash", "scripts/db_role.sh"],
        ["bash", "scripts/migrate.sh", "--operator"],
        ["docker", "compose", "up", "-d", "--build", "--wait"],
    ):
        done = subprocess.run(
            command, cwd=ops, capture_output=True, text=True, check=False, timeout=900
        )
        if done.returncode != 0:
            raise LoadError(
                f"operator stack: `{' '.join(command)}` failed:\n"
                f"{done.stderr.strip()[-2000:]}"
            )
