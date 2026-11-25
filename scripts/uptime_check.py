"""Check /health from another machine and push down and recovery once each (T-9.4).

Usage: python3 scripts/uptime_check.py [--env-file FILE]
One pass; cron runs it every minute (deploy/uptime.cron). FILE holds KEY=VALUE
lines (mode 600, it has the token) and wins over the environment.

The dispatcher's monitor (api/jobs/monitor.py) runs on the server it watches,
so it cannot report that server or its API down. This pass runs on another
machine and pushes to a push server that is not on the checked one
(docs/13_DEPLOYMENT.md section 5). Standard library only: the checking machine
needs Python 3.12 and this file, nothing of the project.

Configuration from the environment:
  UPTIME_URLS              /health addresses, separated by commas or spaces
  UPTIME_PUSH_URL          push server (ntfy JSON publishing), e.g. https://push.example.com/
  UPTIME_PUSH_TOKEN        bearer token of a user that may write the topic
  UPTIME_PUSH_TOPIC        default ops
  UPTIME_FAILURES          failed passes in a row before the push, default 3
  UPTIME_TIMEOUT_SECONDS   per request, default 10
  UPTIME_STATE_FILE        default ~/.local/state/maex-uptime.json

A URL counts as up on HTTP 200 with the JSON body {"status": "ok", ...}. After
UPTIME_FAILURES failed passes in a row one push "Server not answering" goes
out, and one "Server answering again" on the first good pass after it. A push
that fails is tried again on the next pass; nothing else repeats. Exit 0, 1
when a push failed, 2 on a bad configuration (nothing probed).
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

PRIORITIES = {"normal": 3, "high": 4}
LOCAL = ZoneInfo("Europe/Berlin")

State = dict[str, Any]
Send = Callable[[str, str, str], None]
Probe = Callable[[str, float], bool]


def fresh_state() -> State:
    return {"failures": 0, "down": False, "since": None}


def step(
    state: State, ok: bool, now: datetime, threshold: int
) -> tuple[State, str | None]:
    """The next state of one URL and the push it calls for: "down", "recovered" or None."""
    if ok:
        return fresh_state(), ("recovered" if state["down"] else None)
    failures = state["failures"] + 1
    since = state["since"] or now.isoformat()
    if not state["down"] and failures >= threshold:
        return {"failures": failures, "down": True, "since": since}, "down"
    return {"failures": failures, "down": state["down"], "since": since}, None


def probe(url: str, timeout: float) -> bool:
    """Up: 200 and {"status": "ok"}. Anything else, a timeout too, is down."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            body = json.loads(response.read(65536))
            return bool(response.status == 200 and body.get("status") == "ok")
    except Exception:  # noqa: BLE001 - every failure is the same answer: down
        return False


def pusher(url: str, token: str, topic: str, timeout: float) -> Send:
    def send(title: str, text: str, priority: str) -> None:
        request = urllib.request.Request(
            url,
            data=json.dumps(
                {
                    "topic": topic,
                    "title": title,
                    "message": text,
                    "priority": PRIORITIES[priority],
                }
            ).encode(),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout):
            pass

    return send


def _message(event: str, url: str, state: State, now: datetime) -> tuple[str, str, str]:
    since = datetime.fromisoformat(state["since"])
    clock = since.astimezone(LOCAL).strftime("%d.%m. %H:%M")
    if event == "down":
        return (
            "Server not answering",
            f"{url} has failed {state['failures']} checks in a row since {clock}.",
            "high",
        )
    minutes = round((now - since).total_seconds() / 60)
    return (
        "Server answering again",
        f"{url} answers again, down about {minutes} min since {clock}.",
        "normal",
    )


def _config(env: Mapping[str, str]) -> dict[str, Any]:
    """The settings, or ValueError naming the variable (never its value)."""
    # dict.fromkeys: a URL named twice is checked once, else it pushes twice.
    urls = list(dict.fromkeys(env.get("UPTIME_URLS", "").replace(",", " ").split()))
    push_url = env.get("UPTIME_PUSH_URL", "").strip()
    token = env.get("UPTIME_PUSH_TOKEN", "").strip()
    if not urls:
        raise ValueError("UPTIME_URLS is empty")
    if not push_url:
        raise ValueError("UPTIME_PUSH_URL is empty")
    if not token:
        raise ValueError("UPTIME_PUSH_TOKEN is empty")
    try:
        failures = int(env.get("UPTIME_FAILURES", "3"))
        timeout = float(env.get("UPTIME_TIMEOUT_SECONDS", "10"))
    except ValueError:
        raise ValueError(
            "UPTIME_FAILURES or UPTIME_TIMEOUT_SECONDS is not a number"
        ) from None
    if failures < 1 or not timeout > 0:
        raise ValueError(
            "UPTIME_FAILURES must be at least 1, UPTIME_TIMEOUT_SECONDS above 0"
        )
    state_file = env.get("UPTIME_STATE_FILE", "").strip()
    return {
        "urls": urls,
        "push_url": push_url,
        "token": token,
        "topic": env.get("UPTIME_PUSH_TOPIC", "").strip() or "ops",
        "failures": failures,
        "timeout": timeout,
        "state_file": Path(state_file or Path.home() / ".local/state/maex-uptime.json"),
    }


def _load(path: Path) -> dict[str, State]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        print(f"uptime: state file {path} unreadable, starting fresh", file=sys.stderr)
        return {}


def _save(path: Path, states: dict[str, State]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(states, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def run(
    env: Mapping[str, str],
    now: datetime,
    probe: Probe = probe,
    send: Send | None = None,
) -> int:
    try:
        config = _config(env)
    except ValueError as error:
        print(f"uptime: {error} (docs/13_DEPLOYMENT.md section 5)", file=sys.stderr)
        return 2
    if send is None:
        send = pusher(
            config["push_url"], config["token"], config["topic"], config["timeout"]
        )
    # shortcut: no lock between passes; each URL waits at most the timeout, so
    # a pass ends well inside the minute unless there are many slow URLs.
    stored = _load(config["state_file"])
    states: dict[str, State] = {}
    code = 0
    for url in config["urls"]:
        old = {**fresh_state(), **stored.get(url, {})}
        new, event = step(old, probe(url, config["timeout"]), now, config["failures"])
        if event:
            title, text, priority = _message(
                event, url, new if event == "down" else old, now
            )
            try:
                send(title, text, priority)
                print(f"uptime: {now.isoformat()} {title}: {url}")
            except Exception as error:  # noqa: BLE001 - the next pass tries again
                print(
                    f"uptime: push failed ({type(error).__name__}): {title}: {url}",
                    file=sys.stderr,
                )
                # Not booked as sent: the next pass calls for the same push.
                new = old if event == "recovered" else {**new, "down": False}
                code = 1
        states[url] = new
    _save(config["state_file"], states)
    return code


def read_env_file(path: Path) -> dict[str, str]:
    """KEY=VALUE lines; blank lines and # comments skipped, one pair of quotes dropped."""
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.strip().partition("=")
        if not sep or key.startswith("#"):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def main(argv: list[str]) -> int:
    env = dict(os.environ)
    if argv[:1] == ["--env-file"] and len(argv) == 2:
        try:
            env.update(read_env_file(Path(argv[1])))
        except OSError as error:
            print(f"uptime: env file unreadable: {error}", file=sys.stderr)
            return 2
    elif argv:
        print(__doc__, file=sys.stderr)
        return 2
    return run(env, datetime.now(UTC))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
