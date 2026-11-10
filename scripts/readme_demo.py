"""Record the README demo, `.github/assets/demo.gif`, from a real run of the product.

    py -3.14 -m scripts.readme_demo [--keep] [--out PATH]

One command, end to end. It starts a throwaway Compose project of this checkout
(`readmedemo`: database and api only, on a free localhost port, never the working
stack), migrates, loads the repository's evaluation menu, replays the demo call
`scripts/demo/pickup_call.json` turn by turn and takes a screenshot of the operations
view for the tablet (`/gui/`) after every turn. The frames put the replay text on the
left and the screenshot on the right; ffmpeg turns them into the GIF. The stack is
removed in a `finally`, with its volume and image.

Nothing is staged: the left pane is the output of `sim.replay.replay` and
`sim.session.render_turn` (the text phone), the right pane is the real view. A line too
long for the pane is wrapped, and cut with an ellipsis past a few lines, never reworded.
The run fails if the board is not empty before the caller's yes or has no card after it.

Needs on the host: Docker, a Chromium-family browser and ffmpeg. The sign-in of the
view is the product's own setup link (`python -m api.jobs.gui_access link`), used once
over HTTP; a small local proxy adds the session cookie for the headless browser, which
cannot be handed one on its command line.

Only the standard library plus `scripts.readme_assets` (colours and font) and
`scripts.readme_check` (the size limit).
"""

from __future__ import annotations

import argparse
import contextlib
import http.client
import http.cookiejar
import http.server
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import textwrap
import threading
import urllib.parse
import urllib.request
from dataclasses import dataclass
from html import escape
from pathlib import Path

from scripts.readme_assets import FONT, TOKENS
from scripts.readme_check import MAX_GIF_BYTES

REPO = Path(__file__).resolve().parent.parent
PROJECT = "readmedemo"
LOOPBACK = "127.0.0.1"
CALL_FILE = "scripts/demo/pickup_call.json"
OUT = REPO / ".github" / "assets" / "demo.gif"
MAX_BYTES = MAX_GIF_BYTES  # the limit the CI check holds the file to
MARKER = "@@END-OF-TURN"

# The caller ID of the demo call (scripts/demo/pickup_call.json) is +493023125042: Berlin
# 030 23125 000 to 999, the range the Bundesnetzagentur reserves for media productions
# ("Drama Numbers", Amtsblatt-Mitteilung 148/2021), assigned to nobody.
# Handed to Compose through the process environment, never written to a file. Fixed and
# not secret: the database is not published and the stack holds invented data only.
STACK_ENV = {
    "POSTGRES_USER": "maex",
    "POSTGRES_PASSWORD": "readmedemo",
    "POSTGRES_DB": "maex_agent",
    "DATABASE_URL": "postgresql+psycopg://maex:readmedemo@db:5432/maex_agent",
    "AGENT_API_TOKEN": "readmedemo-token",
    "ENV": "dev",
    "TENANT_NAME": "Demo",
    "TENANT_TIMEZONE": "Europe/Berlin",
    "KITCHEN_BRIDGE_TENANT_ID": "",
}
# What the docker client needs from the caller's shell. Anything else (DATABASE_URL,
# LLM_BASE_URL, COMPOSE_FILE ...) would reach the stack, because Compose reads the shell
# before it reads a file.
KEEP_ENV = (
    "PATH", "HOME", "USER", "USERNAME", "LANG", "SYSTEMROOT", "WINDIR", "COMSPEC",
    "PATHEXT", "TEMP", "TMP", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA",
    "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432", "XDG_RUNTIME_DIR",
    "XDG_CONFIG_HOME", "SSH_AUTH_SOCK",
)  # fmt: skip

# --- frame geometry -------------------------------------------------------------------

WIDTH, HEIGHT = 1200, 700
PAD = 24
LEFT_W = 560
RIGHT_W = WIDTH - 3 * PAD - LEFT_W
HEAD_H = 34
PANE_H = HEIGHT - 2 * PAD - HEAD_H
# The tablet view is captured at this size and shown scaled to the pane.
SHOT_W = 760
SHOT_H = round(PANE_H * SHOT_W / RIGHT_W)
FONT_PX, LINE_PX = 14, 21
CHARS = 66  # characters per line in the left pane at FONT_PX
ROWS = (PANE_H - 28) // LINE_PX
MAX_EVENT_LINES = 5
FPS = 10
# Seconds a frame stays, by what appeared in it.
HOLD = {"command": 1.0, "caller": 0.9, "tool": 0.5, "agent": 0.8, "end": 3.0}

# --- the transcript: pure parts -------------------------------------------------------


@dataclass(frozen=True)
class Event:
    """One line of the text phone: what appears in one frame."""

    kind: str  # command | caller | tool | agent | end
    text: str
    turn: int  # index of the turn it belongs to, -1 for the command; the end belongs to the last


def parse_turns(output: str) -> tuple[list[list[str]], list[str]]:
    """Cut the replay output at its end-of-turn markers.

    Returns the lines of each turn and the lines after the last marker. Header and
    state lines (`Zustand: ...`, a dump for developers) are dropped; nothing else is."""
    turns: list[list[str]] = [[]]
    for raw in output.splitlines():
        line = raw.rstrip()
        if line == MARKER:
            turns.append([])
        elif line.strip().startswith("Zustand:") or line.startswith("---"):
            continue
        elif line:
            turns[-1].append(line)
    *done, tail = turns
    return [t for t in done if t], tail


def events_of(command: str, turns: list[list[str]], tail: list[str]) -> list[Event]:
    """Frames of the left pane, in order: the command, then each turn's lines."""
    events = [Event("command", f"$ {command}", -1)]
    for index, lines in enumerate(turns):
        for line in lines:
            stripped = line.strip()
            if line.startswith("Kunde:"):
                kind = "caller"
            elif line.startswith("Agent:"):
                kind = "agent"
            elif stripped.startswith("tool "):
                kind = "tool"
            else:
                raise ValueError(f"line of an unknown kind in turn {index}: {line!r}")
            events.append(Event(kind, stripped, index))
    # `Anruf beendet: ...` ends the call. The line `Datenbank: keine Reservierung` of
    # `sim.replay` is left out here: it counts reservations only, not orders.
    events += [
        Event("end", t.strip(), len(turns) - 1) for t in tail if t.startswith("Anruf")
    ]
    return events


def wrap_event(
    text: str, width: int = CHARS, max_lines: int = MAX_EVENT_LINES
) -> list[str]:
    """Wrap to the pane. Past `max_lines` the last line ends in an ellipsis."""
    lines = textwrap.wrap(text, width, subsequent_indent="  ") or [""]
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1][: width - 1].rstrip() + "…"
    return lines


def visible_lines(
    events: list[Event], upto: int, rows: int = ROWS
) -> list[tuple[str, str]]:
    """The last `rows` wrapped lines of events[0..upto], like a terminal that scrolls."""
    lines = [(e.kind, row) for e in events[: upto + 1] for row in wrap_event(e.text)]
    return lines[-rows:]


def board_index(events: list[Event], upto: int) -> int:
    """Which tablet screenshot goes with frame `upto`.

    Screenshot 0 is taken before the call, screenshot n after turn n. The caller's own
    line still shows the state before that turn; the tool calls and the answer show the
    state after it (the board changes at `confirm`, which is a tool call of the turn)."""
    event = events[upto]
    if event.kind == "command":
        return 0
    if event.kind == "caller":
        return event.turn
    return event.turn + 1


def hold_seconds(event: Event) -> float:
    """How long a frame stays: a fixed time per kind, and for the agent's speech a bit
    longer for a longer sentence, so it can be read."""
    extra = 0.01 * len(event.text) if event.kind == "agent" else 0.0
    return round(HOLD[event.kind] + extra, 2)


COLORS = {
    "command": "ink",
    "caller": "neon",
    "tool": "muted",
    "agent": "violet",
    "end": "ink",
}


def frame_html(lines: list[tuple[str, str]], board_png: str) -> str:
    """One frame as a page: the text phone on the left, the tablet screenshot on the right."""
    t = TOKENS
    body = "".join(
        f'<div class="l" style="color:{t[COLORS[kind]]}">{escape(text) or "&nbsp;"}</div>'
        for kind, text in lines
    )
    return f"""<!doctype html><meta charset="utf-8">
<style>
html,body{{margin:0;width:{WIDTH}px;height:{HEIGHT}px;background:{t["bg"]};overflow:hidden}}
.head{{position:absolute;top:{PAD}px;height:{HEAD_H}px;line-height:{HEAD_H}px;color:{t["muted"]};
font:600 13px {FONT};letter-spacing:.12em;text-transform:uppercase}}
.pane{{position:absolute;top:{PAD + HEAD_H}px;height:{PANE_H}px;box-sizing:border-box;
background:{t["surface"]};border:1px solid {t["grid"]};border-radius:10px;overflow:hidden}}
.phone{{left:{PAD}px;width:{LEFT_W}px;padding:14px 16px;font:{FONT_PX}px/{LINE_PX}px {FONT};
white-space:pre}}
.tablet{{left:{2 * PAD + LEFT_W}px;width:{RIGHT_W}px}}
.tablet img{{display:block;width:{RIGHT_W - 2}px}}
</style>
<div class="head" style="left:{PAD}px">Text phone: one replayed call</div>
<div class="head" style="left:{2 * PAD + LEFT_W}px">Team tablet: operations view</div>
<div class="pane phone">{body}</div>
<div class="pane tablet"><img src="{board_png}"></div>
"""


def check_size(size: int, limit: int = MAX_BYTES) -> None:
    """The GIF must stay light enough for the README (and under the limit of the repo)."""
    if size > limit:
        raise SystemExit(f"demo.gif is {size} bytes, over the limit of {limit}")


def check_board(shots: list[str], name: str) -> None:
    """The point of the demo, checked on the real pages: nothing is booked before the
    caller's yes (shots 0 to 4), the order card is there after it (shot 5)."""
    for index, dom in enumerate(shots):
        has_card = name in dom
        want = index == len(shots) - 1
        if has_card != want:
            raise SystemExit(
                f"board after turn {index}: the card of {name!r} is "
                f"{'there' if has_card else 'missing'}, expected the opposite"
            )


# --- on the host: Docker, browser, ffmpeg ---------------------------------------------


def find_browser() -> str:
    """A Chromium-family browser: PATH first, then the default places on each system."""
    for name in ("chrome", "google-chrome", "chromium", "chromium-browser", "msedge"):
        if found := shutil.which(name):
            return found
    roots = [
        os.environ.get(v, "")
        for v in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")
    ]
    candidates = [
        Path(root) / sub
        for root in roots
        if root
        for sub in (
            "Google/Chrome/Application/chrome.exe",
            "Chromium/Application/chrome.exe",
            "Microsoft/Edge/Application/msedge.exe",
        )
    ] + [
        Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
        Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
    ]
    for path in candidates:
        if path.is_file():
            return str(path)
    raise SystemExit("no Chromium-family browser found (Chrome, Chromium or Edge)")


def require_tools() -> tuple[str, str]:
    if not shutil.which("docker"):
        raise SystemExit("docker not found on PATH")
    if not shutil.which("ffmpeg"):
        raise SystemExit("ffmpeg not found on PATH")
    return find_browser(), "ffmpeg"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind((LOOPBACK, 0))
        return sock.getsockname()[1]


def stack_environment() -> dict[str, str]:
    keep = {k: v for k, v in os.environ.items() if k.upper() in KEEP_ENV}
    keep |= {k: v for k, v in os.environ.items() if k.startswith("DOCKER_")}
    keep |= {k: v for k, v in os.environ.items() if k.lower().endswith("_proxy")}
    return keep | STACK_ENV


class Stack:
    """The throwaway Compose project: own name, own port on loopback, own volume."""

    def __init__(self, work: Path, port: int):
        self.port = port
        self.work = work
        (work / "empty.env").write_text("")
        (work / "override.yml").write_text(
            f'services:\n  api:\n    ports:\n      - "{LOOPBACK}:{port}:8000"\n'
        )

    @property
    def base(self) -> str:
        return f"http://{LOOPBACK}:{self.port}"

    def compose(self, *args: str) -> list[str]:
        return [
            "docker", "compose", "-p", PROJECT, "--project-directory", str(REPO),
            # Not the checkout's `.env`: the values come from STACK_ENV.
            "--env-file", str(self.work / "empty.env"),
            "-f", str(REPO / "docker-compose.yml"),
            "-f", str(self.work / "override.yml"),
            *args,
        ]  # fmt: skip

    def run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        print("+ docker compose", *args, flush=True)
        return subprocess.run(
            self.compose(*args), check=check, env=stack_environment(),
            capture_output=True, text=True, encoding="utf-8",
        )  # fmt: skip

    def exec(self, *cmd: str) -> str:
        return self.run("exec", "-T", "api", *cmd).stdout

    def up(self) -> None:
        self.run("down", "-v", "--remove-orphans", check=False)
        self.run("up", "-d", "--wait", "--build", "db", "api")
        self.exec("alembic", "-c", "db/alembic.ini", "upgrade", "head")
        self.exec("python", "-m", "scripts.demo.stack_setup")

    def down(self) -> None:
        self.run("down", "-v", "--remove-orphans", "--rmi", "local", check=False)

    def replay(self) -> subprocess.Popen[str]:
        """The call, paced: one turn per line written to its stdin."""
        cmd = self.compose(
            "exec",
            "-T",
            "api",
            "python",
            "-u",
            "-m",
            "scripts.demo.paced_replay",
            CALL_FILE,
        )
        return subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", env=stack_environment(),
        )  # fmt: skip


# --- the operations view: sign-in, proxy, screenshots ---------------------------------


def sign_in(stack: Stack) -> dict[str, str]:
    """The product's own setup link, used once: password, team PIN and owner PIN."""
    link = stack.exec(
        "python", "-m", "api.jobs.gui_access", "link", "--base-url", stack.base
    )
    code = link.strip().splitlines()[0].rsplit("#", 1)[1]
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    page = opener.open(f"{stack.base}/gui/einrichten").read().decode()
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', page)
    if not csrf:
        raise SystemExit("setup page has no csrf_token")
    password = secrets.token_urlsafe(18)
    form = {"csrf_token": csrf.group(1), "code": code}
    for field, value in (
        ("passwort", password),
        ("pin", "4821"),
        ("admin_pin", "730915"),
    ):
        form[field] = form[field + "2"] = value
    request = urllib.request.Request(
        f"{stack.base}/gui/einrichten",
        urllib.parse.urlencode(form).encode(),
        headers={"Origin": stack.base},
    )
    opener.open(request).read()
    cookies = {c.name: c.value or "" for c in jar}
    if not any("session" in name for name in cookies):
        raise SystemExit("sign-in gave no session cookie")
    return cookies


class CookieProxy(http.server.ThreadingHTTPServer):
    """Forwards GETs to the api with the session cookie added."""

    daemon_threads = True

    def __init__(self, upstream_port: int, cookies: dict[str, str]):
        cookie = "; ".join(f"{k}={v}" for k, v in cookies.items())

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                conn = http.client.HTTPConnection(LOOPBACK, upstream_port, timeout=30)
                conn.request("GET", self.path, headers={"Cookie": cookie})
                response = conn.getresponse()
                self.send_response(response.status)
                for key, value in response.getheaders():
                    if key.lower() not in (
                        "transfer-encoding",
                        "connection",
                        "content-length",
                    ):
                        self.send_header(key, value)
                self.end_headers()
                with contextlib.suppress(OSError, http.client.HTTPException):
                    while chunk := response.read1(8192):
                        self.wfile.write(chunk)
                        self.wfile.flush()

            def log_message(self, *args: object) -> None:
                pass

        super().__init__((LOOPBACK, 0), Handler)


def browser(exe: str, profile: Path, size: tuple[int, int], *args: str) -> str:
    cmd = [
        exe, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run",
        "--force-device-scale-factor=1", f"--user-data-dir={profile}",
        f"--window-size={size[0]},{size[1]}", *args,
    ]  # fmt: skip
    done = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", timeout=120
    )
    if done.returncode != 0:
        raise SystemExit(f"browser failed: {done.stderr.strip()[-300:]}")
    return done.stdout


def screenshot_board(exe: str, work: Path, url: str, png: Path) -> str:
    """Shoot the view and return its DOM text (for the check that the card is, or is not, there)."""
    profile = work / "profile"
    dom = browser(exe, profile, (SHOT_W, SHOT_H), "--dump-dom", url)
    browser(exe, profile, (SHOT_W, SHOT_H), f"--screenshot={png}", url)
    return dom


def render_frame(exe: str, work: Path, html: str, name: str) -> Path:
    page, png = work / f"{name}.html", work / f"{name}.png"
    page.write_text(html, encoding="utf-8")
    browser(
        exe, work / "profile", (WIDTH, HEIGHT), f"--screenshot={png}", page.as_uri()
    )
    return png


def encode(
    ffmpeg: str, frames: list[tuple[Path, float]], out: Path, work: Path
) -> None:
    """Palette-based GIF through the concat demuxer; each frame stays for its seconds."""
    listing = work / "frames.txt"
    rows = [f"file '{png.as_posix()}'\nduration {sec}" for png, sec in frames]
    rows.append(
        f"file '{frames[-1][0].as_posix()}'"
    )  # the concat demuxer drops the last duration
    listing.write_text("\n".join(rows) + "\n")
    filt = f"fps={FPS},split[a][b];[a]palettegen=max_colors=256[p];[b][p]paletteuse=dither=none"
    subprocess.run(
        [ffmpeg, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(listing),
         "-vf", filt, "-loop", "0", str(out)],
        check=True,
    )  # fmt: skip


# --- the whole run --------------------------------------------------------------------


def read_turn(proc: subprocess.Popen[str]) -> list[str]:
    """Output of the replay up to the next end-of-turn marker (or its end)."""
    assert proc.stdout is not None
    lines: list[str] = []
    for raw in proc.stdout:
        lines.append(raw.rstrip("\n"))
        if lines[-1] == MARKER:
            break
    return lines


def record(out: Path, keep: bool) -> None:
    exe, ffmpeg = require_tools()
    work = Path(tempfile.mkdtemp(prefix="readme_demo_"))
    stack = Stack(work, free_port())
    proxy: CookieProxy | None = None
    try:
        stack.up()
        cookies = sign_in(stack)
        proxy = CookieProxy(stack.port, cookies)
        threading.Thread(target=proxy.serve_forever, daemon=True).start()
        view = f"http://{LOOPBACK}:{proxy.server_address[1]}/gui/"

        shots: list[str] = []

        def look() -> None:
            n = len(shots)
            shots.append(screenshot_board(exe, work, view, work / f"board{n}.png"))

        look()
        call = stack.replay()
        assert call.stdin is not None
        turn_lines: list[str] = []
        while True:
            chunk = read_turn(call)
            turn_lines += chunk
            if not chunk or chunk[-1] != MARKER:
                break
            look()
            call.stdin.write("\n")
            call.stdin.flush()
        call.wait(timeout=60)
        # The last turn ended the call: its marker was answered above, the rest is the tail.
        output = "\n".join(turn_lines)
        turns, tail = parse_turns(output)
        events = events_of(
            f"python -m scripts.demo.paced_replay {CALL_FILE}", turns, tail
        )
        check_board(shots, "Mueller")

        frames = []
        for i, event in enumerate(events):
            html = frame_html(
                visible_lines(events, i), f"board{board_index(events, i)}.png"
            )
            frames.append(
                (render_frame(exe, work, html, f"frame{i:02d}"), hold_seconds(event))
            )
        # Into the work folder first: a failed or oversized encode must not
        # replace the committed recording.
        fresh = work / out.name
        encode(ffmpeg, frames, fresh, work)
        check_size(fresh.stat().st_size)
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(fresh, out)
        print(f"{out}: {out.stat().st_size} bytes, {len(frames)} steps")
    finally:
        if proxy:
            proxy.shutdown()
        if keep:
            print(f"kept: stack `{PROJECT}` on port {stack.port}, files in {work}")
        else:
            stack.down()
            shutil.rmtree(work, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, default=OUT)
    parser.add_argument(
        "--keep", action="store_true", help="leave the stack and files for debugging"
    )
    args = parser.parse_args(argv)
    record(args.out, args.keep)
    return 0


if __name__ == "__main__":
    sys.exit(main())
