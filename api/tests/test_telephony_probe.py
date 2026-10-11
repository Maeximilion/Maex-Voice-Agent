"""telephony/probe.py: the measuring endpoint of the spike T-10.1, without a PBX.

The test plays the media channel's side of a call over a WebSocket and checks
what the probe sends back and what it writes into its log.
"""

import json

import pytest
from fastapi.testclient import TestClient

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
    (path,) = out.glob("*.jsonl")
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


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


def test_an_extension_without_a_plan_gets_silence_and_a_log_line(out):
    with connect("699") as ws:
        ws.send_text(MEDIA_START)
        ws.send_text('{"event": "QUEUE_DRAINED", "channel_id": "pbx-1.1"}')

    log = log_of(out)
    assert not [r for r in log if r["dir"] == "out"]
    assert log[-1]["heard_hz"] is None


def test_a_frame_outside_the_format_is_logged_and_the_call_goes_on(out):
    with connect("610") as ws:
        ws.send_text("MEDIA_START connection_id:abc")
        ws.send_text(MEDIA_START)
        read_tone(ws)

    log = log_of(out)
    assert [r for r in log if r["dir"] == "probe" and "error" in r]
