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
    return {"log": log, "heard": heard, "seconds": seconds}


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


def stopped(flush_at: float, heard_seconds: float, status: bool = True) -> dict:
    events = [
        (flush_at, "in", {"event": "DTMF_END", "digit": "5"}),
        (flush_at, "out", {"command": "FLUSH_MEDIA"}),
    ]
    if status:
        events.append((flush_at, "in", {"event": "STATUS", "queue_length": 0}))
    return {
        "610": call(
            "610",
            digits="5",
            events=events,
            heard=tone(440, heard_seconds, LINE) + bytes(LINE * 4),
        )
    }


def test_a_flush_that_ends_the_tone_passes():
    assert failed(spike.judge_stop(stopped(flush_at=2.17, heard_seconds=2.18))) == []


def test_a_tone_that_ended_before_the_flush_was_not_stopped_by_it():
    assert failed(spike.judge_stop(stopped(flush_at=2.17, heard_seconds=1.7))) == [
        "610: a key stops the playback"
    ]


def test_a_tone_that_started_late_and_ran_past_the_flush_fails():
    calls = stopped(flush_at=2.17, heard_seconds=2.18)
    calls["610"]["heard"] = bytes(LINE * 2) + calls["610"]["heard"]

    assert failed(spike.judge_stop(calls)) == ["610: a key stops the playback"]


def test_a_tone_that_plays_on_after_the_flush_fails():
    assert failed(spike.judge_stop(stopped(flush_at=2.17, heard_seconds=4.0))) == [
        "610: a key stops the playback"
    ]


def test_without_a_flush_nothing_was_stopped():
    calls = {"610": call("610", digits="", events=[])}

    assert failed(spike.judge_stop(calls)) == ["610: a key stops the playback"]


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
