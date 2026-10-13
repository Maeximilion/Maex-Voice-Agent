"""telephony/speech_spike.py: plan, summary and pick of the spike T-10.3, without Docker.

The summary gets what a run would hand it: the bench's `heard.json` and the
judged rows. Both are built here.
"""

import csv
import json
import os
import types
from pathlib import Path

import pytest

from api.telephony import speech_spike as kit
from evals.number_eval import load


@pytest.fixture(autouse=True)
def unprivileged(monkeypatch):
    """`main` refuses to run as root, and `make test` runs the suite in the API
    container, which is root: without this the tests that play a whole run
    stopped at that guard (audit 12.10.2026). Every test here runs as an
    ordinary user; the one for the guard sets root itself.

    Only the kit's own view of `os` is changed: pytest reads the real uid when
    it makes its temporary folders, and refuses one that is not its own."""
    seen = types.ModuleType("os")
    seen.__dict__.update(vars(os))
    seen.getuid = lambda: 1000
    monkeypatch.setattr(kit, "os", seen)


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


def test_a_folder_with_somebodys_files_is_refused(tmp_path, capsys):
    (tmp_path / "notes.txt").write_text("mine")
    assert kit.main(["--run-dir", str(tmp_path)]) == 2
    assert (tmp_path / "notes.txt").read_text() == "mine"
    assert "not a run directory" in capsys.readouterr().err


@pytest.mark.parametrize("dry_run", [[], ["--dry-run"]])
def test_root_is_refused_before_docker_or_the_run_directory_is_touched(
    tmp_path, monkeypatch, capsys, dry_run
):
    """The containers take the caller's uid. As root nothing may happen at
    all: no question to Docker, no old result cleared, no folder made."""
    monkeypatch.setattr(kit.os, "getuid", lambda: 0)

    def never(*args, **kwargs):
        raise AssertionError("root must be refused before this is reached")

    for name in ("docker", "docker_problem", "gpu_in_docker", "usable_run_dir"):
        monkeypatch.setattr(kit, name, never)
    old = tmp_path / "old"
    old.mkdir()
    (old / kit.RUN_MARKER).touch()
    (old / "report.json").write_text("28 of 28")

    assert kit.main([*dry_run, "--run-dir", str(old)]) == 2
    assert kit.main([*dry_run, "--run-dir", str(tmp_path / "new")]) == 2

    assert "unprivileged" in capsys.readouterr().err
    assert sorted(f.name for f in old.iterdir()) == sorted(
        [kit.RUN_MARKER, "report.json"]
    )
    assert (old / "report.json").read_text() == "28 of 28"
    assert not (tmp_path / "new").exists()


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


# --- Another menu than the eval menu (--menu) ------------------------------------

OWN_MENU = (
    "number;name;category;price_eur;description;active\n"
    "7;Glasnudelsalat (4 Stück);Vorspeisen;5,50;;ja\n"
    "S7;Aal Nigiri;Sushi;4,90;;ja\n"
    "35b;Reisnudeln mit Rind;Hauptgerichte;12,90;;ja\n"
    "99;Altes Gericht;Hauptgerichte;9,00;;nein\n"
)


def own_menu_file() -> Path:
    import tempfile

    return own_menu(Path(tempfile.mkdtemp()))


def own_menu(tmp_path: Path, text: str = OWN_MENU) -> Path:
    path = tmp_path / "menu_items.csv"
    # The register export may start with a byte order mark.
    path.write_text(text, encoding="utf-8-sig")
    return path


def test_with_a_menu_file_its_names_and_card_numbers_are_spoken(tmp_path):
    plan = kit.build_plan(None, own_menu(tmp_path))
    numbers = [s for s in plan["sentences"] if s["id"].startswith("n")]
    cards = [s for s in plan["sentences"] if s["id"].startswith("c")]
    dishes = [s for s in plan["sentences"] if s["kind"] == "dish"]

    assert [s["say"] for s in cards] == ["Nummer 7", "Nummer S7", "Nummer 35b"]
    assert [s["expect"] for s in dishes] == [
        "Glasnudelsalat",
        "Aal Nigiri",
        "Reisnudeln mit Rind",
    ]
    assert plan["key_terms"] == [s["expect"] for s in dishes]
    # The sentences of the number eval are spoken too, but their expectations
    # were written for the eval menu: the judge resolves them on this one.
    assert [s["say"] for s in numbers] == [c.say for c in load()]
    assert {s["expect"] for s in numbers + cards} == {None}
    assert {s["kind"] for s in cards} == {"number"}
    assert plan["menu"] == [
        {"number": "7", "category": "Vorspeisen"},
        {"number": "S7", "category": "Sushi"},
        {"number": "35b", "category": "Hauptgerichte"},
    ]
    assert plan["all_sentences"] == len(plan["sentences"])
    assert len({s["id"] for s in plan["sentences"]}) == len(plan["sentences"])


def test_without_a_menu_file_the_plan_holds_the_eval_menu_and_no_card_sentences():
    """Codex PR #268: a recorded run is judged again with the menu it was
    recorded with, also when the eval menu has changed since."""
    plan = kit.build_plan(None)
    assert plan["own_menu"] is False
    with kit.MENU.open(encoding="utf-8", newline="") as f:
        rows = [r for r in csv.DictReader(f, delimiter=";") if r["active"] != "nein"]
    assert plan["menu"] == [
        {"number": r["number"], "category": r["category"]} for r in rows
    ]
    assert not any(s["id"].startswith("c") for s in plan["sentences"])
    assert None not in {s["expect"] for s in plan["sentences"]}
    assert kit.build_plan(None, own_menu_file())["own_menu"] is True


def test_a_limit_shortens_the_card_numbers_as_well(tmp_path):
    plan = kit.build_plan(2, own_menu(tmp_path))
    assert [s["id"][0] for s in plan["sentences"]] == ["n", "n", "c", "c", "d", "d"]
    assert plan["all_sentences"] > 6


@pytest.mark.parametrize(
    "text",
    [
        "",  # empty
        "number;name\n7;Suppe\n",  # no category
        "number;name;category;active\n7;Suppe;Suppen;nein\n",  # no active dish
    ],
)
def test_a_file_that_is_no_menu_is_refused_before_anything_runs(
    tmp_path, monkeypatch, capsys, text
):
    monkeypatch.setattr(kit, "docker_problem", lambda: None)
    run_dir = tmp_path / "run"
    code = kit.main(
        ["--run-dir", str(run_dir), "--menu", str(own_menu(tmp_path, text))]
    )
    assert code == 2
    assert "no menu in the import format" in capsys.readouterr().err
    assert not run_dir.exists()
    assert kit.main(["--run-dir", str(run_dir), "--menu", str(tmp_path / "gone")]) == 2


def test_the_report_names_the_menu_it_was_judged_against(capsys, tmp_path):
    report = run({"e": {"load_seconds": 1}}, judged_rows("e", "rw"))
    kit.print_report(report, tmp_path)
    assert "a dish of the eval menu" in capsys.readouterr().out
    report["own_menu_dishes"] = 173
    kit.print_report(report, tmp_path)
    out = capsys.readouterr().out
    assert "a dish of the menu given with --menu, 173 dishes" in out
    assert "eval menu" not in out


# --- Judging a recorded run again (--judge-only) ----------------------------------


def test_judge_only_judges_the_recorded_run_again_and_speaks_nothing(
    tmp_path, monkeypatch, capsys
):
    """After a change to the number rule: the same transcripts, a new verdict.
    Plan and transcripts stay as they were recorded."""
    fake_containers(monkeypatch, "w")
    assert kit.main(["--limit", "1", "--run-dir", str(tmp_path)]) == 1
    recorded = {
        name: (tmp_path / name).read_bytes() for name in ("plan.json", "heard.json")
    }
    (tmp_path / "judged.json").unlink()
    (tmp_path / "report.json").unlink()
    capsys.readouterr()

    seen = []
    fake = kit.docker
    monkeypatch.setattr(
        kit,
        "docker",
        lambda command, dry, cwd=None: (seen.append(command), fake(command, dry, cwd)),
    )
    assert kit.main(["--run-dir", str(tmp_path), "--judge-only"]) == 1

    assert not any("fetch" in c or "measure" in c for c in seen)
    assert any("evals.heard" in c for c in seen)
    assert {n: (tmp_path / n).read_bytes() for n in recorded} == recorded
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert report["go"] is False
    out = capsys.readouterr().out
    assert "no-go" in out and "Speak and hear" not in out


def test_judge_only_needs_a_recorded_run(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(kit, "docker_problem", lambda: None)
    assert kit.main(["--run-dir", str(tmp_path / "new"), "--judge-only"]) == 2
    assert "plan.json and heard.json" in capsys.readouterr().err
    assert not (tmp_path / "new").exists()


@pytest.mark.parametrize("extra", [["--limit", "2"], ["--menu", "menu_items.csv"]])
def test_judge_only_takes_the_plan_of_the_run_and_no_other(tmp_path, capsys, extra):
    with pytest.raises(SystemExit) as stop:
        kit.main(["--run-dir", str(tmp_path), "--judge-only", *extra])
    assert stop.value.code == 2
    assert "--judge-only" in capsys.readouterr().err


# --- Codex review of PR #268 ------------------------------------------------------


def test_an_inactive_dish_is_left_out_however_the_flag_is_written(tmp_path):
    """The import reads " NEIN " as inactive (`importer._bool`); so does the kit,
    or an inactive dish would be spoken and its number counted as on the card."""
    menu = own_menu(
        tmp_path,
        "number;name;category;active\n"
        "7;Suppe;Suppen;JA\n"
        "8;Alt eins;Suppen;NEIN\n"
        "9;Alt zwei;Suppen; nein \n"
        "10;Salat;Vorspeisen;\n",
    )
    plan = kit.build_plan(None, menu)
    assert [m["number"] for m in plan["menu"]] == ["7", "10"]
    assert plan["key_terms"] == ["Suppe", "Salat"]


def test_a_real_menu_is_not_written_into_a_folder_git_could_track(
    tmp_path, monkeypatch, capsys
):
    """With --menu the run directory holds the menu's dish names. Inside the
    repository only the kit's own run folder is ignored by git."""
    monkeypatch.setattr(kit, "docker_problem", lambda: None)
    menu = own_menu(tmp_path)
    inside = kit.SPEECH_DIR / "run-of-a-test"
    assert kit.main(["--menu", str(menu), "--run-dir", str(inside), "--dry-run"]) == 2
    assert "outside the repository" in capsys.readouterr().err
    assert not inside.exists()
    # Outside the repository, and in the kit's own run folder, it runs.
    assert (
        kit.main(["--menu", str(menu), "--run-dir", str(tmp_path / "r"), "--dry-run"])
        == 0
    )
    monkeypatch.setattr(kit, "usable_run_dir", lambda *a: True)
    assert kit.main(["--menu", str(menu), "--dry-run"]) == 0
    # Without a menu the eval sentences may go anywhere, as before.
    assert kit.main(["--run-dir", str(inside), "--dry-run"]) == 0


# --- Codex review of PR #268, second round ----------------------------------------


def test_the_report_of_the_eval_menu_names_no_own_menu(tmp_path, monkeypatch):
    fake_containers(monkeypatch, "r")
    assert kit.main(["--limit", "1", "--run-dir", str(tmp_path)]) == 0
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert report["own_menu_dishes"] == 0


def test_judge_only_removes_the_old_verdict_before_it_judges(
    tmp_path, monkeypatch, capsys
):
    """A judge that breaks must not leave the verdict of the run before: exit 1
    is also what a no-go returns."""
    fake_containers(monkeypatch, "w")
    assert kit.main(["--limit", "1", "--run-dir", str(tmp_path)]) == 1
    assert (tmp_path / "judged.json").exists() and (tmp_path / "report.json").exists()

    def broken(command, dry_run, cwd=None):
        raise kit.SpikeError("the judge broke")

    monkeypatch.setattr(kit, "docker", broken)
    assert kit.main(["--run-dir", str(tmp_path), "--judge-only"]) == 1
    assert not (tmp_path / "judged.json").exists()
    assert not (tmp_path / "report.json").exists()
    assert (tmp_path / "plan.json").exists() and (tmp_path / "heard.json").exists()


def test_an_utterance_gets_a_window_of_key_terms_never_the_whole_menu():
    """Hard rule 6: the full menu goes into no model context, and an ear's
    prompt is one (Codex PR #268, P1). Each utterance gets a few names; a dish
    sentence its own name first, so every dish is heard with its term."""
    names = [f"Gericht {i}" for i in range(25)]
    window = kit.key_terms_for(names, 3)
    assert window == [f"Gericht {i}" for i in range(3, 3 + kit.KEY_TERMS)]
    assert kit.key_terms_for(names, 22)[:4] == [
        "Gericht 22",
        "Gericht 23",
        "Gericht 24",
        "Gericht 0",
    ]
    # An index beyond the menu (a number sentence) wraps around.
    assert kit.key_terms_for(names, 53)[0] == "Gericht 3"
    # A menu smaller than the window is never handed over whole.
    for size in (2, 5, kit.KEY_TERMS, kit.KEY_TERMS + 1):
        few = names[:size]
        for index in range(size):
            got = kit.key_terms_for(few, index)
            assert got[0] == few[index] and len(set(got)) == len(got) < size
    # One dish is the whole menu: no key term at all (Codex PR #268, P1).
    assert kit.key_terms_for(["Suppe"], 0) == []
    assert kit.key_terms_for([], 0) == []


def test_the_report_counts_the_key_terms_an_utterance_really_got(capsys, tmp_path):
    """What the ear got after its prompt was cut, per utterance, not the size of
    the window before: a dish whose own name did not fit was heard without a
    key term and must not be counted as heard with one (Codex PR #268)."""
    plan = kit.build_plan(None)
    plan["sentences"] = [
        {"id": "n0", "kind": "number", "say": "Nummer 23", "expect": "23"},
        *(
            {"id": f"d{i}", "kind": "dish", "say": "x", "expect": name}
            for i, name in enumerate(["Edamame", "Gyoza", "Pho Bo"])
        ),
    ]
    plan["key_terms"] = ["Edamame", "Gyoza", "Pho Bo"]
    plan["ears"] = [{"id": "terms"}, {"id": "plain"}]
    base = {"voice": plan["voices"][0], "seconds": 0.1, "on_card": False}
    number = {"id": "n0", "kind": "number", "expect": "23", "verdict": "right"}
    judged = [
        {**base, **number, "ear": "terms", "key_terms": 2},
        {
            **base,
            "ear": "terms",
            "id": "d0",
            "kind": "dish",
            "verdict": "right",
            "key_terms": 2,
        },
        {
            **base,
            "ear": "terms",
            "id": "d1",
            "kind": "dish",
            "verdict": "right",
            "key_terms": 1,
        },
        {
            **base,
            "ear": "terms",
            "id": "d2",
            "kind": "dish",
            "verdict": "asked",
            "key_terms": 0,
        },
        {**base, **number, "ear": "plain"},
    ]
    heard = {
        "ears": {"terms": {"load_seconds": 1}, "plain": {"load_seconds": 1}},
        "mouths": [
            {"voice": v, "first_audio_seconds": 0.1, "seconds": 1, "audio_seconds": 2}
            for v in plan["voices"]
        ],
        "turn_end": [],
    }
    report = kit.summarize(plan, heard, judged)
    terms, plain = report["ears"]
    assert (terms["key_terms_min"], terms["key_terms_max"]) == (0, 2)
    assert terms["key_terms_given"] == 3
    assert terms["dishes_with_own_term_share"] == round(2 / 3, 4)
    assert "key_terms_max" not in plain
    kit.print_report(report, tmp_path)
    out = capsys.readouterr().out
    assert (
        "terms: an utterance was heard with 0 to 2 of 3 dish names as key terms" in out
    )
    assert "66.7 % of the dish sentences with their own name among them" in out
    assert "plain:" not in out


def test_a_short_row_is_refused_like_a_blank_field(tmp_path, monkeypatch, capsys):
    """A row that ends early gives no value at all for the last columns: that
    is a missing field, not a crash (Codex PR #268)."""
    monkeypatch.setattr(kit, "docker_problem", lambda: None)
    menu = own_menu(
        tmp_path, "number;name;category;active\n8;Salat;Vorspeisen;ja\n7;Suppe\n"
    )
    with pytest.raises(ValueError, match="row 3"):
        kit.build_plan(None, menu)
    assert kit.main(["--menu", str(menu), "--run-dir", str(tmp_path / "r")]) == 2
    assert "no menu in the import format" in capsys.readouterr().err


def test_only_the_key_terms_that_fit_the_prompt_are_handed_to_the_ear():
    """Codex PR #268: the engine cuts a prompt that is too long without a word.
    The bench cuts at a whole term and says how many it kept."""

    def words(text: str) -> int:
        return len(text.replace(",", " ").split())

    terms = ["Pho Bo", "Edamame", "Gebratene Nudeln", "Gyoza"]
    assert kit.terms_that_fit(words, terms, limit=4) == ["Pho Bo", "Edamame"]
    assert kit.terms_that_fit(words, terms, limit=100) == terms
    assert kit.terms_that_fit(words, terms, limit=1) == []
    assert kit.terms_that_fit(words, [], limit=4) == []


# --- Codex review of PR #268, third round ------------------------------------------


@pytest.mark.parametrize(
    "rows",
    [
        "7;Suppe;Suppen;ja\n07;Salat;Vorspeisen;ja\n",
        "S7;Aal;Sushi;ja\ns07;Lachs;Sushi;ja\n",
    ],
)
def test_a_menu_with_the_same_card_number_twice_is_refused(
    tmp_path, monkeypatch, capsys, rows
):
    """The import refuses "7" next to "07" (`canonical_card`); the kit would
    speak and count that position twice."""
    monkeypatch.setattr(kit, "docker_problem", lambda: None)
    menu = own_menu(tmp_path, "number;name;category;active\n" + rows)
    assert kit.main(["--menu", str(menu), "--run-dir", str(tmp_path / "r")]) == 2
    assert "twice" in capsys.readouterr().err
    # An inactive twin does not count: the import reads only what is active.
    ok = own_menu(
        tmp_path, "number;name;category;active\n7;Suppe;Suppen;ja\n07;Alt;Suppen;nein\n"
    )
    assert [m["number"] for m in kit.build_plan(None, ok)["menu"]] == ["7"]


def test_the_kit_compares_card_numbers_as_the_search_does():
    """The host has no database driver, so the kit carries its own copy of
    `canonical_card`. It must not drift."""
    from api.domain.menu.numberwords import canonical_card

    for card in [
        "7",
        "07",
        "007",
        " S7 ",
        "s07",
        "SM01",
        "25G",
        "0",
        "00",
        "35b",
        "100",
    ]:
        assert kit.canonical(card) == canonical_card(card), card


# --- Codex review of PR #268, fourth round ------------------------------------------


def test_the_same_card_number_in_two_rows_is_refused_whatever_the_objects(tmp_path):
    """Compared by value: two rows that both say "7" may be one string object."""
    menu = own_menu(
        tmp_path,
        "number;name;category;active\n7;Suppe;Suppen;ja\n7;Salat;Vorspeisen;ja\n",
    )
    with pytest.raises(ValueError, match="twice"):
        kit.build_plan(None, menu)


@pytest.mark.parametrize(
    "row", ["7;;Suppen;ja", ";Suppe;Suppen;ja", "7;Suppe; ;ja", "7;  ;Suppen;ja"]
)
def test_an_active_row_without_number_name_or_category_is_refused(tmp_path, row):
    """As in the import. A dish without a name would be "heard" in every
    transcript, the empty name is part of any text."""
    menu = own_menu(
        tmp_path, "number;name;category;active\n8;Salat;Vorspeisen;ja\n" + row + "\n"
    )
    with pytest.raises(ValueError, match="row 3"):
        kit.build_plan(None, menu)
    # An inactive row may be incomplete: it is not spoken.
    ok = own_menu(
        tmp_path, "number;name;category;active\n8;Salat;Vorspeisen;ja\n7;;Suppen;nein\n"
    )
    assert len(kit.build_plan(None, ok)["menu"]) == 1


@pytest.mark.parametrize(
    ("name", "spoken"),
    [
        ("Gyoza (6 Stück)", "Gyoza"),
        ("Lachs Nigiri (2 Stk.)", "Lachs Nigiri"),
        ("Wan-Tan (6 St)", "Wan-Tan"),
        # What says something about the dish is part of its name.
        ("Nudeln (glutenfrei)", "Nudeln (glutenfrei)"),
        ("Curry (vegan)", "Curry (vegan)"),
        ("Suppe (Bandnudeln)", "Suppe (Bandnudeln)"),
        ("Pho Bo", "Pho Bo"),
        # A name that is nothing but the pieces stays as it is: an empty name
        # would be "heard" in every transcript (Codex PR #268).
        ("(6 Stück)", "(6 Stück)"),
    ],
)
def test_only_the_number_of_pieces_is_left_off_a_dish_name(tmp_path, name, spoken):
    menu = own_menu(tmp_path, f"number;name;category;active\n7;{name};Suppen;ja\n")
    plan = kit.build_plan(None, menu)
    [dish] = [s for s in plan["sentences"] if s["kind"] == "dish"]
    assert dish["expect"] == spoken and dish["say"] == f"Ich hätte gern {spoken}."
    assert plan["key_terms"] == [spoken]


# --- Codex review of PR #268, fifth round -------------------------------------------

IMPORTABLE = "number;name;category;price_eur;description;active\n"


@pytest.mark.parametrize(
    "row",
    [
        "1000;Suppe;Suppen;5,00;;ja",  # above the card range
        "23h;Suppe;Suppen;5,00;;ja",  # an ending no card has
        "x12;Suppe;Suppen;5,00;;ja",  # a prefix that means a quantity on the phone
        "7;Suppe;Suppen;;;ja",  # no price
        "7;Suppe;Suppen;5,00;;maybe",  # a flag that is neither ja nor nein
    ],
)
def test_a_menu_the_import_refuses_has_errors(tmp_path, row):
    """The rules are the import's own (`importer.parse`), not a copy of them:
    a menu that cannot be imported is not measured."""
    assert kit.menu_errors(own_menu(tmp_path, IMPORTABLE + row + "\n"))


def test_a_menu_the_import_takes_has_no_errors(tmp_path):
    rows = "7;Suppe;Suppen;5,00;;ja\nS12;Maki (6 Stück);Sushi;5,90;;ja\n25g;Soße;Extras;2,00;;nein\n"
    assert kit.menu_errors(own_menu(tmp_path, IMPORTABLE + rows)) == []
    assert kit.menu_errors(kit.MENU) == []


def test_the_menu_is_checked_in_the_image_before_anything_is_spoken(
    tmp_path, monkeypatch, capsys
):
    """After the build and before the models are fetched: a menu the import
    refuses ends the run. Like every step that breaks, it leaves no report of
    the run before."""
    run_dir = tmp_path / "r"
    run_dir.mkdir()
    (run_dir / kit.RUN_MARKER).touch()
    (run_dir / "report.json").write_text("{}", encoding="utf-8")
    seen: list[list[str]] = []

    def docker(command, dry_run, cwd=None):
        seen.append(command)
        if "check" in command:
            raise kit.SpikeError("the menu has errors")

    monkeypatch.setattr(kit, "docker", docker)
    monkeypatch.setattr(kit, "docker_problem", lambda: None)
    monkeypatch.setattr(kit, "gpu_in_docker", lambda: False)
    menu = own_menu(tmp_path)
    assert kit.main(["--menu", str(menu), "--run-dir", str(run_dir)]) == 1
    assert [("build" in c, "check" in c) for c in seen] == [
        (True, False),
        (False, True),
    ]
    check = seen[1]
    assert "none" in check and not any(str(menu) in part for part in check)
    assert not (run_dir / "report.json").exists()
    # Without --menu nothing is checked: the eval menu is the repository's.
    seen.clear()
    monkeypatch.setattr(
        kit, "docker", lambda command, dry, cwd=None: seen.append(command)
    )
    kit.main(["--run-dir", str(tmp_path / "eval"), "--dry-run"])
    assert not any("check" in c for c in seen)


def test_the_kits_run_folder_counts_only_where_git_ignores_it(
    tmp_path, monkeypatch, capsys
):
    """A run folder that is a link to another folder of the repository is that
    folder: git ignores `deploy/speech/run`, not what it points at."""
    repo = tmp_path / "repo"
    speech = repo / "deploy" / "speech"
    speech.mkdir(parents=True)
    (repo / "scratch").mkdir()
    (speech / "run").symlink_to(repo / "scratch", target_is_directory=True)
    monkeypatch.setattr(kit, "REPO_ROOT", repo)
    monkeypatch.setattr(kit, "SPEECH_DIR", speech)
    monkeypatch.setattr(kit, "docker_problem", lambda: None)
    menu = own_menu(tmp_path)
    assert kit.main(["--menu", str(menu), "--dry-run"]) == 2
    assert "git could track" in capsys.readouterr().err
    # The folder itself, and a link that leaves the repository, are fine.
    (speech / "run").unlink()
    assert kit.main(["--menu", str(menu), "--dry-run"]) == 0
    (tmp_path / "outside").mkdir()
    (speech / "run").symlink_to(tmp_path / "outside", target_is_directory=True)
    assert kit.main(["--menu", str(menu), "--dry-run"]) == 0


# --- Codex review of PR #268, ninth round ------------------------------------------


def test_an_empty_row_is_skipped_as_the_import_skips_it(tmp_path):
    """A spreadsheet leaves rows of separators behind; the import reads past
    them (`importer._rows`), and so does the kit."""
    menu = own_menu(
        tmp_path,
        "number;name;category;active\n7;Suppe;Suppen;ja\n;;;\n ; ; ; \n8;Salat;Vorspeisen;ja\n",
    )
    assert [m["number"] for m in kit.build_plan(None, menu)["menu"]] == ["7", "8"]


# --- Codex review of PR #268, tenth round -------------------------------------------


def test_the_menu_that_is_checked_is_the_menu_that_is_measured(tmp_path, monkeypatch):
    """The file is read once. The plan is built from those bytes and a copy of
    them goes into the run folder, where the check in the image reads it: a
    file that changes while the image builds cannot be checked in one state
    and measured in another."""
    run_dir = tmp_path / "r"
    menu = own_menu(tmp_path)
    first = menu.read_text(encoding="utf-8-sig")
    checked: list[str] = []

    def docker(command, dry_run, cwd=None):
        if "build" in command:
            # Somebody saves another menu while the image builds.
            menu.write_text(
                "number;name;category;active\n99;Neu;Neu;ja\n", encoding="utf-8"
            )
        if "check" in command:
            checked.append((run_dir / kit.MENU_COPY).read_text(encoding="utf-8"))
            raise kit.SpikeError("stop here")

    monkeypatch.setattr(kit, "docker", docker)
    monkeypatch.setattr(kit, "docker_problem", lambda: None)
    assert kit.main(["--menu", str(menu), "--run-dir", str(run_dir)]) == 1
    assert checked == [first]
    plan = json.loads((run_dir / "plan.json").read_text(encoding="utf-8"))
    assert [m["number"] for m in plan["menu"]] == ["7", "S7", "35b"]
    # The copy is a result of the run: the next run clears it.
    kit.clear_results(run_dir)
    assert not (run_dir / kit.MENU_COPY).exists()


def test_menu_errors_reads_the_copy_in_the_run_folder(tmp_path):
    (tmp_path / kit.MENU_COPY).write_text(
        IMPORTABLE + "1000;Suppe;Suppen;5,00;;ja\n", encoding="utf-8"
    )
    assert kit.menu_errors(tmp_path / kit.MENU_COPY)


# --- Spoken orders: a run of its own (--orders) -----------------------------------


# Card numbers without a prefix, plain and with a letter: enough for an order of 12.
PLAIN_MENU = "number;name;category;price_eur;description;active\n" + "".join(
    f"{number};Gericht {number};Hauptgerichte;9,00;;ja\n"
    for number in (*map(str, range(10, 24)), "26", "26a", "26d", "35b")
)


def said_verdicts(plan: dict) -> list[str]:
    """Each order of the plan judged as the text the kit speaks."""
    from evals.heard import Menu, order_verdict

    menu = Menu.from_rows((r["number"], r["category"]) for r in plan["menu"])
    return [
        order_verdict(s["expect"], s["say"], menu)["verdict"] for s in plan["sentences"]
    ]


def test_spoken_orders_are_the_same_on_every_run():
    plan = kit.build_plan(None, orders=5, sizes=(2, 3, 4, 12))
    orders = plan["sentences"]
    assert plan == kit.build_plan(None, orders=5, sizes=(2, 3, 4, 12))
    assert [len(s["expect"]) for s in orders] == [2] * 5 + [3] * 5 + [4] * 5 + [12] * 5
    assert {s["kind"] for s in orders} == {"order"}
    assert plan["all_sentences"] == 20 == len({s["id"] for s in orders})
    for s in orders:
        numbers = [number for number, _ in s["expect"]]
        assert len(set(numbers)) == len(numbers)


def test_a_spoken_order_is_right_as_it_is_said(tmp_path):
    """The sentence the kit speaks is one the conversation splits and resolves
    to the order it stands for, or the run would measure the kit's sentences
    instead of the ears."""
    plan = kit.build_plan(
        None, own_menu(tmp_path, PLAIN_MENU), orders=40, sizes=(2, 3, 4, 12)
    )
    assert set(said_verdicts(plan)) == {"right"}


def test_a_spoken_order_of_the_eval_menu_is_right_as_it_is_said():
    """With prefixed numbers on the menu as well: "die S1" behind two other
    dishes is a position of its own since eval abholung_0081. The report shows
    the share that is right as text (`orders_right_as_said_share`), so a run
    tells what the rule costs from what the ear costs."""
    verdicts = said_verdicts(kit.build_plan(None, orders=40, sizes=(2, 3, 4, 12)))
    assert set(verdicts) == {"right"}


def test_spoken_orders_take_their_dishes_from_the_menu_given(tmp_path):
    plan = kit.build_plan(None, own_menu(tmp_path), orders=3, sizes=(2,))
    on_menu = {kit.canonical(r["number"]) for r in plan["menu"]}
    assert all(
        number in on_menu for s in plan["sentences"] for number, _ in s["expect"]
    )
    assert plan["own_menu"] is True


def test_an_order_larger_than_the_menu_is_refused(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(kit, "docker_problem", lambda: None)
    args = ["--menu", str(own_menu(tmp_path)), "--orders", "1", "--order-sizes", "12"]
    assert kit.main([*args, "--run-dir", str(tmp_path / "run")]) == 2
    assert "an order of 12 dishes" in capsys.readouterr().err


@pytest.mark.parametrize(
    "args",
    [
        ["--orders", "0"],
        ["--orders", "2", "--limit", "1"],
        ["--orders", "2", "--judge-only"],
        ["--order-sizes", "2,3"],
        ["--orders", "2", "--order-sizes", "1,2"],
        ["--orders", "2", "--order-sizes", "13"],
        ["--orders", "2", "--order-sizes", "zwei"],
    ],
)
def test_orders_are_a_run_of_their_own(tmp_path, capsys, args):
    with pytest.raises(SystemExit) as exit_:
        kit.main([*args, "--run-dir", str(tmp_path)])
    assert exit_.value.code == 2
    assert list(tmp_path.iterdir()) == []


def order_rows(ear: str, *verdicts: tuple[str, int, int, str]):
    """One order row per (verdict, size, dishes right, verdict as said)."""
    return [
        {
            "id": f"o{i}",
            "voice": "a",
            "ear": ear,
            "kind": "order",
            "say": "Zweimal die 13 und die 26d.",
            "text": "Zweimal die 13 und die 26.",
            "expect": "2x13, 1x26d",
            "got": "2x13, 1x26",
            "verdict": verdict,
            "on_card": verdict == "wrong",
            "size": size,
            "positions_right": right,
            "as_said": as_said,
            "seconds": 0.2,
        }
        for i, (verdict, size, right, as_said) in enumerate(verdicts)
    ]


def test_orders_are_counted_per_size_and_per_dish(capsys, tmp_path):
    judged = order_rows(
        "e",
        ("right", 2, 2, "right"),
        ("asked", 2, 1, "right"),
        ("right", 12, 12, "right"),
        ("wrong", 12, 9, "asked"),
    )
    report = run({"e": {"load_seconds": 1}}, judged)
    ear = report["ears"][0]
    assert (ear["right_share"], ear["asked_share"], ear["wrong_share"]) == (
        0.5,
        0.25,
        0.25,
    )
    # By the number of dishes, not by the text: 2 comes before 12.
    assert list(ear["by_kind"]) == ["order of 2 dishes", "order of 12 dishes"]
    assert ear["by_kind"]["order of 12 dishes"] == {
        "sentences": 2,
        "right": 0.5,
        "asked": 0.0,
        "wrong": 0.5,
    }
    assert (ear["order_dishes"], ear["order_dishes_right_share"]) == (28, 0.8571)
    assert ear["orders_right_as_said_share"] == 0.75
    assert report["mouths"][0]["understood_share"] == 0.5
    assert report["go"] is False and report["wrong"][0]["got"] == "2x13, 1x26"
    kit.print_report(report, tmp_path)
    out = capsys.readouterr().out
    assert "order of 12 dishes        2 heard" in out
    assert "85.7 % of the 28 dishes said arrived" in out
    assert "Read as text, 75.0 % of the orders are right" in out
    assert "taken as 2x13, 1x26 instead of 2x13, 1x26d" in out


def test_a_run_of_orders_speaks_only_orders_and_judges_each_as_one(
    tmp_path, monkeypatch, capsys
):
    """`main` with the containers replaced: the ear writes what was said, with
    one order heard without its last dish. The judge is the real one."""
    from evals import heard as judge_module

    def docker(command, dry_run, cwd=None):
        run_dir = next(
            (Path(arg.split(":")[0]) for arg in command if arg.endswith(":/work")), None
        )
        if "measure" in command:
            plan = json.loads((run_dir / "plan.json").read_text(encoding="utf-8"))
            assert {s["kind"] for s in plan["sentences"]} == {"order"}
            texts = [s["say"] for s in plan["sentences"]]
            texts[-1] = texts[-1].rsplit(" und ", 1)[0]
            result = {
                "ears": {"parakeet-v3-cpu": {"load_seconds": 1.0}},
                "heard": [
                    {
                        "id": s["id"],
                        "voice": plan["voices"][0],
                        "ear": "parakeet-v3-cpu",
                        "text": text,
                        "seconds": 0.2,
                    }
                    for s, text in zip(plan["sentences"], texts, strict=True)
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
    (tmp_path / "in").mkdir()
    menu = own_menu(tmp_path / "in", PLAIN_MENU)
    run_dir = tmp_path / "run"
    args = ["--orders", "2", "--order-sizes", "2,3", "--menu", str(menu)]
    assert kit.main([*args, "--run-dir", str(run_dir)]) == 1
    report = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
    ear = next(e for e in report["ears"] if e["id"] == "parakeet-v3-cpu")
    assert ear["utterances"] == 4 and ear["wrong_share"] == 0.25
    assert ear["by_kind"]["order of 3 dishes"]["wrong"] == 0.5
    assert ear["orders_right_as_said_share"] == 1.0
    out = capsys.readouterr().out
    assert "Speak and hear: 4 sentences" in out and "TRIAL" not in out
    assert "silent wrong: said" in out
