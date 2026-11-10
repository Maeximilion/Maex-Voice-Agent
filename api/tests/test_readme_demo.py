"""The recorder of the README demo (`scripts/readme_demo.py`): its pure parts only.

No Docker, browser or ffmpeg is needed here; the recording itself is run by hand.
"""

from __future__ import annotations

import http.server
import json
import subprocess
import threading
import urllib.request
from pathlib import Path

import pytest

from scripts import readme_demo as rd
from scripts.readme_assets import TOKENS

REPLAY = f"""\
--- readme_demo_pickup: README demo
Kunde: Guten Tag, ich moechte etwas zum Abholen bestellen.
  tool get_service_status ok 4.4 ms
Agent: Guten Tag, hier ist der KI-Assistent.
  Zustand: {{'stage': 'start', 'slots': {{}}}}
{rd.MARKER}
Kunde: Zweimal die 25.
  tool search_menu ok 7.0 ms
Agent: Nummer 25, sehr gern. Darf es noch etwas sein?
  Zustand: {{'stage': 'start'}}
{rd.MARKER}
Kunde: Ja, passt so.
  tool confirm ok 9.7 ms
Agent: Ihre Bestellung ist aufgegeben.
  Zustand: {{'stage': 'confirmed'}}
{rd.MARKER}
Anruf beendet: completed, 5 s
  Datenbank: keine Reservierung
"""


def parsed() -> tuple[list[list[str]], list[str]]:
    return rd.parse_turns(REPLAY)


def test_parse_turns_cuts_at_markers_and_drops_state_and_header() -> None:
    turns, tail = parsed()
    assert len(turns) == 3
    assert turns[0] == [
        "Kunde: Guten Tag, ich moechte etwas zum Abholen bestellen.",
        "  tool get_service_status ok 4.4 ms",
        "Agent: Guten Tag, hier ist der KI-Assistent.",
    ]
    assert not any(
        "Zustand" in line or line.startswith("---") for t in turns for line in t
    )
    assert tail[0] == "Anruf beendet: completed, 5 s"


def test_events_keep_the_replay_lines_and_drop_the_reservation_summary() -> None:
    turns, tail = parsed()
    events = rd.events_of("run it", turns, tail)
    assert [e.kind for e in events] == [
        "command", "caller", "tool", "agent",
        "caller", "tool", "agent",
        "caller", "tool", "agent", "end",
    ]  # fmt: skip
    assert events[0].text == "$ run it"
    assert (
        events[1].text == "Kunde: Guten Tag, ich moechte etwas zum Abholen bestellen."
    )
    assert events[2].text == "tool get_service_status ok 4.4 ms"
    assert events[-1].text == "Anruf beendet: completed, 5 s"
    assert not any("Datenbank" in e.text for e in events)


def test_an_unknown_line_is_refused_not_guessed() -> None:
    with pytest.raises(ValueError, match="unknown kind"):
        rd.events_of("x", [["Kunde: a", "Something else"]], [])


def test_wrap_keeps_short_text_and_ellipsizes_a_long_one() -> None:
    assert rd.wrap_event("Kunde: Zweimal die 25.") == ["Kunde: Zweimal die 25."]
    long = "Agent: " + "wort " * 200
    lines = rd.wrap_event(long)
    assert len(lines) == rd.MAX_EVENT_LINES
    assert lines[-1].endswith("…")
    assert all(len(line) <= rd.CHARS for line in lines)


def test_visible_lines_scroll_to_the_newest_rows() -> None:
    turns, tail = parsed()
    events = rd.events_of("run it", turns, tail)
    first = rd.visible_lines(events, 0)
    assert first == [("command", "$ run it")]
    last = rd.visible_lines(events, len(events) - 1, rows=3)
    assert len(last) == 3
    assert last[-1] == ("end", "Anruf beendet: completed, 5 s")


def test_board_shot_changes_only_after_the_turn_that_booked() -> None:
    turns, tail = parsed()
    events = rd.events_of("run it", turns, tail)
    shots = [rd.board_index(events, i) for i in range(len(events))]
    # command: before the call; the caller's line shows the state before its turn;
    # tool call and answer show the state after it; the end shows the last one.
    assert shots == [0, 0, 1, 1, 1, 2, 2, 2, 3, 3, 3]
    assert max(shots) == len(turns)


def test_hold_is_longer_for_a_longer_answer() -> None:
    short = rd.Event("agent", "Agent: ja", 0)
    long = rd.Event("agent", "Agent: " + "x" * 150, 0)
    assert (
        rd.hold_seconds(long)
        > rd.hold_seconds(short)
        > rd.hold_seconds(rd.Event("tool", "tool", 0))
    )


def test_frame_html_uses_the_brand_colours_and_escapes_text() -> None:
    html = rd.frame_html(
        [("caller", "Kunde: <b>&"), ("agent", "Agent: ok"), ("tool", "tool x")],
        "board2.png",
    )
    assert TOKENS["bg"] in html and TOKENS["surface"] in html and TOKENS["grid"] in html
    assert f"color:{TOKENS['neon']}" in html
    assert f"color:{TOKENS['violet']}" in html
    assert f"color:{TOKENS['muted']}" in html
    assert "Kunde: &lt;b&gt;&amp;" in html
    assert '<img src="board2.png">' in html


def test_frame_is_not_wider_than_1200_px() -> None:
    assert rd.WIDTH <= 1200


def test_size_guard() -> None:
    rd.check_size(rd.MAX_BYTES)
    with pytest.raises(SystemExit):
        rd.check_size(rd.MAX_BYTES + 1)


def test_board_check_wants_an_empty_board_before_the_yes_and_a_card_after() -> None:
    rd.check_board(["a", "b", "x Mueller x"], "Mueller")
    with pytest.raises(SystemExit):
        rd.check_board(["a", "Mueller", "Mueller"], "Mueller")  # booked too early
    with pytest.raises(SystemExit):
        rd.check_board(["a", "b", "c"], "Mueller")  # never booked


def test_demo_call_file_is_a_replay_case_with_the_reserved_caller_id() -> None:
    root = Path(__file__).resolve().parents[2]
    case = json.loads((root / rd.CALL_FILE).read_text(encoding="utf-8"))
    assert case["caller_id"] == "+493023125042"
    texts = " ".join(t["text"] for t in case["transcript"])
    assert not any(ch.isdigit() for ch in texts.replace("25", ""))  # no number spoken
    assert all(t["role"] == "customer" for t in case["transcript"])
    # The caller ID is in no other file the demo ships.
    assert (root / "scripts" / "readme_demo.py").read_text(encoding="utf-8").count(
        "+49"
    ) == 1


def test_stack_environment_does_not_pass_the_callers_shell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://elsewhere")
    monkeypatch.setenv("LLM_BASE_URL", "http://elsewhere")
    env = rd.stack_environment()
    assert env["DATABASE_URL"] == rd.STACK_ENV["DATABASE_URL"]
    assert "LLM_BASE_URL" not in env
    assert env["ENV"] == "dev"


def test_a_failing_compose_command_says_what_it_printed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert kwargs["check"] is False
        return subprocess.CompletedProcess(cmd, 3, "", "boom: image build failed")

    monkeypatch.setattr(rd.subprocess, "run", fake_run)
    stack = rd.Stack(tmp_path, 12345)
    with pytest.raises(SystemExit) as failed:
        stack.run("up", "-d", "--build")
    assert "up -d --build" in str(failed.value)
    assert "boom: image build failed" in str(failed.value)
    assert stack.run("down", check=False).returncode == 3  # unchecked stays silent


def test_a_failed_replay_stops_before_anything_is_encoded() -> None:
    rd.check_replay(0, ["Anruf beendet: completed, 5 s"])
    with pytest.raises(SystemExit, match="Traceback line"):
        rd.check_replay(1, ["Kunde: a", "Traceback line"])


def test_cookie_proxy_forwards_the_cookie_and_the_body() -> None:
    seen: list[str | None] = []

    class Upstream(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            seen.append(self.headers.get("Cookie"))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"board")

        def log_message(self, *args: object) -> None:
            pass

    upstream = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    proxy = rd.CookieProxy(upstream.server_address[1], {"maex_session": "abc"})
    for server in (upstream, proxy):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        assert proxy.server_address[0] == "127.0.0.1"
        url = f"http://127.0.0.1:{proxy.server_address[1]}/gui/"
        assert urllib.request.urlopen(url, timeout=10).read() == b"board"
        assert seen == ["maex_session=abc"]
    finally:
        for server in (upstream, proxy):
            server.shutdown()
            server.server_close()


@pytest.mark.parametrize(
    ("raw", "sent"),
    [
        ("text/html", "text/html"),
        ("a\r\nSet-Cookie: x=1", "aSet-Cookie: x=1"),
        ("a\nb\rc", "abc"),
    ],
)
def test_a_forwarded_header_is_one_line(raw: str, sent: str) -> None:
    assert rd._one_line(raw) == sent
