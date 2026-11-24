"""Tests for scripts/uptime_check.py: the check of /health from another machine (T-9.4)."""

import importlib.util
import json
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, ClassVar

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "uptime_check",
    Path(__file__).resolve().parents[2] / "scripts" / "uptime_check.py",
)
assert _SPEC is not None and _SPEC.loader is not None
uptime_check = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(uptime_check)

T0 = datetime(2026, 11, 24, 13, 0, tzinfo=UTC)
URL = "https://a.example.com/health"


def _steps(results: list[bool], threshold: int = 3) -> list[str | None]:
    state = uptime_check.fresh_state()
    events = []
    for i, ok in enumerate(results):
        state, event = uptime_check.step(
            state, ok, T0 + timedelta(minutes=i), threshold
        )
        events.append(event)
    return events


def test_down_is_reported_once_at_the_threshold() -> None:
    assert _steps([False] * 5) == [None, None, "down", None, None]


def test_recovery_is_reported_once_after_a_reported_outage() -> None:
    assert _steps([False, False, False, True, True]) == [
        None,
        None,
        "down",
        "recovered",
        None,
    ]


def test_a_blip_below_the_threshold_reports_nothing() -> None:
    assert _steps([False, False, True, False, False, True]) == [None] * 6


def test_a_threshold_of_one_reports_the_first_failure() -> None:
    assert _steps([False, True], threshold=1) == ["down", "recovered"]


def test_the_outage_keeps_the_time_of_its_first_failure() -> None:
    state = uptime_check.fresh_state()
    for i in range(3):
        state, _ = uptime_check.step(state, False, T0 + timedelta(minutes=i), 3)
    assert state["since"] == T0.isoformat()


# The whole pass: configuration, state file and push.

ENV = {
    "UPTIME_URLS": URL,
    "UPTIME_PUSH_URL": "http://push.example.com/",
    "UPTIME_PUSH_TOKEN": "tk_" + "x" * 29,
}


class Pushes:
    def __init__(self, fail: bool = False) -> None:
        self.sent: list[tuple[str, str, str]] = []
        self.fail = fail

    def __call__(self, title: str, text: str, priority: str) -> None:
        if self.fail:
            raise OSError("push server down")
        self.sent.append((title, text, priority))


def _run(
    tmp_path: Path,
    results: list[bool],
    pushes: Pushes,
    env: dict[str, str] | None = None,
    start: datetime = T0,
) -> list[int]:
    env = {**ENV, "UPTIME_STATE_FILE": str(tmp_path / "state.json"), **(env or {})}
    codes = []
    for i, ok in enumerate(results):
        codes.append(
            uptime_check.run(
                env,
                start + timedelta(minutes=i),
                probe=lambda url, timeout, ok=ok: ok,
                send=pushes,
            )
        )
    return codes


def test_a_pass_pushes_down_and_recovery_once_each(tmp_path: Path) -> None:
    pushes = Pushes()
    codes = _run(tmp_path, [False] * 4 + [True] * 2, pushes)
    assert codes == [0] * 6
    assert [(t, p) for t, _, p in pushes.sent] == [
        ("Server not answering", "high"),
        ("Server answering again", "normal"),
    ]
    down, back = pushes.sent[0][1], pushes.sent[1][1]
    assert URL in down and "14:00" in down  # 13:00 UTC in Europe/Berlin
    assert URL in back and "4 min" in back


def test_a_failed_down_push_is_tried_again_on_the_next_pass(tmp_path: Path) -> None:
    failing = Pushes(fail=True)
    assert _run(tmp_path, [False] * 3, failing) == [0, 0, 1]
    pushes = Pushes()
    _run(tmp_path, [False], pushes, start=T0 + timedelta(minutes=3))
    assert [t for t, _, _ in pushes.sent] == ["Server not answering"]
    assert "14:00" in pushes.sent[0][1]


def test_a_failed_recovery_push_is_tried_again_on_the_next_pass(
    tmp_path: Path,
) -> None:
    _run(tmp_path, [False] * 3, Pushes())
    assert _run(
        tmp_path, [True], Pushes(fail=True), start=T0 + timedelta(minutes=3)
    ) == [1]
    pushes = Pushes()
    _run(tmp_path, [True, True], pushes, start=T0 + timedelta(minutes=4))
    assert [t for t, _, _ in pushes.sent] == ["Server answering again"]


def test_each_url_has_its_own_state(tmp_path: Path) -> None:
    other = "https://b.example.com/health"
    env = {
        **ENV,
        "UPTIME_URLS": f"{URL}, {other}",
        "UPTIME_STATE_FILE": str(tmp_path / "s.json"),
    }
    pushes = Pushes()
    for i in range(3):
        uptime_check.run(
            env,
            T0 + timedelta(minutes=i),
            probe=lambda url, timeout: url == URL,
            send=pushes,
        )
    assert len(pushes.sent) == 1 and other in pushes.sent[0][1]


def test_a_broken_state_file_starts_fresh(tmp_path: Path) -> None:
    (tmp_path / "state.json").write_text("{not json", encoding="utf-8")
    pushes = Pushes()
    assert _run(tmp_path, [False] * 3, pushes) == [0, 0, 0]
    assert len(pushes.sent) == 1


@pytest.mark.parametrize(
    "change",
    [
        {"UPTIME_URLS": ""},
        {"UPTIME_PUSH_URL": " "},
        {"UPTIME_PUSH_TOKEN": ""},
        {"UPTIME_FAILURES": "0"},
        {"UPTIME_FAILURES": "three"},
        {"UPTIME_TIMEOUT_SECONDS": "-1"},
    ],
)
def test_a_bad_configuration_refuses_before_any_probe(
    tmp_path: Path, change: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    probed: list[str] = []

    def probe(url: str, timeout: float) -> bool:
        probed.append(url)
        return True

    env = {**ENV, "UPTIME_STATE_FILE": str(tmp_path / "s.json"), **change}
    assert uptime_check.run(env, T0, probe=probe, send=Pushes()) == 2
    assert probed == []
    assert ENV["UPTIME_PUSH_TOKEN"] not in capsys.readouterr().err


# probe and push against a real HTTP server.


class _Handler(BaseHTTPRequestHandler):
    reply: ClassVar[tuple[int, bytes]] = (200, b'{"status":"ok","env":"prod"}')
    received: ClassVar[list[dict[str, Any]]] = []

    def do_GET(self) -> None:
        status, body = type(self).reply
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        length = int(self.headers["Content-Length"])
        type(self).received.append(
            {
                "auth": self.headers["Authorization"],
                "body": json.loads(self.rfile.read(length)),
            }
        )
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def server() -> Iterator[str]:
    _Handler.received = []
    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}/"
    httpd.shutdown()
    httpd.server_close()


@pytest.mark.parametrize(
    ("reply", "ok"),
    [
        ((200, b'{"status":"ok","env":"prod"}'), True),
        ((500, b'{"status":"ok"}'), False),
        ((200, b"<html>proxy page</html>"), False),
        ((200, b'{"status":"starting"}'), False),
    ],
)
def test_probe_wants_200_and_status_ok(
    server: str, reply: tuple[int, bytes], ok: bool
) -> None:
    _Handler.reply = reply
    assert uptime_check.probe(server + "health", 2.0) is ok


def test_probe_of_a_closed_port_is_down() -> None:
    assert uptime_check.probe("http://127.0.0.1:9/health", 1.0) is False


def test_push_posts_topic_title_and_priority_with_the_token(server: str) -> None:
    send = uptime_check.pusher(server, "tk_secret", "ops", 2.0)
    send("Server not answering", "text", "high")
    assert _Handler.received == [
        {
            "auth": "Bearer tk_secret",
            "body": {
                "topic": "ops",
                "title": "Server not answering",
                "message": "text",
                "priority": 4,
            },
        }
    ]


# The env file and the cron line.


def test_env_file_skips_comments_and_drops_one_pair_of_quotes(tmp_path: Path) -> None:
    path = tmp_path / "uptime.env"
    path.write_text(
        "# the checked stacks\n\nUPTIME_URLS='https://a.example.com/health'\n"
        'UPTIME_PUSH_TOKEN="tk_a=b"\nUPTIME_FAILURES = 5\nno separator\n',
        encoding="utf-8",
    )
    assert uptime_check.read_env_file(path) == {
        "UPTIME_URLS": "https://a.example.com/health",
        "UPTIME_PUSH_TOKEN": "tk_a=b",
        "UPTIME_FAILURES": "5",
    }


def test_main_refuses_a_missing_env_file_and_unknown_arguments(
    tmp_path: Path,
) -> None:
    assert uptime_check.main(["--env-file", str(tmp_path / "missing.env")]) == 2
    assert uptime_check.main(["--once"]) == 2


def test_main_reads_the_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key in ("UPTIME_URLS", "UPTIME_PUSH_URL", "UPTIME_PUSH_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    path = tmp_path / "uptime.env"
    path.write_text(
        "UPTIME_URLS=http://127.0.0.1:9/health\nUPTIME_PUSH_URL=http://127.0.0.1:9/\n"
        "UPTIME_PUSH_TOKEN=tk\nUPTIME_TIMEOUT_SECONDS=1\n"
        f"UPTIME_STATE_FILE={tmp_path / 'state.json'}\n",
        encoding="utf-8",
    )
    assert uptime_check.main(["--env-file", str(path)]) == 0
    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert state["http://127.0.0.1:9/health"]["failures"] == 1


def test_the_cron_line_runs_every_minute_with_the_env_file() -> None:
    cron = Path(__file__).resolve().parents[2] / "deploy" / "uptime.cron"
    (line,) = [
        row
        for row in cron.read_text(encoding="utf-8").splitlines()
        if row.strip() and not row.startswith("#")
    ]
    assert line.startswith("* * * * * python3 ")
    assert "scripts/uptime_check.py --env-file " in line
    assert line.endswith(" 2>&1")


def test_a_url_named_twice_pushes_once(tmp_path: Path) -> None:
    pushes = Pushes()
    _run(tmp_path, [False] * 3, pushes, env={"UPTIME_URLS": f"{URL},{URL}"})
    assert len(pushes.sent) == 1
