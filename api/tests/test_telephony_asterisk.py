"""telephony/adapters/asterisk.py: the wire format of the media channel (T-10.1).

The frames in `fixtures/asterisk_media_*.jsonl` were recorded by the probe from
Asterisk 22.10.1 on the workbench (invented caller numbers, test tones only).
"""

import json
from pathlib import Path

import pytest

from api.telephony.adapters.asterisk import (
    DTMF_END,
    MEDIA_START,
    ProtocolError,
    command,
    parse_event,
    sample_rate,
)

FIXTURES = Path(__file__).parent / "fixtures"


def recorded_events(name: str) -> list[str]:
    """The TEXT frames Asterisk sent in a recorded call, in order."""
    lines = (FIXTURES / name).read_text(encoding="utf-8").splitlines()
    records = [json.loads(line) for line in lines]
    return [r["text"] for r in records if r.get("dir") == "in" and "text" in r]


def test_a_recorded_call_starts_with_media_start():
    first = parse_event(recorded_events("asterisk_media_two_way.jsonl")[0])

    assert first.name == MEDIA_START
    assert first.data["format"] == "slin16"
    assert first.data["optimal_frame_size"] == 640
    assert first.data["ptime"] == 20
    assert first.data["channel_id"]


def test_a_recorded_key_press_is_a_dtmf_end_with_its_digit():
    events = [parse_event(t) for t in recorded_events("asterisk_media_two_way.jsonl")]

    digits = [e.data["digit"] for e in events if e.name == DTMF_END]

    assert digits == ["1"]


def test_every_recorded_frame_parses():
    for fixture in sorted(FIXTURES.glob("asterisk_media_*.jsonl")):
        for text in recorded_events(fixture.name):
            assert parse_event(text).name


def test_an_event_this_code_does_not_know_still_parses():
    event = parse_event('{"event": "SOMETHING_NEW", "channel_id": "x"}')

    assert event.name == "SOMETHING_NEW"
    assert event.data == {"channel_id": "x"}


@pytest.mark.parametrize(
    "text",
    [
        "MEDIA_START connection_id:abc format:ulaw",  # the legacy plain-text format
        "[1, 2]",
        '{"channel_id": "x"}',
        '{"event": 5}',
        "",
    ],
)
def test_a_frame_outside_the_format_is_a_protocol_error(text):
    with pytest.raises(ProtocolError):
        parse_event(text)


def test_a_command_is_one_json_object():
    assert json.loads(command("FLUSH_MEDIA")) == {"command": "FLUSH_MEDIA"}
    assert json.loads(command("MARK_MEDIA", correlation_id="s1")) == {
        "command": "MARK_MEDIA",
        "correlation_id": "s1",
    }


def test_an_unknown_command_is_refused_before_it_reaches_the_wire():
    with pytest.raises(ValueError):
        command("flush_media")


def test_sample_rate_of_the_formats_we_dial():
    assert sample_rate("slin16") == 16000
    assert sample_rate("slin") == 8000
    with pytest.raises(ProtocolError):
        sample_rate("opus")
