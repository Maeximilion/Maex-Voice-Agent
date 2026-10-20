"""Outage drills of the AI telephone on the workbench (T-1.11, docs/20 §5 and §10).

    python3 -m scripts.voice_drill [--dry-run] [--keep] [--down] [--run-dir DIR]

    --dry-run   check the environment and print every step, change nothing
    --keep      leave the containers running afterwards
    --down      only stop the containers a run with --keep left behind
    --run-dir   where the router's rendered configuration and the report go
                (default deploy/asterisk/drill-run, ignored by git)

Starts the AI telephone as the product runs it, the service `asterisk` of
docker-compose.yml with its configuration in deploy/asterisk/conf/, and the
service `voice` with the drill engines (an ear that hears nothing, a mouth of
noise), a model address that does not exist and a throwaway database. In front
of it a stand-in for the router (deploy/asterisk/drill/): the registrar the AI
telephone registers at, the caller who rings it, and the team telephone. Then
one call per drill:

  silent caller   the AI answers, asks twice into the silence, says goodbye and
                  hangs up; the team does not ring
  own number      a call from the AI telephone's own number is refused; nobody
                  answers and the team does not ring
  keys            the caller's keys reach the core as a turn, the model cannot
                  be reached, and the core hands the call over: the team rings,
                  or, outside the opening hours, a callback card and goodbye
  paused          the core sends the call to the team before a word: the team
                  rings, and the AI never answered the call
  voice crashes   `voice` is killed in the middle of a call: the team rings
  voice down      `voice` is stopped: the team rings
  voice frozen    `voice` is paused, it takes the connection and answers
                  nothing: the team rings
  frozen mid-call `voice` is paused after it answered: the team rings
  database down   the database is stopped: the team rings
  off the list    `voice` hands the call to a number that is not on the
                  dialplan's list: the dialplan says so, the team rings at
                  TEAM_PHONE, the number is never dialled
  listed, busy    `voice` hands the call to another number of the list, which
                  is busy: the team rings at TEAM_PHONE after it
  stranger        an INVITE from an address that is not the router, From user
                  `access` (the endpoint's name), is refused by the ACL (403)
  team missed     the team telephone is busy after the core handed the call
                  over: a callback card under the caller's number, and the
                  caller hears that the restaurant calls back (T-10.8)
  missed, paused  the same in mode paused: a card, and nothing is played to a
                  caller who never heard the AI
  missed, down    the team telephone rings unanswered and `voice` is stopped
                  meanwhile: no card, the caller hears to call again, from the
                  file `voice` rendered at its start
  sentences       the files Asterisk plays hold the sentences of the code

A team ring is timed on this machine's monotonic clock from the call being
placed, or from the kill, against the limit of docs/20 §5 (proposal: 30 s).
Prints one line per point and writes `report.json` into the run directory.

Needs Docker and Python 3.12, no sudo, and not root: the containers take the
caller's uid. Invented numbers only, no host port. Token and password are made
new on every run and live in the run directory and the containers only.

Exit codes: 0 every point passed, 1 a point failed or a step broke, 2 wrong
call or Docker not usable.
"""

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from api.telephony.spike import docker_problem, step, usable_run_dir

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "deploy" / "asterisk"
COMPOSE_FILE = CONFIG_DIR / "docker-compose.drill.yml"
PASSWORD_MARK = "@ACCESS_PASSWORD@"
RUN_MARKER = ".voice-drill-run"
# Invented: the router's caller n rings from +4972215550<n>.
NUMBER_PREFIX = "+4972215550"
VOICE_NUMBER = NUMBER_PREFIX + "100"
TEAM_PHONE = NUMBER_PREFIX + "900"
# On the AI telephone's list of transfer targets (TRANSFER_TARGETS); the
# router's telephone there is busy. And a number on no list.
LISTED_BUSY_NUMBER = NUMBER_PREFIX + "901"
UNLISTED_NUMBER = NUMBER_PREFIX + "999"
# docs/20 §5: each row of the outage map within the limit Maxi sets (proposal).
TEAM_LIMIT_SECONDS = 30.0
# What a call may take at most before the drill gives up on it.
CALL_TIMEOUT_SECONDS = 60.0
POLL_SECONDS = 0.25

Check = tuple[str, bool, str]


class DrillError(Exception):
    """A step that broke, with the message for the user."""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m scripts.voice_drill", description=__doc__
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--down", action="store_true")
    parser.add_argument("--run-dir", type=Path, default=CONFIG_DIR / "drill-run")
    args = parser.parse_args(argv)
    run_dir = args.run_dir.resolve()

    if os.getuid() == 0:
        print(
            "voice_drill: run it as an unprivileged user. The containers take the "
            "caller's uid, as root they would run as root",
            file=sys.stderr,
        )
        return 2
    if not usable_run_dir(run_dir, RUN_MARKER):
        print(
            f"voice_drill: {run_dir} is not a run directory of this kit (it holds "
            f"files and no {RUN_MARKER}): name an empty or a new one",
            file=sys.stderr,
        )
        return 2
    problem = docker_problem()
    if problem:
        print(f"voice_drill: {problem}", file=sys.stderr)
        return 2
    drill = Drill(run_dir, dry_run=args.dry_run)
    if args.down:
        return 0 if stop(drill) else 1
    checks: list[Check] = []
    tidied = True
    try:
        step("Render the router's configuration into", run_dir)
        drill.compose("down", "--volumes")
        if not args.dry_run:
            render(run_dir, drill.secrets)
        step("Start the AI telephone, the service voice and the router")
        drill.compose("up", "-d", "--build", "--wait")
        if not args.dry_run:
            drill.registered()
            drill.mode("primary")
        for title, scenario in SCENARIOS:
            step("Drill:", title)
            if not args.dry_run:
                checks += scenario(drill)
        if not args.dry_run:
            checks.append(drill.no_secret_in_the_logs())
            checks.append(drill.no_caller_in_the_logs())
    except DrillError as exc:
        print(f"voice_drill: {exc}", file=sys.stderr)
        checks.append(("the drills ran to the end", False, str(exc)))
    finally:
        tidied = save_output(drill)
        if not args.keep:
            tidied = stop(drill) and tidied
    result = 0 if args.dry_run else report(checks, run_dir)
    return result if tidied else 1


class Drill:
    """The Compose project of the drills, and what a drill does with it."""

    def __init__(self, run_dir: Path, *, dry_run: bool) -> None:
        self.run_dir = run_dir
        self.dry_run = dry_run
        self.project = (
            "voice-drill-" + hashlib.sha256(str(run_dir).encode()).hexdigest()[:8]
        )
        self.secrets = {
            "VOICE_MEDIA_TOKEN": secrets.token_hex(16),
            # A ";" as a password of a provider may hold one (render.sh).
            "ACCESS_PASSWORD": secrets.token_hex(8) + ";" + secrets.token_hex(8),
        }
        self._env = {
            **os.environ,
            **self.secrets,
            "DRILL_RUN": str(run_dir),
            "DRILL_UID": str(os.getuid()),
            "DRILL_GID": str(os.getgid()),
            "VOICE_NUMBER": VOICE_NUMBER,
            "TEAM_PHONE": TEAM_PHONE,
            # Written loosely, with TEAM_PHONE in its national form: render.sh
            # cleans it, and the busy drill fails if the clean list does not
            # reach the dialplan.
            "TRANSFER_TARGETS": f" {LISTED_BUSY_NUMBER}, ,0{TEAM_PHONE[3:]},",
            "ACCESS_HOST": "router",
            "ACCESS_USER": "ai",
        }

    def compose(self, *args: str) -> str:
        command = [
            "docker",
            "compose",
            "-p",
            self.project,
            "-f",
            str(COMPOSE_FILE),
            "--profile",
            "voice",
            *args,
        ]
        if self.dry_run:
            print("   [dry-run]", " ".join(command))
            return ""
        done = subprocess.run(
            command,
            env=self._env,
            capture_output=True,
            text=True,
            errors="replace",
            check=False,
        )
        if done.returncode != 0:
            raise DrillError(
                f"`{' '.join(command)}` failed:\n{done.stderr.strip()[-2000:]}"
            )
        return done.stdout

    def cli(self, service: str, command: str) -> str:
        return self.compose("exec", "-T", service, "asterisk", "-rx", command)

    def sql(self, statement: str) -> str:
        return self.compose(
            "exec", "-T", "db", "psql", "-U", "maex", "-d", "maex", "-tAc", statement
        ).strip()

    def mode(self, mode: str) -> None:
        self.sql(f"update service_config set call_mode = '{mode}'")

    def team_phone(self, number: str) -> None:
        self.sql(f"update service_config set team_phone = '{number}'")

    def log(self, service: str) -> list[str]:
        return self.compose("logs", "--no-color", service).splitlines()

    def registered(self) -> None:
        """The AI telephone is registered at the router, or the drills would
        measure nothing."""
        end = time.monotonic() + 30
        while "Registered" not in self.cli("asterisk", "pjsip show registrations"):
            if time.monotonic() > end:
                raise DrillError("the AI telephone did not register at the router")
            time.sleep(1)

    def place(self, n: int) -> "Call":
        call = Call(self, n)
        self.cli("router", f"channel originate Local/{n}@dialout extension {n}@caller")
        return call

    def restart(self, service: str) -> None:
        self.compose("up", "-d", "--wait", service)

    def quiet(self) -> None:
        """Wait until no call is left on the router, so the next drill starts
        alone."""
        end = time.monotonic() + CALL_TIMEOUT_SECONDS
        while not self.cli("router", "core show channels count").startswith(
            "0 active channels"
        ):
            if time.monotonic() > end:
                raise DrillError("a call of the last drill does not end")
            time.sleep(POLL_SECONDS)

    def no_secret_in_the_logs(self) -> Check:
        """Asterisk prints every dialplan step when it runs verbose; the media
        token would be in that output (docs/20 §2)."""
        found = [
            service
            for service in ("asterisk", "voice", "router")
            for line in self.log(service)
            if any(value in line for value in self.secrets.values())
        ]
        return (
            "token and password are in no container's output",
            not found,
            "found in " + ", ".join(sorted(set(found))) if found else "none found",
        )

    def no_caller_in_the_logs(self) -> Check:
        """A caller's number belongs in the call log, which has a deletion
        period, not in the output of `voice` or `asterisk` (docs/17); nor does
        the team's, which can be somebody's own mobile. Every drill's caller,
        also the one whose call found `voice` down: Asterisk logs the dial
        string then, which therefore carries no number (Codex on PR #297)."""
        numbers = [
            form
            for number in [
                *(f"{NUMBER_PREFIX}{n}" for n in range(101, 114)),
                TEAM_PHONE,
                LISTED_BUSY_NUMBER,
                UNLISTED_NUMBER,
            ]
            for form in (number, "%2B" + number[1:])
        ]
        found = sorted(
            {
                service
                for service in ("asterisk", "voice")
                for line in self.log(service)
                if any(number in line for number in numbers)
            }
        )
        return (
            "no caller's or team number in the output of voice or asterisk",
            not found,
            "found in " + ", ".join(found) if found else "none found",
        )


class Call:
    """One call of the router's caller n, and the lines it left."""

    def __init__(self, drill: Drill, n: int) -> None:
        self.drill = drill
        self.n = n
        self.caller = f"{NUMBER_PREFIX}{n}"
        # Only lines after this point belong to this call.
        self._seen = {s: len(drill.log(s)) for s in ("router", "asterisk", "voice")}
        self.placed = time.monotonic()

    def lines(self, service: str) -> list[str]:
        return self.drill.log(service)[self._seen[service] :]

    def wait(
        self, *patterns: str, timeout: float = CALL_TIMEOUT_SECONDS
    ) -> tuple[str, float]:
        """The first of `patterns` that shows in the router's log for this call,
        and when it was seen."""
        end = time.monotonic() + timeout
        while True:
            for line in self.lines("router"):
                for pattern in patterns:
                    if re.search(pattern, line):
                        return pattern, time.monotonic()
            if time.monotonic() > end:
                raise DrillError(
                    f"call {self.n}: none of {patterns} after {timeout:.0f} s"
                )
            time.sleep(POLL_SECONDS)

    @property
    def team_rings(self) -> str:
        return self.rings_at(TEAM_PHONE)

    @staticmethod
    def rings_at(number: str) -> str:
        # Not by the caller's number: the team leg carries the user of our
        # registration (`from_user`). One call at a time, and only lines after
        # this one was placed count.
        return rf"DRILL team rings at {re.escape(number)}\b"

    @property
    def answered(self) -> str:
        return rf"DRILL {self.n} answered"

    @property
    def ended(self) -> str:
        return rf"DRILL {self.n} caller leg ended dialstatus=(\w*)"

    def router_says(self, pattern: str) -> bool:
        return any(re.search(pattern, line) for line in self.lines("router"))

    def ending(self) -> str:
        """What the AI telephone logged as this call's ending."""
        for line in self.lines("asterisk"):
            found = re.search(r"voice leg (\w*) ending \[([^\]]*)\]", line)
            if found:
                return f"{found.group(1)} [{found.group(2)}]"
        return "none logged"

    def dialstatus(self) -> str:
        for line in self.lines("router"):
            found = re.search(self.ended, line)
            if found:
                return found.group(1)
        return ""


def team_check(name: str, call: Call, since: float, extra: str = "") -> Check:
    """The team rang for `call` within the limit, counted from `since`."""
    _, seen = call.wait(call.team_rings, timeout=TEAM_LIMIT_SECONDS + 10)
    seconds = seen - since
    ok = seconds <= TEAM_LIMIT_SECONDS
    return (name, ok, f"the team rang after {seconds:.1f} s{extra}")


def drill_silent(drill: Drill) -> list[Check]:
    call = drill.place(101)
    call.wait(call.ended)
    seconds = time.monotonic() - call.placed
    drill.quiet()
    return [
        (
            "silent caller: the AI answers, asks, says goodbye and hangs up",
            call.dialstatus() == "ANSWER"
            and call.ending().endswith("[hangup]")
            and not call.router_says(call.team_rings),
            f"call over after {seconds:.1f} s, ending {call.ending()}, "
            f"team rang: {call.router_says(call.team_rings)}",
        )
    ]


def drill_own_number(drill: Drill) -> list[Check]:
    """100 rings from VOICE_NUMBER as E.164, 200 from it in the national form."""
    checks: list[Check] = []
    for n, form in ((100, "E.164"), (200, "national form")):
        call = drill.place(n)
        call.wait(call.ended, timeout=15)
        drill.quiet()
        refused = any(
            "own number and refused" in line for line in call.lines("asterisk")
        )
        checks.append(
            (
                f"own number ({form}): refused, nobody answers, the team does not ring",
                refused
                and call.dialstatus() != "ANSWER"
                and not call.router_says(call.team_rings),
                f"refused: {refused}, caller's dial status {call.dialstatus()}",
            )
        )
    return checks


def drill_keys(drill: Drill) -> list[Check]:
    call = drill.place(102)
    first, seen = call.wait(call.team_rings, call.ended)
    turned = any("model failed" in line for line in call.lines("voice"))
    if first == call.team_rings:
        handed = f"the team rang after {seen - call.placed:.1f} s"
        ok = turned
    else:
        outcome = drill.sql(
            "select outcome from calls order by started_at desc limit 1"
        )
        handed = f"no team ring, call outcome {outcome!r}"
        ok = turned and outcome == "callback" and call.ending().endswith("[hangup]")
    drill.quiet()
    return [
        (
            "keys: a turn of the core, the model is down, the call is handed over",
            ok,
            f"turn reached the model: {turned}, {handed}",
        )
    ]


def handed_over(drill: Drill, n: int, name: str, number: str) -> tuple[Call, Check]:
    """Call n in mode paused, the core hands it over before a word, while the
    database names `number` as the team's (#252 findings 1, 3). Mode and
    number are set back whatever fails, the number last."""
    try:
        drill.team_phone(number)
        drill.mode("paused")
        call = drill.place(n)
        check = team_check(name, call, call.placed)
    finally:
        try:
            drill.mode("primary")
        finally:
            drill.team_phone(TEAM_PHONE)
    drill.quiet()
    return call, check


def drill_paused(drill: Drill) -> list[Check]:
    call, check = handed_over(drill, 103, "paused: the team rings", TEAM_PHONE)
    # Answered only when the team picked up: the AI never did.
    return [
        check,
        (
            "paused: the AI never answered the call",
            call.ending().startswith("NOANSWER") and "[transfer]" in call.ending(),
            f"ending {call.ending()}",
        ),
    ]


def drill_unlisted(drill: Drill) -> list[Check]:
    call, check = handed_over(
        drill, 109, "off the list: the team rings at TEAM_PHONE", UNLISTED_NUMBER
    )
    said = any("not on the list" in line for line in call.lines("asterisk"))
    dialled = call.router_says(call.rings_at(UNLISTED_NUMBER))
    return [
        check,
        (
            "off the list: the dialplan refuses the number, it is never dialled",
            said and "[transfer]" in call.ending() and not dialled,
            f"refused: {said}, ending {call.ending()}, dialled: {dialled}",
        ),
    ]


def drill_listed_busy(drill: Drill) -> list[Check]:
    call, check = handed_over(
        drill, 110, "listed, busy: the team rings at TEAM_PHONE", LISTED_BUSY_NUMBER
    )
    lines = call.lines("router")
    busy, team = (
        next((i for i, line in enumerate(lines) if re.search(p, line)), None)
        for p in (call.rings_at(LISTED_BUSY_NUMBER), call.team_rings)
    )
    return [
        check,
        (
            "listed, busy: tried first, then the team at TEAM_PHONE",
            busy is not None and team is not None and busy < team,
            f"router log lines: listed number {busy}, TEAM_PHONE {team}",
        ),
    ]


# A special day open around the clock: the core hands a call to the team only
# inside the opening hours (Maxi, 20.10.2026), and a drill must not depend on
# the hour it runs at.
OPEN_ALL_DAY = (
    "insert into special_days (tenant_id, date, closed, opens_at, closes_at, note) "
    "select id, (now() at time zone timezone)::date, false, '00:00', '00:00', "
    "'drill' from tenants"
)


@contextmanager
def team_away(drill: Drill, how: str) -> Iterator[None]:
    """The router's team telephone does not answer for one drill: "busy" at
    once, or "ring" for longer than the AI telephone waits. Set back whatever
    fails, as are the hours."""
    drill.sql(OPEN_ALL_DAY)
    try:
        drill.cli("router", f"dialplan set global TEAM_AWAY {how}")
        yield
    finally:
        try:
            drill.cli("router", "dialplan set global TEAM_AWAY answer")
        finally:
            drill.sql("delete from special_days where note = 'drill'")


def missed_of(call: Call) -> tuple[str, str, str, str]:
    """The call's id, what `voice` answered the report, and what the caller
    was told, from the AI telephone's log; and the call's row as
    "outcome|cards|phone"."""
    sid = card = told = ""
    for line in call.lines("asterisk"):
        found = re.search(r"call ([0-9.]+): nobody at the team, card \[(\w*)\]", line)
        if found:
            sid, card = found.groups()
        found = re.search(r"told the caller (missed_\w+) (\w+)", line)
        if found:
            told = " ".join(found.groups())
    row = (
        call.drill.sql(
            "select c.outcome || '|' || count(b.id) || '|' || coalesce(max(b.phone), '') "
            "from calls c left join callbacks b on b.call_id = c.id "
            f"and b.deleted_at is null where c.external_session_id = '{sid}' "
            "group by c.outcome"
        )
        if sid
        else ""
    )
    return sid, card, told, row


def drill_team_missed(drill: Drill) -> list[Check]:
    """111 presses keys, the core hands the call to the team, whose telephone
    is busy: within a fraction of a second, while our leg's log may still be
    written."""
    with team_away(drill, "busy"):
        call = drill.place(111)
        check = team_check("team missed: the team rings", call, call.placed)
        call.wait(call.ended, timeout=CALL_TIMEOUT_SECONDS)
    drill.quiet()
    _, card, told, row = missed_of(call)
    return [
        check,
        (
            "team missed: a callback card under the caller's number",
            card == "noted" and row == f"callback|1|{call.caller}",
            f"voice answered [{card}], call row {row or 'not found'}",
        ),
        (
            "team missed: the caller hears that the restaurant calls back",
            told == "missed_noted SUCCESS",
            f"told: {told or 'nothing'}",
        ),
    ]


def drill_team_missed_paused(drill: Drill) -> list[Check]:
    """112 in mode paused: the AI never answered and never said who it is, so
    the card is written and nothing is played."""
    try:
        drill.mode("paused")
        with team_away(drill, "busy"):
            call = drill.place(112)
            call.wait(call.ended, timeout=CALL_TIMEOUT_SECONDS)
    finally:
        drill.mode("primary")
    drill.quiet()
    _, card, told, row = missed_of(call)
    return [
        (
            "team missed, paused: a card, nothing played, the call never answered",
            card == "noted"
            and row == f"callback|1|{call.caller}"
            and not told
            and call.dialstatus() != "ANSWER",
            f"voice answered [{card}], call row {row or 'not found'}, "
            f"told: {told or 'nothing'}, caller's dial status {call.dialstatus()}",
        )
    ]


def drill_team_missed_voice_down(drill: Drill) -> list[Check]:
    """113: the team telephone rings unanswered, and `voice` is stopped
    meanwhile. No card can be written; the caller hears the sentence from the
    file anyway."""
    try:
        with team_away(drill, "ring"):
            call = drill.place(113)
            call.wait(call.team_rings, timeout=TEAM_LIMIT_SECONDS + 10)
            drill.compose("stop", "voice")
            _, ended = call.wait(call.ended, timeout=CALL_TIMEOUT_SECONDS + 30)
    finally:
        drill.restart("voice")
    drill.quiet()
    _, card, told, row = missed_of(call)
    return [
        (
            "team missed, voice down: no card, the caller hears to call again",
            card == ""
            and told == "missed_later SUCCESS"
            and (not row or row.split("|")[1] == "0"),
            f"voice answered [{card}], told: {told or 'nothing'}, "
            f"call row {row or 'not found'}, call over {ended - call.placed:.1f} s "
            "after it was placed",
        )
    ]


def drill_announcements(drill: Drill) -> list[Check]:
    """What Asterisk plays is what the code says now: rendered by the drill's
    mouth, whose audio is the sentence's text."""
    expected = json.loads(
        drill.compose(
            "exec",
            "-T",
            "voice",
            "python",
            "-c",
            "import json; from api.telephony.handler import ANNOUNCEMENTS; "
            "print(json.dumps(ANNOUNCEMENTS))",
        )
    )
    differ = [
        name
        for name, sentence in expected.items()
        if drill.compose(
            "exec",
            "-T",
            "asterisk",
            "head",
            "-c",
            str(len(sentence.encode())),
            f"/announcements/{name}.sln16",
        )
        != sentence
    ]
    return [
        (
            "the sentences Asterisk plays are the code's",
            bool(expected) and not differ,
            f"different: {', '.join(differ)}" if differ else f"{len(expected)} match",
        )
    ]


def drill_stranger(drill: Drill) -> list[Check]:
    """Sent from the container of `voice`, an address on the network that is
    not the router's (#252 finding 2)."""
    answers = drill.compose(
        "exec",
        "-T",
        "voice",
        "python",
        "-c",
        "from api.telephony.drill import stranger_invite; print(*stranger_invite())",
    )
    codes = [int(code) for code in answers.split()]
    # Whatever got through ends here, so the next drill starts alone.
    drill.cli("asterisk", "channel request hangup all")
    return [
        (
            "stranger: an INVITE from another address as `access` is refused by the ACL",
            refused(codes),
            f"answers: {' '.join(map(str, codes)) or 'none'}",
        )
    ]


def refused(codes: list[int]) -> bool:
    """403, the ACL's answer, and nothing before it but Trying. A 401 is no
    pass: it is what identification by address alone answers, with the ACL
    gone or too wide (measured on 22.11.0, ultrareview of PR #340)."""
    return codes[-1:] == [403] and all(c == 100 for c in codes[:-1])


def stranger_invite(
    host: str = "asterisk", port: int = 5060, timeout: float = 4.0
) -> list[int]:
    """One INVITE as the access sends it, From user `access`: the name of the
    endpoint, which Asterisk's identification by user took from anywhere. The
    status codes of the answers, up to the first final one or `timeout`. Runs
    inside the drill's `voice` container; standard library only."""
    address = socket.gethostbyname(host)
    tag = secrets.token_hex(8)
    codes: list[int] = []
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.connect((address, port))
        me, my_port = sock.getsockname()
        sdp = (
            f"v=0\r\no=- 1 1 IN IP4 {me}\r\ns=-\r\nc=IN IP4 {me}\r\nt=0 0\r\n"
            "m=audio 40000 RTP/AVP 8\r\na=rtpmap:8 PCMA/8000\r\n"
        )
        sock.send(
            (
                f"INVITE sip:ai@{address} SIP/2.0\r\n"
                f"Via: SIP/2.0/UDP {me}:{my_port};branch=z9hG4bK{tag};rport\r\n"
                "Max-Forwards: 70\r\n"
                f"From: <sip:access@stranger.invalid>;tag={tag}\r\n"
                f"To: <sip:ai@{address}>\r\n"
                f"Call-ID: {tag}@stranger.invalid\r\n"
                "CSeq: 1 INVITE\r\n"
                f"Contact: <sip:access@{me}:{my_port}>\r\n"
                "Content-Type: application/sdp\r\n"
                f"Content-Length: {len(sdp)}\r\n\r\n{sdp}"
            ).encode()
        )
        end = time.monotonic() + timeout
        while not codes or codes[-1] < 200:
            sock.settimeout(max(end - time.monotonic(), 0.001))
            try:
                status = sock.recv(65535).split(b"\r\n", 1)[0].split()
            except (TimeoutError, ConnectionRefusedError):
                break
            if len(status) > 1 and status[0] == b"SIP/2.0" and status[1].isdigit():
                codes.append(int(status[1]))
    return codes


def drill_crash(drill: Drill) -> list[Check]:
    call = drill.place(105)
    call.wait(call.answered, timeout=15)
    killed = time.monotonic()
    drill.compose("kill", "voice")
    try:
        check = team_check(
            "voice crashes mid-call: the team rings", call, killed, " from the kill"
        )
        ending = call.ending()
    finally:
        drill.restart("voice")
    drill.quiet()
    return [check, ("voice crashes: no ending from voice", "[]" in ending, ending)]


def drill_voice_down(drill: Drill) -> list[Check]:
    drill.compose("stop", "voice")
    try:
        call = drill.place(104)
        check = team_check("voice down: the team rings", call, call.placed)
        unreached = any("voice not reached" in line for line in call.lines("asterisk"))
    finally:
        drill.restart("voice")
    drill.quiet()
    return [
        check,
        ("voice down: the dialplan does not ask it for an ending", unreached, ""),
    ]


def drill_voice_frozen(drill: Drill) -> list[Check]:
    """`voice` takes the connection and answers nothing: its process is paused,
    so the kernel completes the TCP handshake and the WebSocket handshake waits
    for ever. Asterisk before 22.11.0 waited with it and the team never rang
    (Asterisk issue #1979, Codex on PR #297)."""
    drill.compose("pause", "voice")
    try:
        call = drill.place(107)
        check = team_check("voice frozen: the team rings", call, call.placed)
    finally:
        drill.compose("unpause", "voice")
    drill.quiet()
    return [check]


def drill_voice_frozen_mid_call(drill: Drill) -> list[Check]:
    """`voice` stops answering after it took the call: only Asterisk's pings
    notice (websocket_client.conf); the backstop of the dialplan is an hour."""
    call = drill.place(108)
    call.wait(call.answered, timeout=15)
    frozen = time.monotonic()
    drill.compose("pause", "voice")
    try:
        check = team_check(
            "voice frozen mid-call: the team rings", call, frozen, " from the freeze"
        )
    finally:
        drill.compose("unpause", "voice")
    drill.quiet()
    return [check]


def drill_database_down(drill: Drill) -> list[Check]:
    drill.compose("stop", "db")
    try:
        call = drill.place(106)
        check = team_check("database down: the team rings", call, call.placed)
    finally:
        drill.restart("db")
    drill.quiet()
    return [check]


SCENARIOS: list[tuple[str, Callable[[Drill], list[Check]]]] = [
    ("a silent caller (101)", drill_silent),
    ("a call from our own number (100)", drill_own_number),
    ("an INVITE from a stranger", drill_stranger),
    ("keys, and the model is down (102)", drill_keys),
    ("mode paused (103)", drill_paused),
    ("the sentences after a team leg nobody answered", drill_announcements),
    ("the team does not answer (111)", drill_team_missed),
    ("the team does not answer a paused line (112)", drill_team_missed_paused),
    ("the team does not answer, voice goes down (113)", drill_team_missed_voice_down),
    ("a transfer to a number off the list (109)", drill_unlisted),
    ("a transfer to a busy number on the list (110)", drill_listed_busy),
    ("voice crashes in the middle of a call (105)", drill_crash),
    ("voice is down (104)", drill_voice_down),
    ("voice is frozen (107)", drill_voice_frozen),
    ("voice freezes in the middle of a call (108)", drill_voice_frozen_mid_call),
    ("the database is down (106)", drill_database_down),
]


def render(run_dir: Path, secret: dict[str, str]) -> None:
    """The router's configuration with a fresh password."""
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / RUN_MARKER).touch()
    (run_dir / "report.json").unlink(missing_ok=True)
    target = run_dir / "router"
    shutil.rmtree(target, ignore_errors=True)
    target.mkdir()
    sources = [
        CONFIG_DIR / "conf" / n
        for n in ("asterisk.conf", "logger.conf", "modules.conf")
    ]
    sources += sorted((CONFIG_DIR / "drill").iterdir())
    for source in sources:
        rendered = target / source.name
        rendered.touch(mode=0o600)
        # Escaped as render.sh does for the AI telephone: ";" starts a comment.
        password = secret["ACCESS_PASSWORD"].replace(";", r"\;")
        rendered.write_text(source.read_text().replace(PASSWORD_MARK, password))


def save_output(drill: Drill) -> bool:
    """What the containers printed, as drill.log in the run directory."""
    if drill.dry_run:
        return True
    try:
        output = drill.compose("logs", "--no-color", "asterisk", "router", "voice")
        (drill.run_dir / "drill.log").write_text(output)
    except (DrillError, OSError) as exc:
        print(
            f"voice_drill: saving the containers' output failed: {exc}", file=sys.stderr
        )
        return False
    return True


def stop(drill: Drill) -> bool:
    step("Stop the containers")
    try:
        drill.compose("down", "--volumes")
    except DrillError as exc:
        print(f"voice_drill: {exc}", file=sys.stderr)
        return False
    return True


def report(checks: list[Check], run_dir: Path) -> int:
    print()
    for name, ok, detail in checks:
        print(f"{'pass' if ok else 'FAIL'}  {name}: {detail}")
    passed = sum(ok for _, ok, _ in checks)
    print(f"\n{passed} of {len(checks)} points passed")
    (run_dir / "report.json").write_text(
        json.dumps(
            [{"point": n, "ok": ok, "detail": d} for n, ok, d in checks], indent=2
        )
    )
    return 0 if checks and passed == len(checks) else 1
