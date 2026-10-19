"""telephony/adapters/asterisk.py: the wire format of the media channel (T-10.1)
and the line on it (T-1.11).

The frames in `fixtures/asterisk_media_*.jsonl` were recorded on the workbench
(invented caller numbers, test tones only): `_two_way`, `_stop` and `_hangup` by
the probe of T-10.1 from Asterisk 22.10.1, the others by a capture endpoint from
Asterisk 22.11.0 (see below).
"""

import json
from pathlib import Path

import pytest

from api.telephony.adapters.asterisk import (
    DTMF_END,
    IN_FLIGHT_CHUNKS,
    MAX_MESSAGE_BYTES,
    MEDIA_MARK_PROCESSED,
    MEDIA_START,
    MediaLine,
    ProtocolError,
    call_of,
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


# -- the media line (T-1.11) ---------------------------------------------------------
#
# `asterisk_media_marks`, `_flush`, `_overflow`, `_unanswered` and `_closed` were
# recorded on 14.10.2026 from Asterisk 22.11.0 dialled as the product dials:
# `n` (no answer until ANSWER), no URI parameters, the call named by inherited
# variables. A capture endpoint sent marks with its own correlation ids; 22.10.1
# behaved the same on every point. The tests take Asterisk's frames as recorded
# and put our ids into them.


def recorded(name: str) -> list[dict]:
    lines = (FIXTURES / name).read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines]


def recorded_event(name: str, event: str, **changes: object) -> str:
    """The first TEXT frame of `event` Asterisk sent in a recorded call."""
    for text in recorded_events(name):
        frame = json.loads(text)
        if frame["event"] == event:
            return json.dumps({**frame, **changes})
    raise AssertionError(f"{event} not in {name}")


class RecordingSession:
    """What the line hands to the call session."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def start(self) -> None:
        self.calls.append(("start",))

    def audio(self, frame: bytes) -> None:
        self.calls.append(("audio", len(frame)))

    def key(self, digit: str) -> None:
        self.calls.append(("key", digit))

    def played(self, mark: int) -> None:
        self.calls.append(("played", mark))

    def closed(self) -> None:
        self.calls.append(("closed",))


class Wire:
    """The connection: what the line sent, and what it decided."""

    def __init__(self) -> None:
        self.frames: list[str | bytes] = []
        self.decisions: list[str] = []
        self.line = MediaLine(self.frames.append, ended=self.decided)
        self.session = RecordingSession()
        self.line.attach(self.session)  # type: ignore[arg-type]

    def decided(self, decision: str) -> None:
        # What was on the wire when the decision was made.
        self.decisions.append(f"{decision} after {len(self.frames)} frames")

    def started(self, name: str = "asterisk_media_marks.jsonl") -> "Wire":
        self.line.on_text(recorded_event(name, MEDIA_START))
        return self

    def commands(self) -> list[dict]:
        return [json.loads(f) for f in self.frames if isinstance(f, str)]

    def audio_bytes(self) -> int:
        return sum(len(f) for f in self.frames if isinstance(f, bytes))

    def processed(self, correlation_id: str) -> None:
        self.line.on_text(
            recorded_event(
                "asterisk_media_marks.jsonl",
                MEDIA_MARK_PROCESSED,
                correlation_id=correlation_id,
            )
        )

    def chunk_marks(self) -> list[str]:
        return [
            c["correlation_id"]
            for c in self.commands()
            if c["command"] == "MARK_MEDIA" and c["correlation_id"].startswith("c")
        ]


def tone_seconds(seconds: float) -> bytes:
    return b"\x01\x00" * int(16000 * seconds)


def test_media_start_starts_the_session_with_the_recorded_format():
    wire = Wire().started()

    assert wire.session.calls == [("start",)]


def test_the_recorded_call_was_dialled_without_answer_and_with_basic_credentials():
    """What router and dialplan rely on: no query in the URL, the call named by
    inherited variables in MEDIA_START, the subprotocol, and `username` and
    `password` of websocket_client.conf as Basic credentials."""
    first = recorded("asterisk_media_unanswered.jsonl")[0]

    assert first["query"] == ""
    assert first["subprotocols"] == ["media"]
    assert first["authorization"] == "Basic"
    start = parse_event(recorded_events("asterisk_media_unanswered.jsonl")[0])
    assert start.data["channel_variables"]["DIALEDPEERNUMBER"].endswith(
        "/nc(slin16)f(json)"
    )
    name = call_of(start)
    assert name.call_id == start.data["channel_variables"]["__VOICE_CALL"]
    assert name.caller_id == "+4972215550703"


@pytest.mark.parametrize(
    "variables",
    [
        None,
        {},
        {"__VOICE_CALLER": "+4972215550701"},
        {"__VOICE_CALL": ""},
        {"__VOICE_CALL": "abc"},
        {"__VOICE_CALL": "1.2;Dial(x)"},
        {"__VOICE_CALL": 5},
    ],
)
def test_a_media_start_without_a_call_id_names_no_call(variables):
    frame = json.loads(recorded_event("asterisk_media_marks.jsonl", MEDIA_START))
    frame["channel_variables"] = variables
    with pytest.raises(ProtocolError):
        call_of(parse_event(json.dumps(frame)))


def test_a_withheld_caller_is_no_caller_id():
    frame = json.loads(recorded_event("asterisk_media_marks.jsonl", MEDIA_START))
    frame["channel_variables"]["__VOICE_CALLER"] = ""
    assert call_of(parse_event(json.dumps(frame))).caller_id is None


def test_a_format_we_do_not_dial_ends_the_call():
    wire = Wire()
    with pytest.raises(ProtocolError):
        wire.line.on_text(
            recorded_event("asterisk_media_marks.jsonl", MEDIA_START, format="slin")
        )
    assert wire.session.calls == []


def test_answer_is_its_own_command():
    wire = Wire().started()
    wire.line.answer()

    assert wire.commands() == [{"command": "ANSWER"}]


def test_a_sentence_goes_out_in_whole_frames_with_its_mark_behind_it():
    wire = Wire().started()
    wire.line.play(b"\x01\x02\x03", mark=1)

    assert wire.frames[0] == b"\x01\x02\x03" + bytes(637)  # one frame of 640
    assert [c["correlation_id"] for c in wire.commands()] == ["c1", "s1"]


def test_the_mark_of_a_sentence_reports_it_played():
    wire = Wire().started()
    wire.line.play(tone_seconds(1), mark=1)
    wire.processed("c1")
    wire.processed("s1")

    assert wire.session.calls[-1] == ("played", 1)


def test_no_message_is_larger_than_asterisk_takes_and_each_is_whole_frames():
    wire = Wire().started()
    wire.line.play(tone_seconds(5), mark=1)
    for _ in range(10):
        if wire.chunk_marks():
            wire.processed(wire.chunk_marks()[-1])

    sizes = [len(f) for f in wire.frames if isinstance(f, bytes)]
    assert max(sizes) <= MAX_MESSAGE_BYTES
    assert all(size % 640 == 0 for size in sizes)
    assert wire.audio_bytes() == 5 * 32000


def test_a_long_read_back_never_overflows_the_queue_asterisk_keeps():
    """Recorded: 25 s sent in one go filled the queue (1020 frames), the rest was
    dropped, and the mark behind it was reported at 20.5 s - a read-back would
    count as heard while its end was never played (rule 3). The line keeps no
    more on the wire than Asterisk reports as its limit, and sends every byte."""
    status = json.loads(recorded_event("asterisk_media_overflow.jsonl", "STATUS"))
    assert status["queue_full"] is True
    limit_bytes = status["xoff_level"] * 640

    wire = Wire().started("asterisk_media_overflow.jsonl")
    wire.line.play(tone_seconds(25), mark=1)
    assert wire.audio_bytes() <= limit_bytes
    assert {"command": "MARK_MEDIA", "correlation_id": "s1"} not in wire.commands()

    acknowledged = 0
    while acknowledged < len(wire.chunk_marks()):
        wire.processed(wire.chunk_marks()[acknowledged])
        acknowledged += 1
        played = acknowledged * (MAX_MESSAGE_BYTES // 640 * 640)
        assert wire.audio_bytes() - played <= limit_bytes

    assert wire.audio_bytes() == 25 * 32000
    assert wire.commands()[-1] == {"command": "MARK_MEDIA", "correlation_id": "s1"}


def test_a_flush_drops_what_waits_and_its_mark_never_reaches_the_wire():
    """Recorded: marks queued before FLUSH_MEDIA are never reported."""
    flush = recorded("asterisk_media_flush.jsonl")
    marks_out = [
        json.loads(r["text"])["correlation_id"]
        for r in flush
        if r.get("dir") == "out" and "MARK_MEDIA" in r.get("text", "")
    ]
    marks_in = [
        json.loads(r["text"])["correlation_id"]
        for r in flush
        if r.get("dir") == "in" and MEDIA_MARK_PROCESSED in r.get("text", "")
    ]
    assert marks_out == ["m1", "m2", "m3"] and marks_in == ["m3"]

    wire = Wire().started("asterisk_media_flush.jsonl")
    wire.line.play(tone_seconds(25), mark=1)
    wire.line.flush()
    assert wire.commands()[-1] == {"command": "FLUSH_MEDIA"}

    # The next sentence goes out at once, as far as there is room: nothing
    # from before the flush counts as unplayed or waits any more.
    sent = len(wire.frames)
    wire.line.play(b"\x02\x00" * 16000 * 25, mark=2)
    after_flush = [f for f in wire.frames[sent:] if isinstance(f, bytes)]
    assert len(after_flush) == IN_FLIGHT_CHUNKS
    assert all(set(f) == {2, 0} for f in after_flush)
    # A mark from before the flush frees no room, and the mark of the dropped
    # sentence never went out.
    sent = len(wire.frames)
    wire.processed("c1")
    assert len(wire.frames) == sent
    assert {"command": "MARK_MEDIA", "correlation_id": "s1"} not in wire.commands()


def test_hangup_is_decided_before_it_goes_out():
    wire = Wire().started()
    wire.line.hangup()

    assert wire.decisions == ["hangup after 0 frames"]
    assert wire.commands() == [{"command": "HANGUP"}]


def test_a_transfer_is_a_decision_for_the_dialplan_and_ends_our_leg():
    wire = Wire().started()
    wire.line.transfer("+4972215550900")

    assert wire.decisions == ["transfer +4972215550900 after 0 frames"]
    assert wire.commands() == [{"command": "HANGUP"}]


@pytest.mark.parametrize("end", ["hangup", "transfer"])
def test_an_ending_that_was_recorded_stands_when_hangup_cannot_go_out(end):
    """The dialplan acts on the recorded ending once the connection closes. A
    HANGUP the stopping server can no longer send does not turn a call that
    went as it should into a failure (ultra review and Codex on PR #310)."""
    wire = Wire().started()

    def stopped(frame):
        raise RuntimeError("Event loop is closed")

    wire.line._send = stopped  # type: ignore[method-assign]
    if end == "hangup":
        wire.line.hangup()
    else:
        wire.line.transfer("+4972215550900")

    assert len(wire.decisions) == 1 and wire.decisions[0].startswith(end)


@pytest.mark.parametrize("target", ["", "team", "+49 7221 555", "1;Dial(PJSIP/x)"])
def test_a_transfer_to_something_that_is_not_a_number_is_refused(target):
    wire = Wire().started()
    with pytest.raises(ValueError):
        wire.line.transfer(target)
    assert wire.decisions == [] and wire.frames == []


def test_keys_audio_and_the_end_of_the_connection_reach_the_session():
    events = recorded_events("asterisk_media_two_way.jsonl")
    wire = Wire().started("asterisk_media_two_way.jsonl")
    wire.line.on_text(events[1])  # DTMF_END "1"
    wire.line.on_audio(bytes(640))
    wire.line.on_text(events[2])  # QUEUE_DRAINED: nothing for the session
    wire.line.closed()

    assert wire.session.calls == [
        ("start",),
        ("key", "1"),
        ("audio", 640),
        ("closed",),
    ]


def test_audio_before_media_start_is_refused():
    wire = Wire()
    with pytest.raises(ProtocolError):
        wire.line.play(b"\x00\x00", mark=1)
