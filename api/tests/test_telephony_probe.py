"""telephony/probe.py: the measuring endpoint of the spike T-10.1, without a PBX.

The test plays the media channel's side of a call over a WebSocket and checks
what the probe sends back and what it writes into its log.
"""

import json
import time

import pytest
from fastapi.testclient import TestClient

from api.telephony import probe
from api.telephony.adapters.asterisk import MAX_MESSAGE_BYTES
from api.telephony.audio import tone
from api.telephony.probe import app

FRAME = 640
MEDIA_START = json.dumps(
    {
        "event": "MEDIA_START",
        "connection_id": "c-1",
        "channel": "WebSocket/voice/0x1",
        "channel_id": "pbx-1.1",
        "format": "slin16",
        "optimal_frame_size": FRAME,
        "ptime": 20,
    }
)


@pytest.fixture
def out(tmp_path, monkeypatch):
    monkeypatch.setenv("PROBE_OUT", str(tmp_path))
    return tmp_path


def connect(exten: str, caller: str = "%2B4972215550601"):
    client = TestClient(app)
    return client.websocket_connect(
        f"/media?exten={exten}&caller={caller}", subprotocols=["media"]
    )


def read_tone(ws) -> bytes:
    """Binary frames up to the command that follows them; returns the audio."""
    audio = b""
    while True:
        message = ws.receive()
        if message.get("bytes") is not None:
            assert len(message["bytes"]) <= MAX_MESSAGE_BYTES
            assert len(message["bytes"]) % FRAME == 0
            audio += message["bytes"]
        else:
            assert json.loads(message["text"]) == {"command": "REPORT_QUEUE_DRAINED"}
            return audio


def log_of(out) -> list[dict]:
    """The call's log once the probe finished it. The probe writes the closing
    record in a thread after the socket is gone, so the test waits for it."""
    (path,) = out.glob("*.jsonl")
    for _ in range(200):
        lines = path.read_text(encoding="utf-8").splitlines()
        if lines and '"closed": true' in lines[-1]:
            return [json.loads(line) for line in lines]
        time.sleep(0.05)
    raise AssertionError("the probe never finished its log")


def test_a_call_gets_its_tone_in_whole_frames_and_a_drain_request(out):
    with connect("601") as ws:
        assert ws.accepted_subprotocol == "media"
        ws.send_text(MEDIA_START)
        audio = read_tone(ws)

    assert len(audio) == 4 * 16000 * 2
    log = log_of(out)
    assert log[0]["exten"] == "601"
    assert log[0]["caller"] == "+4972215550601"
    assert {"dir": "in", "text": MEDIA_START}.items() <= log[1].items()


def test_key_five_stops_the_playback_and_asks_for_the_queue(out):
    with connect("610") as ws:
        ws.send_text(MEDIA_START)
        read_tone(ws)
        ws.send_text('{"event": "DTMF_END", "channel_id": "pbx-1.1", "digit": "5"}')

        assert json.loads(ws.receive_text()) == {"command": "FLUSH_MEDIA"}
        assert json.loads(ws.receive_text()) == {"command": "GET_STATUS"}


def test_another_key_is_logged_and_stops_nothing(out):
    with connect("601") as ws:
        ws.send_text(MEDIA_START)
        read_tone(ws)
        ws.send_text('{"event": "DTMF_END", "channel_id": "pbx-1.1", "digit": "1"}')

    log = log_of(out)
    sent = [r["text"] for r in log if r["dir"] == "out" and "text" in r]
    assert sent == ['{"command": "REPORT_QUEUE_DRAINED"}']
    assert log[-1]["digits"] == "1"


def test_extension_611_hangs_up_once_everything_was_spoken(out):
    with connect("611") as ws:
        ws.send_text(MEDIA_START)
        read_tone(ws)
        ws.send_text('{"event": "QUEUE_DRAINED", "channel_id": "pbx-1.1"}')

        assert json.loads(ws.receive_text()) == {"command": "HANGUP"}


def test_what_the_caller_plays_is_named_in_the_summary(out):
    heard = tone(1000, 1.0, 16000)
    with connect("601") as ws:
        ws.send_text(MEDIA_START)
        read_tone(ws)
        for start in range(0, len(heard), FRAME):
            ws.send_bytes(heard[start : start + FRAME])

    summary = log_of(out)[-1]
    assert summary["dir"] == "probe"
    assert summary["heard_hz"] == 1000
    assert summary["audio_in_seconds"] == pytest.approx(1.0, abs=0.02)
    assert summary["frame_bytes"] == {str(FRAME): 50}
    assert 0 <= summary["frame_gap_max_ms"] < 1000
    assert summary["tones_in"]["1000"] == pytest.approx(1.0, abs=0.03)
    assert summary["tones_in"]["1200"] == 0.0


def test_the_summary_says_when_the_callers_tone_began_not_when_audio_began(
    tmp_path, monkeypatch
):
    class Clock:
        now = 50.0

        def monotonic(self) -> float:
            return self.now

    clock = Clock()
    monkeypatch.setattr(probe, "time", clock)
    call = probe.ProbeCall("610", None, tmp_path)
    audio = bytes(FRAME * 25) + tone(1000, 1.0, 16000)  # half a second of silence first

    for start in range(0, len(audio), FRAME):
        clock.now += 0.02
        call.on_audio(audio[start : start + FRAME])
    call.close()

    (path,) = tmp_path.glob("*.jsonl")
    summary = json.loads(path.read_text(encoding="utf-8").splitlines()[-1])
    assert summary["tones_in_at"]["1000"] == pytest.approx(0.52, abs=0.03)
    assert summary["tones_in_at"]["1200"] is None


def test_an_extension_without_a_plan_gets_silence_and_a_log_line(out):
    with connect("699") as ws:
        ws.send_text(MEDIA_START)
        ws.send_text('{"event": "QUEUE_DRAINED", "channel_id": "pbx-1.1"}')

    log = log_of(out)
    assert not [r for r in log if r["dir"] == "out"]
    assert log[-1]["heard_hz"] is None
    assert log[-1]["frame_gap_max_ms"] is None


CALLER = "+4917612345678"
# A call of the product's dialplan (T-10.1 step 2): no extension in the URL, the
# call and the caller as channel variables, as recorded from Asterisk.
LINE_START = json.dumps(
    {
        **json.loads(MEDIA_START),
        "channel_variables": {"__VOICE_CALL": "1791981993.2", "__VOICE_CALLER": CALLER},
    }
)


def connect_line():
    return TestClient(app).websocket_connect("/media", subprotocols=["media"])


def test_a_line_call_is_answered_toned_echoed_and_logs_no_number(out):
    speech = tone(300, 0.2, 16000)
    with connect_line() as ws:
        ws.send_text(LINE_START)
        assert json.loads(ws.receive_text()) == {"command": "ANSWER"}
        assert len(read_tone(ws)) == int(probe.LINE_PLAN.seconds * 16000 * 2)
        for start in range(0, len(speech), FRAME):
            ws.send_bytes(speech[start : start + FRAME])
            assert ws.receive_bytes() == speech[start : start + FRAME]

    text = next(out.glob("*.jsonl")).read_text(encoding="utf-8")
    assert CALLER not in text and "12345678" not in text
    log = log_of(out)
    assert {"caller_seen": "+49xxxxxxxxx78", "live": 1}.items() <= log[2].items()
    assert log[-1]["voice_in_seconds"] == pytest.approx(0.2, abs=0.02)
    assert log[-1]["echoed_seconds"] == pytest.approx(0.2, abs=0.02)


def test_a_line_call_hangs_up_once_after_its_time(out, monkeypatch):
    monkeypatch.setattr(probe, "LINE_SECONDS", 0)
    with connect_line() as ws:
        ws.send_text(LINE_START)
        ws.receive_text()
        read_tone(ws)
        ws.send_bytes(bytes(FRAME))
        assert json.loads(ws.receive_text()) == {"command": "HANGUP"}
        ws.send_bytes(bytes(FRAME))
        ws.send_text('{"event": "QUEUE_DRAINED", "channel_id": "pbx-1.1"}')

    sent = [r.get("text") for r in log_of(out) if r["dir"] == "out" and "text" in r]
    assert sent.count('{"command": "HANGUP"}') == 1
    assert log_of(out)[-1]["voice_in_seconds"] == 0.0


def test_a_line_call_without_a_caller_is_answered_and_says_so(out):
    start = json.dumps({**json.loads(LINE_START), "channel_variables": {}})
    with connect_line() as ws:
        ws.send_text(start)
        assert json.loads(ws.receive_text()) == {"command": "ANSWER"}
        read_tone(ws)

    log = log_of(out)
    assert [r for r in log if r.get("caller_seen") == "none"]
    assert [r for r in log if "error" in r]


def test_a_second_line_call_counts_two_live_calls(out):
    with connect_line() as first, connect_line() as second:
        first.send_text(LINE_START)
        first.receive_text()
        second.send_text(LINE_START)
        second.receive_text()

    lives = sorted(
        json.loads(line)["live"]
        for path in out.glob("*.jsonl")
        for line in path.read_text(encoding="utf-8").splitlines()
        if '"live"' in line
    )
    assert lives == [1, 2]


@pytest.mark.parametrize(
    ("path", "body"), [("/calls/1791981993.2/ending", "hangup"), ("/health", "ok")]
)
def test_the_dialplan_is_told_hangup_so_a_test_call_never_rings_the_team(path, body):
    assert TestClient(app).get(path).text == body


@pytest.mark.parametrize(
    ("number", "seen"),
    [(None, "none"), ("0761234567", "076xxxxx67"), ("12345", "xxxxx")],
)
def test_masked_keeps_the_form_of_a_number_and_hides_the_rest(number, seen):
    assert probe.masked(number) == seen


@pytest.mark.parametrize("size", [0, -640, None, "x"])
def test_an_unusable_frame_size_is_logged_and_the_call_goes_on(out, size):
    start = json.dumps({**json.loads(MEDIA_START), "optimal_frame_size": size})
    with connect("601") as ws:
        ws.send_text(start)
        ws.send_text(MEDIA_START)
        read_tone(ws)

    assert [r for r in log_of(out) if r["dir"] == "probe" and "error" in r]


def test_a_frame_outside_the_format_is_logged_and_the_call_goes_on(out):
    with connect("610") as ws:
        ws.send_text("MEDIA_START connection_id:abc")
        ws.send_text(MEDIA_START)
        read_tone(ws)

    log = log_of(out)
    assert [r for r in log if r["dir"] == "probe" and "error" in r]
