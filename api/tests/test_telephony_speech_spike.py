"""telephony/speech_spike.py: plan, summary and pick of the spike T-10.3, without Docker.

The summary gets what a run would hand it: the bench's `heard.json` and the
judged rows. Both are built here.
"""

import csv
import json
from pathlib import Path

import pytest

from api.telephony import speech_spike as kit
from evals.number_eval import load


def judged_rows(ear: str, verdicts: str, *, seconds: float = 0.2, voice: str = "a"):
    """One number row per letter: r right, a asked, w wrong."""
    names = {"r": "right", "a": "asked", "w": "wrong"}
    return [
        {
            "id": f"n{i}",
            "voice": voice,
            "ear": ear,
            "kind": "number",
            "say": "Nummer 23",
            "text": "Nummer 24",
            "expect": "23",
            "got": "24",
            "verdict": names[letter],
            "on_card": False,
            "seconds": seconds,
        }
        for i, letter in enumerate(verdicts)
    ]


def run(ears: dict, judged: list, voices: tuple[str, ...] = ("a",)):
    plan = {
        "sentences": [{}] * 3,
        "all_sentences": 3,
        "voices": list(voices),
        "ears": [{"id": ear} for ear in ears],
        "silences_ms": [500],
    }
    heard = {
        "ears": ears,
        "mouths": [
            {
                "voice": v,
                "first_audio_seconds": 0.1,
                "seconds": 0.5,
                "audio_seconds": 5.0,
            }
            for v in voices
        ],
        "turn_end": [
            {"id": "n0", "voice": "a", "silence_ms": 500, "parts": parts}
            for parts in (1, 1, 2, 0)
        ],
    }
    return kit.summarize(plan, heard, judged)


def test_the_plan_takes_its_sentences_from_the_eval_files():
    plan = kit.build_plan(None)
    numbers = [s for s in plan["sentences"] if s["kind"] == "number"]
    dishes = [s for s in plan["sentences"] if s["kind"] == "dish"]
    assert [(s["say"], s["expect"]) for s in numbers] == [
        (c.say, c.expect) for c in load()
    ]
    with kit.MENU.open(encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f, delimiter=";"))
    active = [r["name"] for r in rows if r["active"] != "nein"]
    assert 0 < len(dishes) == len(active) < len(rows)
    assert plan["key_terms"] == [s["expect"] for s in dishes]
    assert plan["all_sentences"] == len(plan["sentences"])
    # Nobody says "(6 Stück)" on the phone.
    assert not any("(" in s["say"] for s in dishes)
    assert len({s["id"] for s in plan["sentences"]}) == len(plan["sentences"])


def test_a_limit_shortens_both_lists_and_shows_as_a_trial(capsys, tmp_path):
    plan = kit.build_plan(2)
    assert [s["kind"] for s in plan["sentences"]] == ["number"] * 2 + ["dish"] * 2
    assert plan["all_sentences"] > 4
    report = run({"e": {"load_seconds": 1}}, judged_rows("e", "rr"))
    report["all_sentences"] = 10
    kit.print_report(report, tmp_path)
    assert "TRIAL: 3 of 10" in capsys.readouterr().out


def test_the_ear_that_understands_most_is_picked():
    judged = judged_rows("good", "rrrr") + judged_rows("fair", "rrra")
    report = run({"fair": {"load_seconds": 1}, "good": {"load_seconds": 1}}, judged)
    assert report["ear"] == "good" and report["mouth"] == "a" and report["go"]
    good = next(e for e in report["ears"] if e["id"] == "good")
    assert good["right_share"] == 1.0 and good["median_seconds"] == 0.2
    assert report["ear_and_mouth_seconds"] == pytest.approx(0.3)
    assert report["left_for_silence_and_model_seconds"] == pytest.approx(1.2)


@pytest.mark.parametrize(
    ("expect", "kind"),
    [
        ("23", "plain number"),
        ("07", "plain number"),
        ("s12", "number with a letter"),
        ("25g", "number with a letter"),
        ("s12|sm12", "number with a letter"),
        ("?", "no card number"),
        ("name", "no card number"),
        ("!23h", "no card number"),
    ],
)
def test_number_kind(expect, kind):
    assert kit.number_kind(expect) == kind


def test_the_verdicts_are_also_counted_per_kind_of_sentence(capsys, tmp_path):
    judged = judged_rows("e", "r" * 99)
    lettered = judged_rows("e", "w")[0] | {"id": "n99", "expect": "s12"}
    report = run({"e": {"load_seconds": 1}}, [*judged, lettered])
    by_kind = report["ears"][0]["by_kind"]
    assert by_kind["plain number"] == {
        "sentences": 99,
        "right": 1.0,
        "asked": 0.0,
        "wrong": 0.0,
    }
    assert by_kind["number with a letter"]["wrong"] == 1.0
    assert report["ears"][0]["wrong_on_card_share"] == 0.0
    assert "no card number" not in by_kind
    kit.print_report(report, tmp_path)
    assert "number with a letter      1 heard" in capsys.readouterr().out


def test_among_equal_ears_the_faster_one_is_picked():
    judged = judged_rows("slow", "rr", seconds=3.0) + judged_rows("fast", "rr")
    report = run({"slow": {"load_seconds": 1}, "fast": {"load_seconds": 1}}, judged)
    assert report["ear"] == "fast"


def test_an_ear_that_only_heard_a_sample_is_timed_and_never_picked():
    plan = kit.build_plan(1)
    assert [e["id"] for e in plan["ears"] if "sample" in e] == ["whisper-turbo-cpu"]
    judged = judged_rows("whisper-turbo-cpu", "rr") + judged_rows(
        "parakeet-v3-cpu", "ra"
    )
    heard = {
        "ears": {
            "whisper-turbo-cpu": {"load_seconds": 1},
            "parakeet-v3-cpu": {"load_seconds": 1},
        },
        "mouths": [
            {
                "voice": v,
                "first_audio_seconds": 0.1,
                "seconds": 0.5,
                "audio_seconds": 5.0,
            }
            for v in plan["voices"]
        ],
        "turn_end": [],
    }
    report = kit.summarize(plan, heard, judged)
    assert report["ear"] == "parakeet-v3-cpu"
    timed = next(e for e in report["ears"] if e["id"] == "whisper-turbo-cpu")
    assert timed["utterances"] == 2 and timed["right_share"] == 1.0


def test_an_ear_with_too_many_silent_wrong_numbers_is_never_picked():
    # 1 of 30 is above the 2 % of docs/20 §6, however much else it understood.
    judged = judged_rows("bold", "r" * 29 + "w") + judged_rows("shy", "ra")
    report = run({"bold": {"load_seconds": 1}, "shy": {"load_seconds": 1}}, judged)
    assert report["ear"] == "shy" and report["go"]
    assert report["wrong"] == []


def test_without_an_ear_within_the_limit_the_closest_one_is_shown_as_no_go(
    capsys, tmp_path
):
    judged = judged_rows("bold", "r" * 29 + "w") + judged_rows("wild", "rw")
    report = run({"wild": {"load_seconds": 1}, "bold": {"load_seconds": 1}}, judged)
    assert report["go"] is False
    assert report["ear"] == "bold" and report["mouth"] == "a"
    assert len(report["wrong"]) == 1
    kit.print_report(report, tmp_path)
    out = capsys.readouterr().out
    assert "no-go: no ear with at most 2.0 % silent wrong numbers" in out
    assert "Closest: bold with 3.3 %" in out
    assert "(not on the eval menu)" in out


def test_one_silent_wrong_number_within_the_limit_is_listed_with_the_pick():
    report = run({"e": {"load_seconds": 1}}, judged_rows("e", "r" * 59 + "w"))
    assert report["ear"] == "e"
    assert report["wrong"] == [
        {
            "say": "Nummer 23",
            "voice": "a",
            "text": "Nummer 24",
            "expect": "23",
            "got": "24",
            "on_card": False,
        }
    ]


def test_an_ear_that_failed_or_did_not_run_is_listed_and_not_picked(capsys, tmp_path):
    report = run({"broken": {"error": "RuntimeError: no CUDA"}, "absent": {}}, [])
    assert report["go"] is False
    assert [e["error"] for e in report["ears"]] == [
        "RuntimeError: no CUDA",
        "heard no number sentence",
    ]
    kit.print_report(report, tmp_path)
    out = capsys.readouterr().out
    assert "failed: RuntimeError: no CUDA" in out
    assert "no-go: no ear was measured" in out


def test_the_voice_the_picked_ear_understands_best_is_the_mouth():
    judged = judged_rows("e", "rrrr", voice="clear") + judged_rows(
        "e", "rraa", voice="dull"
    )
    report = run({"e": {"load_seconds": 1}}, judged, voices=("dull", "clear"))
    assert report["mouth"] == "clear"
    dull = next(m for m in report["mouths"] if m["voice"] == "dull")
    assert dull["understood_share"] == 0.5 and dull["times_real_time"] == 10.0


def test_turn_end_shares_per_silence():
    report = run({"e": {"load_seconds": 1}}, judged_rows("e", "r"))
    assert report["turn_end"] == [
        {
            "silence_ms": 500,
            "one_turn_share": 0.5,
            "cut_share": 0.25,
            "no_speech_share": 0.25,
        }
    ]


def fake_containers(monkeypatch, verdicts: str):
    """`main` with the containers replaced: the bench writes what an ear heard,
    the judge is the real one."""
    from evals import heard as judge_module

    def docker(command, dry_run, cwd=None):
        run_dir = next(
            (Path(arg.split(":")[0]) for arg in command if arg.endswith(":/work")), None
        )
        if "measure" in command:
            plan = json.loads((run_dir / "plan.json").read_text(encoding="utf-8"))
            said = [s for s in plan["sentences"] if s["kind"] == "number"]
            texts = {"r": "Nummer 23", "w": "Nummer 24"}
            result = {
                "ears": {"parakeet-v3-cpu": {"load_seconds": 1.0}},
                "heard": [
                    {
                        "id": sentence["id"],
                        "voice": plan["voices"][0],
                        "ear": "parakeet-v3-cpu",
                        "text": texts[letter],
                        "seconds": 0.2,
                    }
                    for sentence, letter in zip(said, verdicts, strict=True)
                ],
                "mouths": [
                    {
                        "voice": v,
                        "first_audio_seconds": 0.1,
                        "seconds": 0.5,
                        "audio_seconds": 5.0,
                    }
                    for v in plan["voices"]
                ],
                "turn_end": [],
            }
            (run_dir / "heard.json").write_text(json.dumps(result), encoding="utf-8")
        if "evals.heard" in command:
            assert judge_module.main([str(run_dir)]) == 0

    monkeypatch.setattr(kit, "docker", docker)
    monkeypatch.setattr(kit, "docker_problem", lambda: None)
    monkeypatch.setattr(kit, "gpu_in_docker", lambda: False)


def test_a_run_writes_its_report_and_exits_with_the_pick(tmp_path, monkeypatch, capsys):
    # The first sentence of the number eval is "Nummer 23".
    fake_containers(monkeypatch, "r")
    assert kit.main(["--limit", "1", "--run-dir", str(tmp_path)]) == 0
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert report["go"] is True and report["ear"] == "parakeet-v3-cpu"
    assert (tmp_path / kit.RUN_MARKER).exists()
    out = capsys.readouterr().out
    assert "ear   parakeet-v3-cpu" in out and "TRIAL: 2 of" in out


def test_a_run_without_an_ear_within_the_limit_exits_1_with_its_report(
    tmp_path, monkeypatch, capsys
):
    fake_containers(monkeypatch, "w")
    assert kit.main(["--limit", "1", "--run-dir", str(tmp_path)]) == 1
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert report["go"] is False and report["ear"] == "parakeet-v3-cpu"
    assert "another dish" in capsys.readouterr().out


def test_a_step_that_breaks_ends_the_run_and_leaves_no_old_report(
    tmp_path, monkeypatch, capsys
):
    (tmp_path / kit.RUN_MARKER).touch()
    (tmp_path / "report.json").write_text("old")

    def broken(command, dry_run, cwd=None):
        raise kit.SpikeError("`docker build` failed")

    monkeypatch.setattr(kit, "docker", broken)
    monkeypatch.setattr(kit, "docker_problem", lambda: None)
    assert kit.main(["--run-dir", str(tmp_path)]) == 1
    assert not (tmp_path / "report.json").exists()
    assert "docker build" in capsys.readouterr().err


def test_the_run_directory_of_the_other_kit_is_refused(tmp_path, capsys):
    from api.telephony import spike

    (tmp_path / spike.RUN_MARKER).touch()
    (tmp_path / "report.json").write_text("28 of 28")
    assert kit.main(["--run-dir", str(tmp_path)]) == 2
    assert (tmp_path / "report.json").read_text() == "28 of 28"


def test_a_dry_run_prints_every_step_and_writes_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(kit, "docker_problem", lambda: None)
    run_dir = tmp_path / "run"
    assert kit.main(["--dry-run", "--run-dir", str(run_dir)]) == 0
    out = capsys.readouterr().out
    assert out.count("[dry-run]") == 4
    assert not run_dir.exists()


def test_only_the_download_step_gets_a_network(tmp_path):
    fetch = kit.in_image(tmp_path, "m", "fetch", network=True)
    measure = kit.in_image(tmp_path, "m", "measure", gpu=True)
    assert "--network" not in fetch and "--gpus" not in fetch
    assert measure[measure.index("--network") + 1] == "none"
    assert "HF_HUB_OFFLINE=1" in measure and "--gpus" in measure
    assert (
        measure[-5:] == [kit.IMAGE, "python", "-m", "m", "measure", kit.RUN_MOUNT][-5:]
    )


def test_root_and_a_folder_with_somebodys_files_are_refused(
    tmp_path, monkeypatch, capsys
):
    (tmp_path / "notes.txt").write_text("mine")
    assert kit.main(["--run-dir", str(tmp_path)]) == 2
    assert (tmp_path / "notes.txt").read_text() == "mine"
    monkeypatch.setattr(kit.os, "getuid", lambda: 0)
    assert kit.main(["--run-dir", str(tmp_path / "new")]) == 2
    assert "unprivileged" in capsys.readouterr().err


def test_clearing_results_keeps_the_model_files(tmp_path):
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "ear.bin").write_text("big")
    (tmp_path / "samples").mkdir()
    (tmp_path / "samples" / "a-1.wav").write_text("old")
    for name in kit.RESULTS:
        (tmp_path / name).write_text("old")
    kit.clear_results(tmp_path)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["models"]
    assert (tmp_path / "models" / "ear.bin").exists()
