"""telephony/spike.py: how the kit of the spike T-10.1 judges a call, without Docker.

The judges get what a run would hand them: the probe's log of a call and what the
softphone recorded. Both are built here, so every pass and every fail is a case.
"""

import json

import pytest

from api.telephony import spike
from api.telephony.audio import tone
from api.telephony.probe import CALLER_TONES, PLANS

LINE = spike.LINE_RATE


def call(
    exten: str,
    *,
    heard: bytes | None = None,
    sent: bytes = b"",
    first_audio_t: float | None = 0.05,
    digits: str | None = None,
    tones_in: dict[str, float] | None = None,
    mono: float = 100.0,
    lasted: float = 5.4,
    events: list[tuple[float, str, dict]] | None = None,
    media_start: str | None = None,
    seconds: float = 5.9,
) -> dict:
    """One finished call as `place()` returns it. `events` are control frames as
    (time, direction, frame) after the tone was sent at 0.01 s."""
    plan = PLANS[exten]
    if events is None:
        events = [(0.01 + plan.seconds, "in", {"event": "QUEUE_DRAINED"})]
    if tones_in is None:
        tones_in = {str(hz): 0.0 for hz in CALLER_TONES.values()}
        if exten in CALLER_TONES:
            tones_in[str(CALLER_TONES[exten])] = 2.98
    log = [
        {
            "t": 0.0,
            "dir": "probe",
            "exten": exten,
            "caller": f"+4972215550{exten}",
            "mono": mono,
        },
        {
            "t": 0.0,
            "dir": "in",
            "text": media_start
            or json.dumps(
                {"event": "MEDIA_START", "format": "slin16", "optimal_frame_size": 640}
            ),
        },
        {"t": 0.01, "dir": "out", "bytes": 64000},
        *(
            {"t": t, "dir": direction, "text": json.dumps(frame)}
            for t, direction, frame in events
        ),
        {
            "t": lasted,
            "dir": "probe",
            "closed": True,
            "heard_hz": CALLER_TONES.get(exten),
            "first_audio_t": first_audio_t,
            "tones_in": tones_in,
            "audio_in_seconds": 2.997,
            "frame_bytes": {"640": 149, "552": 1},
            "frame_interval_ms": 19.96,
            "frame_gap_max_ms": 21.0,
            "digits": exten[-1] if digits is None else digits,
        },
    ]
    if heard is None:
        heard = tone(plan.tone_hz, plan.seconds, LINE)
    return {"log": log, "heard": heard, "sent": sent, "seconds": seconds}


def failed(checks: list[spike.Check]) -> list[str]:
    return [name for name, passed, _ in checks if not passed]


def test_a_good_call_passes_every_point():
    checks = spike.judge_two_way({"601": call("601")})

    assert len(checks) == 6
    assert failed(checks) == []


def test_a_dropout_in_the_tone_fails_audio_to_the_caller():
    heard = tone(440, 0.4, LINE) + bytes(int(3.2 * LINE) * 2) + tone(440, 0.4, LINE)

    assert failed(spike.judge_two_way({"601": call("601", heard=heard)})) == [
        "601: audio to the caller"
    ]


def test_another_calls_tone_in_the_recording_is_cross_talk():
    heard = tone(440, 4, LINE) + tone(554, 1, LINE)

    assert failed(spike.judge_two_way({"601": call("601", heard=heard)})) == [
        "601: audio to the caller"
    ]


def test_another_callers_tone_at_the_probe_is_cross_talk():
    tones_in = {"1000": 2.98, "1200": 0.6, "1400": 0.0}

    assert failed(spike.judge_two_way({"601": call("601", tones_in=tones_in)})) == [
        "601: audio from the caller"
    ]


def test_a_caller_tone_that_mostly_got_lost_fails_audio_from_the_caller():
    tones_in = {"1000": 0.1, "1200": 0.0, "1400": 0.0}

    assert failed(spike.judge_two_way({"601": call("601", tones_in=tones_in)})) == [
        "601: audio from the caller"
    ]


def test_a_wrong_key_fails_the_key_event():
    assert failed(spike.judge_two_way({"601": call("601", digits="7")})) == [
        "601: key event"
    ]


def test_control_frames_that_are_not_json_fail_the_point_instead_of_crashing():
    legacy = "MEDIA_START connection_id:voice format:slin16 optimal_frame_size:640"

    checks = spike.judge_two_way({"601": call("601", media_start=legacy)})

    assert "601: media channel connects" in failed(checks)


def test_calls_that_overlap_on_the_probes_clock_are_at_once():
    calls = {
        e: call(e, mono=100.0 + i * 0.1) for i, e in enumerate(("601", "602", "603"))
    }

    checks = spike.judge_two_way(calls)

    assert failed(checks) == []
    assert checks[-1][0] == "3 calls at once"


def test_calls_one_after_the_other_are_not_at_once():
    calls = {
        e: call(e, mono=100.0 + i * 6) for i, e in enumerate(("601", "602", "603"))
    }

    assert failed(spike.judge_two_way(calls)) == ["3 calls at once"]


@pytest.mark.parametrize(
    ("frame_bytes", "interval", "gap", "steady"),
    [
        ({"640": 149, "552": 1}, 19.96, 24.0, True),
        ({"640": 150}, 19.96, 20.2, True),
        ({"64000": 2}, 1500.0, 1500.0, False),  # two bursts
        ({"640": 100, "320": 50}, 20.0, 20.0, False),  # many short frames
        ({"640": 150}, 40.0, 41.0, False),  # half the rate
        ({"640": 150}, 20.0, 400.0, False),  # one long stall
        ({}, None, None, False),  # no audio at all
    ],
)
def test_steady_frames(frame_bytes, interval, gap, steady):
    summary = {
        "frame_bytes": frame_bytes,
        "frame_interval_ms": interval,
        "frame_gap_max_ms": gap,
    }

    assert spike.steady_frames(summary) is steady


# In these calls the caller's tone starts with the recording and reaches the probe
# 0.05 s after the socket opened: the recording's clock is the probe's minus 0.05.
MARK_AT_PROBE = 0.05
STOP_FAILS = ["610: a key stops the playback"]


def stopped(
    flush_at: float,
    heard_seconds: float,
    *,
    recorder_early: float = 0.0,
    tone_late: float = 0.0,
    mark: bool = True,
) -> dict:
    """A call to 610: we flush at `flush_at` on the probe's clock, the caller heard
    our tone for `heard_seconds`. `recorder_early` puts silence in front of both
    recordings, `tone_late` only in front of what the caller heard."""
    events = [
        (flush_at, "in", {"event": "DTMF_END", "digit": "5"}),
        (flush_at, "out", {"command": "FLUSH_MEDIA"}),
        (flush_at, "in", {"event": "STATUS", "queue_length": 0}),
    ]
    lead = bytes(int(recorder_early * LINE) * 2)
    heard = lead + bytes(int(tone_late * LINE) * 2) + tone(440, heard_seconds, LINE)
    sent = lead + tone(CALLER_TONES["610"], 3, LINE) if mark else lead
    return {
        "610": call(
            "610",
            digits="5",
            events=events,
            heard=heard + bytes(LINE * 4),
            sent=sent + bytes(LINE * 4),
            first_audio_t=MARK_AT_PROBE if mark else None,
        )
    }


def test_a_flush_that_ends_the_tone_passes():
    assert failed(spike.judge_stop(stopped(flush_at=3.25, heard_seconds=3.21))) == []


def test_a_recorder_that_started_before_the_audio_changes_nothing():
    calls = stopped(flush_at=3.25, heard_seconds=3.21, recorder_early=0.5)

    assert failed(spike.judge_stop(calls)) == []


def test_a_tone_that_ended_before_the_flush_was_not_stopped_by_it():
    calls = stopped(flush_at=3.25, heard_seconds=2.7)

    assert failed(spike.judge_stop(calls)) == STOP_FAILS


def test_a_tone_that_started_late_and_ran_past_the_flush_fails():
    calls = stopped(flush_at=3.25, heard_seconds=3.21, tone_late=1.0)

    assert failed(spike.judge_stop(calls)) == STOP_FAILS


def test_a_tone_that_plays_on_after_the_flush_fails():
    assert (
        failed(spike.judge_stop(stopped(flush_at=3.25, heard_seconds=5.0)))
        == STOP_FAILS
    )


def test_without_the_callers_tone_there_is_no_clock_to_judge_the_stop_by():
    calls = stopped(flush_at=3.25, heard_seconds=3.21, mark=False)

    assert failed(spike.judge_stop(calls)) == STOP_FAILS


def test_without_a_flush_nothing_was_stopped():
    calls = {"610": call("610", digits="", events=[])}

    assert failed(spike.judge_stop(calls)) == STOP_FAILS


def hung_up(heard_seconds: float, seconds: float = 1.4) -> dict:
    events = [
        (1.01, "in", {"event": "QUEUE_DRAINED"}),
        (1.01, "out", {"command": "HANGUP"}),
    ]
    return {
        "611": call(
            "611", events=events, heard=tone(440, heard_seconds, LINE), seconds=seconds
        )
    }


def test_a_hangup_after_the_whole_tone_passes():
    assert failed(spike.judge_hangup(hung_up(1.0))) == []


def test_a_hangup_that_cut_the_tone_fails():
    assert failed(spike.judge_hangup(hung_up(0.5))) == [
        "611: we hang up after everything was spoken"
    ]


def test_a_call_the_caller_had_to_end_fails():
    assert failed(spike.judge_hangup(hung_up(1.0, seconds=20.5))) == [
        "611: we hang up after everything was spoken"
    ]


class FakeStack:
    """Stands in for the containers: a call is over the moment it is placed."""

    def __init__(self, run_dir, clock, steps: int) -> None:
        self.run_dir = run_dir
        self.clock = clock
        self.steps = steps  # how many calls the wall clock is set during
        self.placed = 0
        for folder in ("rec", "probe"):
            (run_dir / folder).mkdir(parents=True)

    def softphone(self, cli_command: str) -> str:
        if not cli_command.startswith("channel originate"):
            return "0 active channels\n"
        self.placed += 1
        if self.placed <= self.steps:
            self.clock.offset += 3.3
        closing = {"t": 5.0, "dir": "probe", "closed": True}
        (self.run_dir / "probe" / "601-abc.jsonl").write_text(
            json.dumps(closing) + "\n"
        )
        return ""


class Clock:
    """The time module as the kit sees it, with a wall clock that can be set."""

    def __init__(self) -> None:
        self.offset = 0.0
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    def time(self) -> float:
        return self.now + self.offset

    def sleep(self, seconds: float) -> None:
        self.now += seconds


@pytest.mark.parametrize(
    ("steps", "placed"),
    [
        (0, 1),  # a steady clock: one call
        (1, 2),  # set during the first call: placed again
    ],
)
def test_a_call_during_which_the_wall_clock_was_set_is_placed_again(
    tmp_path, monkeypatch, steps, placed
):
    clock = Clock()
    monkeypatch.setattr(spike, "time", clock)
    stack = FakeStack(tmp_path, clock, steps)

    calls = spike.place(stack, ("601",))

    assert stack.placed == placed
    assert calls["601"]["log"][-1]["closed"]


def test_a_clock_that_is_set_during_every_attempt_breaks_the_run(tmp_path, monkeypatch):
    clock = Clock()
    monkeypatch.setattr(spike, "time", clock)
    stack = FakeStack(tmp_path, clock, steps=99)

    with pytest.raises(spike.SpikeError, match="wall clock"):
        spike.place(stack, ("601",))

    assert stack.placed == 1 + spike.CLOCK_STEP_RETRIES
    assert not (tmp_path / "calls").exists()


def test_a_recording_that_is_missing_or_half_written_reads_as_empty(tmp_path):
    broken = tmp_path / "half.wav"
    broken.write_bytes(b"RIFF\x00\x00")

    assert spike.read_wav(tmp_path / "missing.wav") == b""
    assert spike.read_wav(broken) == b""


def test_each_run_directory_is_its_own_compose_project(tmp_path):
    one = spike.Stack(tmp_path / "a", dry_run=True)
    other = spike.Stack(tmp_path / "b", dry_run=True)

    assert one.project != other.project
    assert one.project == spike.Stack(tmp_path / "a", dry_run=True).project


def test_root_is_refused_before_anything_is_touched(monkeypatch, capsys):
    monkeypatch.setattr(spike.os, "getuid", lambda: 0)

    assert spike.main(["--dry-run"]) == 2
    assert "unprivileged" in capsys.readouterr().err


def test_the_config_folder_is_refused_as_run_directory(monkeypatch, capsys):
    monkeypatch.setattr(spike.os, "getuid", lambda: 1000)

    assert spike.main(["--run-dir", str(spike.CONFIG_DIR)]) == 2
    assert "not a run directory" in capsys.readouterr().err
    assert (spike.CONFIG_DIR / "pbx" / "pjsip.conf").exists()


def test_a_folder_with_somebodys_files_is_refused_and_left_alone(tmp_path, monkeypatch):
    monkeypatch.setattr(spike.os, "getuid", lambda: 1000)
    (tmp_path / "rec").mkdir()
    (tmp_path / "rec" / "holiday.wav").write_bytes(b"mine")

    assert spike.main(["--run-dir", str(tmp_path)]) == 2
    assert (tmp_path / "rec" / "holiday.wav").read_bytes() == b"mine"


def test_only_a_new_an_empty_or_an_own_folder_is_a_run_directory(tmp_path):
    own = tmp_path / "own"
    own.mkdir()
    (own / spike.RUN_MARKER).touch()
    (own / "rec").mkdir()
    a_file = tmp_path / "file"
    a_file.write_text("x")
    empty = tmp_path / "empty"
    empty.mkdir()

    assert spike.usable_run_dir(tmp_path / "new")
    assert spike.usable_run_dir(empty)
    assert spike.usable_run_dir(own)
    assert not spike.usable_run_dir(a_file)
    assert not spike.usable_run_dir(tmp_path)


def test_the_results_of_an_earlier_run_are_removed(tmp_path):
    run = tmp_path / "run"
    spike.render(run)
    (run / "report.json").write_text("[]")
    (run / "pbx.log").write_text("old")
    (run / "calls" / "601").mkdir(parents=True)

    spike.clear_results(run)

    assert not (run / "report.json").exists()
    assert not (run / "pbx.log").exists()
    assert not (run / "calls").exists()
    assert (run / "pbx" / "pjsip.conf").exists()


def test_clearing_results_needs_no_run_directory(tmp_path):
    spike.clear_results(tmp_path / "never-made")


def test_a_run_that_breaks_at_its_first_step_leaves_no_old_report(
    tmp_path, monkeypatch
):
    run = tmp_path / "run"
    spike.render(run)
    (run / "report.json").write_text("[]")
    monkeypatch.setattr(spike.os, "getuid", lambda: 1000)
    monkeypatch.setattr(spike, "docker_problem", lambda: None)

    def broken(self, *args):
        raise spike.SpikeError("the daemon is gone")

    monkeypatch.setattr(spike.Stack, "compose", broken)

    assert spike.main(["--run-dir", str(run)]) == 1
    assert not (run / "report.json").exists()


def test_render_marks_its_run_directory(tmp_path):
    spike.render(tmp_path / "run")

    assert (tmp_path / "run" / spike.RUN_MARKER).exists()
    assert spike.usable_run_dir(tmp_path / "run")
