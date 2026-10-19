"""The generator of the README images (`scripts/readme_assets.py`)."""

from __future__ import annotations

import csv
import itertools
import threading
import xml.dom.minidom
from datetime import date, datetime
from fnmatch import fnmatch
from pathlib import Path

import pytest
import yaml

from scripts import readme_assets as ra

WORKPACKAGES = """\
## Block 0 - Scaffold  ·  group issue #5

| ID | Issue | Task | Spec | Depends | Status |
|---|---|---|---|---|---|
| T-0.1 | #14 | compose | x | - | fertig 16.09.2026: healthy |
| T-0.2 | #15 | api | x | T-0.1 | done 06.10.2026: built |
| T-0.3 | #16 | auth | x | T-0.2 | fertig, mit Test |

## Block 1c - Stage 1: own voice layer  ·  no group issue yet

| ID | Issue | Task | Spec | Depends | Status |
|---|---|---|---|---|---|
| T-1.11 | #1 | phone | x | - | part 1 done 14.10.2026 on the workbench |
| T-10.2 | #2 | router | x | - | open |
| T-10.11 | #3 | rules | x | - | blocked |
| T-9.1 | #4 | tunnel | x | - | dropped 07.10.2026: not needed |
| T-10.12 | #5 | trunk | x | - | deferred until customer acceptance |
| T-10.9 | #6 | odd | x | - | something nobody wrote down |

## Empfohlene Reihenfolge

| T-99.1 | #9 | not a task row of a block | x | - | fertig 01.01.2030 |
"""

STATUS = """\
> Status: 14.10.2026 · Stage 0 (Foundation) · Next gate: **G0 Go/No-Go** · Status version: 2.2.17

## Gates in Detail

| Gate | Content | Status |
|---|---|---|
| P | Plan released | passed 11.09.2026 |
| G0 | Budget | open. State 07.10.2026: budget approved |
| G1 | Reservation | open |
"""


def report(**over: object) -> dict:
    data: dict = {
        "run_at": "2026-10-14T22:30:00+00:00",
        "model": "qwen3.6-35b-a3b",
        "total": 4,
        "passed": 2,
        "hard": {"guessed_items": 0, "unconfirmed": 0, "missed_escalations": 1},
        "false_escalation_rate": 0.25,
        "refused_attempts": 3,
        "tokens_per_case": 21000,
        "cases": [
            {"passed": True, "error": None, "pending": None, "requests": 2},
            {"passed": True, "error": None, "pending": None, "requests": 2},
            {"passed": False, "error": "Timeout", "pending": None, "requests": 2},
            {"passed": False, "error": None, "pending": "T-6.5", "requests": 2},
        ],
    }
    data.update(over)
    return data


# --- task status ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("cell", "state"),
    [
        ("fertig 16.09.2026: healthy", "done"),
        ("done 06.10.2026", "done"),
        ("erledigt 23.09.2026 (Abholung)", "done"),
        ("B fertig 25.09.2026: Druckbruecke", "done"),
        ("in Arbeit, built in four parts", "progress"),
        ("in progress. **Step 1 done**", "progress"),
        ("part 1 done 14.10.2026 on the workbench", "progress"),
        ("blockiert", "blocked"),
        ("blocked", "blocked"),
        ("dropped 07.10.2026: not needed", "dropped"),
        ("offen", "open"),
        ("deferred until customer acceptance", "open"),
        ("proposal 05.10.2026, no issue yet", "open"),
        ("something nobody wrote down", "open"),
    ],
)
def test_classify_reads_german_and_english_status_words(cell: str, state: str) -> None:
    assert ra.classify(cell) == state


def test_read_blocks_counts_per_block_and_skips_rows_outside_blocks() -> None:
    blocks, latest = ra.read_blocks(WORKPACKAGES)
    assert [b.key for b in blocks] == ["0", "1c"]
    scaffold, voice = blocks
    assert scaffold.name == "Scaffold"
    assert (scaffold.count("done"), scaffold.total) == (3, 3)
    # Dropped tasks leave the total; deferred and unknown words count as open.
    assert voice.total == 5
    assert (voice.count("progress"), voice.count("blocked"), voice.count("open")) == (
        1,
        1,
        3,
    )
    assert "T-99.1" not in {t for b in blocks for t in b.tasks}
    # The latest finished task, not the 2030 row after the blocks.
    assert latest == date(2026, 10, 6)


def test_node_state_from_its_tasks() -> None:
    states = {"T-1": "done", "T-2": "done", "T-3": "progress", "T-4": "open"}
    assert ra.node_state(["T-1", "T-2"], states) == "built"
    assert ra.node_state(["T-1", "T-4"], states) == "progress"
    assert ra.node_state(["T-3"], states) == "progress"
    assert ra.node_state(["T-4"], states) == "planned"
    # A task the list does not know yet is planned, never built.
    assert ra.node_state(["T-404"], states) == "planned"
    # Caller and team have no tasks and are always there.
    assert ra.node_state([], states) == "built"


def test_every_flow_node_names_tasks_that_exist() -> None:
    blocks, _ = ra.read_blocks(ra.WORKPACKAGES.read_text(encoding="utf-8"))
    known = {t for b in blocks for t in b.tasks}
    for key, node in ra.FLOW_NODES.items():
        missing = [t for t in node["tasks"] if t not in known]
        assert not missing, f"{key} names unknown tasks {missing}"
    for a, b, _channel in ra.FLOW_LINKS:
        assert a in ra.FLOW_NODES and b in ra.FLOW_NODES
    for step in ra.FLOW_STEPS:
        ra._link_for(step.a, step.b)  # raises when a step has no line to run on


def test_read_status_finds_stage_and_gates() -> None:
    stage, gates = ra.read_status(STATUS)
    assert stage == "Stage 0 (Foundation)"
    assert [(g.key, g.passed, g.content) for g in gates] == [
        ("P", True, "Plan released"),
        ("G0", False, "Budget"),
        ("G1", False, "Reservation"),
    ]


# --- eval history -----------------------------------------------------------------


def test_run_from_report_splits_crashed_and_known_gaps() -> None:
    row = ra.run_from_report(report(), "cases")
    # 22:30 UTC on the 14th is the 15th in Berlin: the date is local (CLAUDE.md §8).
    assert row["date"] == "2026-10-15"
    assert (row["total"], row["passed"], row["crashed"], row["pending"]) == (
        "4",
        "2",
        "1",
        "1",
    )
    assert (row["guessed"], row["unconfirmed"], row["missed_esc"]) == ("0", "0", "1")
    assert row["false_esc_rate"] == "0.2500"
    assert row["tokens_per_case"] == "21000"


def test_run_from_report_leaves_missing_values_empty() -> None:
    row = ra.run_from_report(
        report(
            model="",
            hard=None,
            false_escalation_rate=None,
            tokens_per_case=None,
            cases=[],
        ),
        "targets",
    )
    assert row["model"] == ra.STAND_IN
    assert row["suite"] == "targets"
    assert row["guessed"] == row["false_esc_rate"] == row["tokens_per_case"] == ""
    assert row["crashed"] == "0"


def test_append_and_read_history_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "metrics" / "eval_history.csv"
    ra.append_row(path, ra.EVAL_FIELDS, ra.run_from_report(report(), "cases"))
    ra.append_row(path, ra.EVAL_FIELDS, ra.run_from_report(report(passed=3), "cases"))
    with path.open(encoding="utf-8") as fh:
        assert next(csv.reader(fh)) == ra.EVAL_FIELDS
    runs = ra.read_eval_history(path)
    assert [r.passed for r in runs] == [2, 3]
    assert runs[-1].hard == 1
    assert runs[-1].false_esc_rate == pytest.approx(0.25)


def test_hard_is_unknown_when_one_metric_was_not_recorded() -> None:
    run = ra.EvalRun(date(2026, 10, 14), "m", "cases", 41, 26, missed_esc=1)
    assert run.hard is None
    assert ra.read_eval_history(Path("/no/such/file.csv")) == []


def test_split_run_never_goes_negative() -> None:
    run = ra.EvalRun(date(2026, 10, 14), "m", "cases", 4, 2, crashed=1, pending=1)
    assert ra.split_run(run) == {"passed": 2, "pending": 1, "crashed": 1, "red": 0}
    unsplit = ra.EvalRun(date(2026, 10, 14), "m", "cases", 41, 26)
    assert ra.split_run(unsplit)["red"] == 15


# --- rendering --------------------------------------------------------------------


def test_every_asset_is_valid_svg_and_dated() -> None:
    facts = ra.gather()
    assets = ra.build(facts)
    for name, content in assets.items():
        xml.dom.minidom.parseString(content.encode("utf-8"))
        assert "<title" in content and "<desc" in content, name
    latest = facts.latest("cases")
    assert latest is not None
    for name in ("banner.svg", "eval-latest.svg"):
        assert latest.date.isoformat() in assets[name], name
    assert facts.progress_date is not None
    assert facts.progress_date.isoformat() in assets["progress.svg"]


def test_assets_without_any_history_still_render() -> None:
    facts = ra.gather()
    facts.runs, facts.voice = [], []
    for name in ("eval-latest.svg", "eval-trend.svg", "voice-bench.svg", "banner.svg"):
        content = ra.build(facts)[name]
        xml.dom.minidom.parseString(content.encode("utf-8"))


def test_rendering_is_deterministic() -> None:
    assert ra.build(ra.gather()) == ra.build(ra.gather())


def test_animation_hides_under_reduced_motion() -> None:
    flow = ra.build(ra.gather())["call-flow.svg"]
    assert "animateMotion" in flow
    assert "prefers-reduced-motion" in flow
    static = ra.build(ra.gather())["call-flow-static.svg"]
    assert "animateMotion" not in static


def test_check_reports_a_stale_asset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = ra.ASSETS
    for name in (*ra.PNGS, ra.PNG_SOURCES):
        (tmp_path / name).write_bytes((real / name).read_bytes())
    monkeypatch.setattr(ra, "ASSETS", tmp_path)
    assert ra.render() == 0
    assert ra.render(check=True) == 0
    (tmp_path / "progress.svg").write_text("<svg/>", encoding="utf-8")
    assert ra.render(check=True) == 1


# --- recording guards (Codex on 73a0005) -----------------------------------------


def test_the_scripted_stand_in_is_refused_unless_it_is_the_reference(
    tmp_path: Path,
) -> None:
    path = tmp_path / "eval_history.csv"
    with pytest.raises(ra.RecordError, match="stand-in"):
        ra.record_report(report(model="scripted"), None, path=path)
    assert not path.exists()
    ra.record_report(report(model="scripted"), None, reference=True, path=path)
    (run,) = ra.read_eval_history(path)
    assert run.model == ra.STAND_IN


def test_a_tagged_run_needs_a_suite_of_its_own(tmp_path: Path) -> None:
    path = tmp_path / "eval_history.csv"
    tagged = report(tags=["reservierung"])
    with pytest.raises(ra.RecordError, match="partial"):
        ra.record_report(tagged, None, path=path)
    with pytest.raises(ra.RecordError, match="full suite"):
        ra.record_report(tagged, "cases", path=path)
    with pytest.raises(ra.RecordError, match="lowercase"):
        ra.record_report(tagged, "Reservations!", path=path)
    ra.record_report(tagged, "reservations", path=path)
    assert [r.suite for r in ra.read_eval_history(path)] == ["reservations"]


def test_recording_the_same_report_twice_keeps_one_row(tmp_path: Path) -> None:
    path = tmp_path / "eval_history.csv"
    first = ra.record_report(report(), None, path=path)
    again = ra.record_report(report(), None, path=path)
    assert first.startswith("recorded")
    assert again.startswith("already recorded")
    assert len(ra.read_eval_history(path)) == 1
    # The same report as targets is a different row, not a duplicate.
    ra.record_report(report(), "targets", path=path)
    assert len(ra.read_eval_history(path)) == 2


def test_the_eval_card_names_the_model_of_a_foreign_target_row() -> None:
    facts = ra.gather()
    day = date(2026, 10, 20)
    facts.runs = [
        ra.EvalRun(day, "model-a", "targets", 27, 5),
        ra.EvalRun(day, "model-b", "cases", 41, 30),
    ]
    card = ra.build(facts)["eval-latest.svg"]
    assert ">model-b<" in card
    assert "model-a · 2026-10-20" in card
    # With targets of the same model, those are shown and no model is named twice.
    facts.runs.append(ra.EvalRun(day, "model-b", "targets", 27, 9))
    card = ra.build(facts)["eval-latest.svg"]
    assert ">9/27<" in card
    assert "model-a" not in card


def test_the_social_preview_carries_no_number_that_can_go_stale() -> None:
    social = ra.build(ra.gather())["social-preview.svg"]
    assert "eval" not in social
    assert "/41" not in social


# --- second Codex round on 93576eb ---------------------------------------------


def test_a_run_in_which_no_request_reached_the_model_is_refused(tmp_path: Path) -> None:
    dead = report(cases=[{"passed": False, "requests": 3, "unmetered": 3}])
    with pytest.raises(ra.RecordError, match="no request"):
        ra.record_report(dead, None, path=tmp_path / "h.csv")
    alive = report(cases=[{"passed": True, "requests": 3, "unmetered": 1}])
    assert ra.record_report(alive, None, path=tmp_path / "h.csv").startswith("recorded")


def test_two_reports_of_the_same_second_stay_two_rows(tmp_path: Path) -> None:
    path = tmp_path / "h.csv"
    ra.record_report(report(), None, path=path, report_name="eval_1_000001.json")
    ra.record_report(report(), None, path=path, report_name="eval_1_000002.json")
    again = ra.record_report(
        report(), None, path=path, report_name="eval_1_000002.json"
    )
    assert again.startswith("already recorded")
    assert len(ra.read_eval_history(path)) == 2


def test_runs_of_one_day_sort_by_their_own_time(tmp_path: Path) -> None:
    path = tmp_path / "h.csv"
    late = report(run_at="2026-10-20T20:00:00+02:00", passed=4)
    early = report(run_at="2026-10-20T09:00:00+02:00", passed=1)
    ra.record_report(late, None, path=path)
    ra.record_report(early, None, path=path)
    assert [r.passed for r in ra.read_eval_history(path)] == [1, 4]


def test_a_passed_case_still_marked_pending_counts_once() -> None:
    row = ra.run_from_report(
        report(
            total=3,
            passed=1,
            cases=[
                {"passed": True, "pending": "T-6.5"},
                {"passed": False, "pending": "T-6.5"},
                {"passed": False},
            ],
        ),
        "targets",
    )
    assert (row["passed"], row["pending"]) == ("1", "1")


def test_the_description_names_the_model_of_a_foreign_target_row() -> None:
    facts = ra.gather()
    day = date(2026, 10, 20)
    facts.runs = [
        ra.EvalRun(day, "model-a", "targets", 27, 5),
        ra.EvalRun(day, "model-b", "cases", 41, 30),
    ]
    card = ra.build(facts)["eval-latest.svg"]
    assert "Target cases 5 of 27, from model model-a" in card


def test_png_derivatives_go_stale_with_their_source() -> None:
    assets = ra.build(ra.gather())
    assert ra.stale_pngs(assets) == []
    assets["logo-mark.svg"] += "<!-- changed -->"
    assert ra.stale_pngs(assets) == ["logo-mark-512.png"]


def test_newest_report_follows_the_runner_folders(tmp_path: Path) -> None:
    assert ra.newest_report(None, tmp_path) is None
    (tmp_path / "targets").mkdir()
    for name in (
        "eval_20261020_090000_000001.json",
        "eval_20261020_200000_000001.json",
    ):
        (tmp_path / name).write_text("{}")
    (tmp_path / "targets" / "eval_20261019_100000_000001.json").write_text("{}")
    newest, targets = (
        ra.newest_report(None, tmp_path),
        ra.newest_report("targets", tmp_path),
    )
    assert newest is not None and newest.name == "eval_20261020_200000_000001.json"
    assert targets is not None and targets.name == "eval_20261019_100000_000001.json"


# --- third Codex round on 08639c9 ----------------------------------------------


def test_rule_one_names_every_kind_of_data_that_only_the_database_gives() -> None:
    title, *lines = ra.HARD_RULES[0]
    rule = " ".join(lines)
    assert title == "AI understands, code decides"
    for word in ("Prices", "zones", "hours", "availability", "allergens"):
        assert word in rule


def test_the_trend_draws_one_model_only() -> None:
    facts = ra.gather()
    facts.runs = [
        ra.EvalRun(date(2026, 10, 20), "model-a", "cases", 41, 10),
        ra.EvalRun(date(2026, 10, 21), "model-a", "cases", 41, 12),
        ra.EvalRun(date(2026, 10, 22), "model-b", "cases", 41, 30),
    ]
    trend = ra.build(facts)["eval-trend.svg"]
    assert "MODEL-B" in trend
    assert "regular cases 2026-10-22: 30/41" in trend
    assert "10/41" not in trend and "12/41" not in trend


def test_recording_the_same_voice_measurement_twice_keeps_one_row(
    tmp_path: Path,
) -> None:
    path = tmp_path / "voice.csv"
    row = {
        "date": "2026-10-14",
        "package": "T-1.11",
        "metric": "Outage drills end at the team phone",
        "value": "15",
        "unit": "of 15 points",
        "limit": "",
        "verdict": "go",
        "note": "",
    }
    assert ra.record_voice(row, path).startswith("recorded")
    assert ra.record_voice(dict(row), path).startswith("already recorded")
    assert ra.record_voice({**row, "value": "15.0"}, path).startswith("already")
    assert len(ra.read_voice_history(path)) == 1
    ra.record_voice({**row, "value": "14"}, path)
    assert len(ra.read_voice_history(path)) == 2


# --- fourth Codex round on c5eb108 ---------------------------------------------


def test_a_real_model_run_without_a_single_request_is_refused(tmp_path: Path) -> None:
    crashed_early = report(
        cases=[{"passed": False, "error": "boom", "requests": 0, "unmetered": 0}]
    )
    with pytest.raises(ra.RecordError, match="no request"):
        ra.record_report(crashed_early, None, path=tmp_path / "h.csv")
    no_usage_fields = report(cases=[{"passed": False}])
    with pytest.raises(ra.RecordError, match="no request"):
        ra.record_report(no_usage_fields, None, path=tmp_path / "h.csv")


def test_a_missing_or_changed_png_is_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = ra.ASSETS
    for name in (*ra.PNGS, ra.PNG_SOURCES):
        (tmp_path / name).write_bytes((real / name).read_bytes())
    monkeypatch.setattr(ra, "ASSETS", tmp_path)
    assets = ra.build(ra.gather())
    assert ra.stale_pngs(assets) == []
    (tmp_path / "social-preview.png").write_bytes(b"not a png")
    assert ra.stale_pngs(assets) == ["social-preview.png"]
    (tmp_path / "logo-mark-512.png").unlink()
    assert ra.stale_pngs(assets) == ["social-preview.png", "logo-mark-512.png"]


# --- the call flow follows the code (Maxi, 14.10.2026) ---------------------------

TOOLS = ("status", "menu", "draft", "confirm")


def _steps(a: str, b: str) -> list:
    return [s for s in ra.FLOW_STEPS if (s.a, s.b) == (a, b)]


def test_the_llm_talks_to_the_core_and_nothing_else() -> None:
    """Hard rule 1 in the picture: the model never reaches a tool or the database."""
    llm_links = {frozenset((a, b)) for a, b, _c in ra.FLOW_LINKS if "llm" in (a, b)}
    assert llm_links == {frozenset(("core", "llm"))}
    for step in ra.FLOW_STEPS:
        if "llm" in (step.a, step.b):
            assert {step.a, step.b} == {"core", "llm"}


def test_one_thing_moves_at_a_time_inside_a_turn() -> None:
    """One tool per hop: the steps through core, LLM, tools and Postgres never overlap."""
    inner = {"core", "llm", "postgres", *TOOLS}
    steps = sorted(
        (s for s in ra.FLOW_STEPS if s.a in inner and s.b in inner), key=lambda s: s.t0
    )
    assert steps
    for before, after in itertools.pairwise(steps):
        assert after.t0 >= before.t1 - 1e-9, (before, after)


def test_every_tool_hop_returns_to_the_llm_before_the_next_tool() -> None:
    seq = [(s.a, s.b) for s in sorted(ra.FLOW_STEPS, key=lambda s: s.t0)]
    for tool in ("status", "menu", "draft"):
        i = seq.index(("core", tool))
        assert seq[i - 2 : i] == [("core", "llm"), ("llm", "core")]
        assert seq[i + 1 : i + 4] == [
            (tool, "postgres"),
            ("postgres", tool),
            (tool, "core"),
        ]


def test_confirm_comes_only_after_the_read_back_and_the_yes() -> None:
    readback_heard, confirmation_heard = sorted(
        _steps("access", "caller"), key=lambda s: s.t0
    )
    (confirm,) = _steps("core", "confirm")
    words_in = sorted(s.t0 for s in _steps("caller", "access"))
    assert len(words_in) == 2, "the order and the yes"
    assert readback_heard.t1 <= words_in[1] < confirm.t0
    (stored,) = _steps("confirm", "postgres")
    for target in ("slip", "tablet"):
        (handover,) = _steps("postgres", target)
        assert handover.t0 >= stored.t1
    # The result of confirm goes back through the loop: the caller hears it.
    (back,) = _steps("confirm", "core")
    assert stored.t1 <= back.t0 < confirmation_heard.t0


def test_the_slip_waits_for_the_approval_on_the_tablet() -> None:
    (shown,) = _steps("postgres", "tablet")
    (approved,) = _steps("team", "tablet")
    (stored,) = _steps("tablet", "postgres")
    (printed,) = _steps("postgres", "slip")
    assert shown.t1 <= approved.t0 < stored.t0 < printed.t0


def test_flow_labels_are_english() -> None:
    """Repository text is English (CLAUDE.md §8); the tablet's German button
    text stays in the product, not in the diagram."""
    words = {w for node in ra.FLOW_NODES.values() for w in node["label"]}
    assert "Passt" not in words


def test_a_person_is_reached_by_phone_not_by_the_tablet() -> None:
    def ends_at_box(route: list[tuple[float, float]], key: str) -> bool:
        x, y, w, h = ra._node_box(key)
        ex, ey = route[-1]
        return x - 1 <= ex <= x + w + 1 and y - 1 <= ey <= y + h + 1

    assert ends_at_box(ra.ESCALATION_ROUTE, "teamphone")
    assert ends_at_box(ra.OUTAGE_ROUTE, "teamphone")
    ax, ay, aw, ah = ra._node_box("asterisk")
    sx, sy = ra.OUTAGE_ROUTE[0]
    assert ax <= sx <= ax + aw and sy >= ay + ah - 1, (
        "the outage starts at the exchange"
    )
    cx, cy, cw, ch = ra._node_box("core")
    sx, sy = ra.ESCALATION_ROUTE[0]
    assert cx <= sx <= cx + cw and sy >= cy + ch - 1, "the handover starts at the core"


def test_words_go_in_on_one_wire_and_the_reply_comes_back_on_the_other() -> None:
    for step in ra.FLOW_STEPS:
        link, _reverse = ra._link_for(step.a, step.b)
        if link[2] == "in":
            assert step.color == "neon"
        if link[2] == "out":
            assert step.color == "violet"


def test_every_caption_is_shown_and_the_last_holds_to_the_loop_end() -> None:
    flow = ra.build(ra.gather())["call-flow.svg"]
    for _start, _end, words in ra.FLOW_CAPTIONS:
        assert words in flow
    assert ra.FLOW_CAPTIONS[-1][1] == ra.FLOW_LOOP
    assert ra.FLOW_STILL in ra.build(ra.gather())["call-flow-static.svg"]


def test_the_picture_names_the_direct_print_and_the_voice_failure_exactly() -> None:
    flow = ra.build(ra.gather())["call-flow.svg"]
    assert "primary mode: at once" in ra.FLOW_CAPTIONS[-1][2]
    # Only a failure of the service voice is the exchange's fallback; a model
    # failure in the core takes the handover path (api/agent/loop.py _too_slow).
    assert "any failure" not in flow
    assert "the service voice fails" in flow
    assert "model failure" in flow
    # A callback needs the caller's number; without one the call ends with the
    # honest "nobody reachable" sentence (api/agent/loop.py _handoff).
    assert "number known" in flow


def test_tablet_and_reply_start_together_when_confirm_commits() -> None:
    """The tablet polls Postgres; it does not wait for the spoken reply."""
    (stored,) = _steps("confirm", "postgres")
    (shown,) = _steps("postgres", "tablet")
    (back,) = _steps("postgres", "confirm")
    assert shown.t0 == back.t0
    # Right after the commit, at most the short pause between two captions.
    assert stored.t1 <= shown.t0 <= stored.t1 + ra.FLOW_PAUSE + 1e-9


# --- measurements record themselves (T-0.9) -------------------------------------


@pytest.fixture
def recording(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """Histories in tmp_path, a branch to record on, and render counted, not run."""
    state: dict = {"renders": 0, "reports": tmp_path / "reports"}
    state["reports"].mkdir()
    monkeypatch.setattr(ra, "REPORTS", state["reports"])
    monkeypatch.setattr(ra, "EVAL_HISTORY", tmp_path / "eval.csv")
    monkeypatch.setattr(ra, "VOICE_HISTORY", tmp_path / "voice.csv")
    monkeypatch.setattr(ra, "recording_branch", lambda root=ra.ROOT: "task/x")
    monkeypatch.setattr(ra, "merged_voice", set)

    def render() -> int:
        state["renders"] += 1
        return 0

    monkeypatch.setattr(ra, "render", render)
    return state


def write_report(folder: Path, name: str, **over: object) -> Path:
    path = folder / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(ra.json.dumps(report(**over)), encoding="utf-8")
    return path


def test_a_full_real_model_run_records_itself_once_on_a_branch(recording: dict) -> None:
    write_report(recording["reports"], "eval_20261015_100000_000001.json")
    assert ra.auto_record("cases", 0, recording["reports"]) == 0
    assert ra.auto_record("cases", 0, recording["reports"]) == 0
    runs = ra.read_eval_history(ra.EVAL_HISTORY)
    assert [(r.suite, r.passed, r.total) for r in runs] == [("cases", 2, 4)]
    assert recording["renders"] == 2


def test_targets_are_read_from_their_own_folder(recording: dict) -> None:
    write_report(recording["reports"] / "targets", "eval_20261015_100000_000001.json")
    ra.auto_record("targets", 0, recording["reports"])
    assert [r.suite for r in ra.read_eval_history(ra.EVAL_HISTORY)] == ["targets"]


@pytest.mark.parametrize(
    ("over", "said"),
    [
        ({"tags": ["menu"]}, "--suite menu"),
        ({"model": "scripted"}, "stand-in"),
        ({"cases": [{"passed": False, "requests": 0}]}, "no request"),
    ],
)
def test_a_partial_a_stand_in_or_an_unmetered_run_is_not_recorded(
    recording: dict, capsys: pytest.CaptureFixture[str], over: dict, said: str
) -> None:
    write_report(recording["reports"], "eval_20261015_100000_000001.json", **over)
    assert ra.auto_record("cases", 0, recording["reports"]) == 0
    assert said in capsys.readouterr().out
    assert not ra.EVAL_HISTORY.exists()
    assert recording["renders"] == 0


def test_a_report_older_than_the_run_is_not_taken_for_it(
    recording: dict, capsys: pytest.CaptureFixture[str]
) -> None:
    path = write_report(recording["reports"], "eval_20261015_100000_000001.json")
    ra.auto_record("cases", path.stat().st_mtime + 60, recording["reports"])
    assert "wrote no report" in capsys.readouterr().out
    assert not ra.EVAL_HISTORY.exists()


def test_on_main_nothing_is_written_and_pending_lists_the_run(
    recording: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ra, "recording_branch", lambda root=ra.ROOT: None)
    folder = recording["reports"]
    path = write_report(folder, "eval_20261015_100000_000001.json")
    write_report(folder, "eval_20261015_090000_000001.json", tags=["menu"])
    write_report(folder / "targets", "eval_20261015_080000_000001.json", model="")
    (folder / "eval_20261015_070000_000001.json").write_text("{not json")
    ra.auto_record("cases", 0, folder)
    assert not ra.EVAL_HISTORY.exists()
    reports, rows = ra.pending([folder], ra.EVAL_HISTORY, ra.VOICE_HISTORY)
    assert reports == [(path, "cases")] and rows == []


def test_pending_records_on_a_branch_and_then_lists_nothing(recording: dict) -> None:
    folder = recording["reports"]
    write_report(folder, "eval_20261015_100000_000001.json")
    ra.append_row(folder / ra.VOICE_PENDING, ra.VOICE_FIELDS, drill_row())
    assert ra.record_pending([folder]) == 0
    assert len(ra.read_eval_history(ra.EVAL_HISTORY)) == 1
    assert len(ra.read_voice_history(ra.VOICE_HISTORY)) == 1
    assert (folder / ra.VOICE_PENDING).exists()
    assert ra.pending([folder], ra.EVAL_HISTORY, ra.VOICE_HISTORY) == ([], [])


def test_rows_recorded_on_a_closed_branch_still_wait_for_main(
    recording: dict, tmp_path: Path
) -> None:
    """#207: a branch that records and is then closed unmerged loses nothing.

    The rows wait in the main folder, the second report folder of a worktree."""
    here, main_folder = recording["reports"], tmp_path / "main" / "reports"
    main_folder.mkdir(parents=True)
    ra.append_row(main_folder / ra.VOICE_PENDING, ra.VOICE_FIELDS, drill_row())
    assert ra.record_pending([here, main_folder]) == 0
    main_history = tmp_path / "main_voice.csv"
    _, rows = ra.pending([here, main_folder], ra.EVAL_HISTORY, main_history)
    assert rows == [drill_row()]
    # A second recording on the same branch writes no line twice.
    assert ra.record_pending([here, main_folder]) == 0
    with ra.VOICE_HISTORY.open(encoding="utf-8", newline="") as fh:
        assert len(list(ra.csv.DictReader(fh))) == 1
    # A later kit run is the only one still pending on that branch.
    later = {**drill_row("14"), "run_at": "2026-10-15T20:00:00+00:00"}
    ra.append_row(main_folder / ra.VOICE_PENDING, ra.VOICE_FIELDS, later)
    _, rows = ra.pending([here, main_folder], ra.EVAL_HISTORY, ra.VOICE_HISTORY)
    assert rows == [later]


def test_recording_drops_the_waiting_rows_main_already_holds(
    recording: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = recording["reports"]
    later = {**drill_row("14"), "run_at": "2026-10-15T20:00:00+00:00"}
    for row in (drill_row(), later):
        ra.append_row(folder / ra.VOICE_PENDING, ra.VOICE_FIELDS, row)
    merged = {ra._voice_identity({**drill_row(), "value": "15.0"})}
    monkeypatch.setattr(ra, "merged_voice", lambda: merged)
    assert ra.record_pending([folder]) == 0
    with (folder / ra.VOICE_PENDING).open(encoding="utf-8", newline="") as fh:
        assert list(ra.csv.DictReader(fh)) == [later]
    merged.add(ra._voice_identity(later))
    assert ra.record_pending([folder]) == 0
    assert not (folder / ra.VOICE_PENDING).exists()


def test_a_waiting_row_that_does_not_parse_is_skipped_and_kept(
    recording: dict,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A line cut off when a kit was killed neither blocks `pending` nor is pruned,
    and the next kit's row starts a line of its own."""
    folder = recording["reports"]
    merged_row = {**drill_row("14"), "run_at": "2026-10-15T18:00:00+00:00"}
    ra.append_row(folder / ra.VOICE_PENDING, ra.VOICE_FIELDS, merged_row)
    with (folder / ra.VOICE_PENDING).open("a", encoding="utf-8") as fh:
        fh.write("2026-10-15,T-1.11,Outage drills,1")  # the "5" of 15 never written
    hand_edit = {**drill_row("13"), "date": "16.10.2026"}
    for row in (drill_row(), hand_edit):
        ra.append_row(folder / ra.VOICE_PENDING, ra.VOICE_FIELDS, row)
    monkeypatch.setattr(ra, "merged_voice", lambda: {ra._voice_identity(merged_row)})
    assert ra.record_pending([folder]) == 0
    assert capsys.readouterr().err.count("does not parse") == 2
    with ra.VOICE_HISTORY.open(encoding="utf-8", newline="") as fh:
        assert [r["value"] for r in ra.csv.DictReader(fh)] == ["15"]
    with (folder / ra.VOICE_PENDING).open(encoding="utf-8", newline="") as fh:
        cut, kept, edited = ra.csv.DictReader(fh)
    assert cut["value"] == "1" and cut["unit"] is None
    assert kept == drill_row() and edited == hand_edit


def test_a_kit_writing_to_an_empty_pending_file_writes_the_header(
    tmp_path: Path,
) -> None:
    waiting = tmp_path / ra.VOICE_PENDING
    waiting.touch()  # the kit before was killed ahead of its header
    ra.append_row(waiting, ra.VOICE_FIELDS, drill_row())
    _, rows = ra.pending([tmp_path], tmp_path / "e.csv", tmp_path / "v.csv")
    assert rows == [drill_row()]


def test_a_pending_file_another_session_holds_open_is_pruned_later(
    recording: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = recording["reports"]
    for row in (drill_row(), {**drill_row("14"), "run_at": "2026-10-15T20:00:00Z"}):
        ra.append_row(folder / ra.VOICE_PENDING, ra.VOICE_FIELDS, row)
    before = (folder / ra.VOICE_PENDING).read_text(encoding="utf-8")

    def held_open(src: object, dst: object) -> None:
        raise PermissionError("in use")

    monkeypatch.setattr(ra.os, "replace", held_open)
    ra.prune_pending(folder / ra.VOICE_PENDING, {ra._voice_identity(drill_row())})
    assert (folder / ra.VOICE_PENDING).read_text(encoding="utf-8") == before
    assert [p.name for p in folder.iterdir()] == [ra.VOICE_PENDING]


@pytest.mark.skipif(ra.shutil.which("git") is None, reason="needs git")
def test_main_s_voice_rows_come_from_origin_main(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))

    def git(*args: str) -> None:
        isolated = ["-c", "commit.gpgsign=false", "-c", "core.hooksPath="]
        ra.subprocess.run(
            ["git", *isolated, *args], cwd=tmp_path, check=True, capture_output=True
        )

    history = tmp_path / ra.VOICE_PATH
    history.parent.mkdir(parents=True)
    row = {**drill_row(), "metric": "Outage drill café"}  # read as UTF-8 on Windows
    ra.append_row(history, ra.VOICE_FIELDS, row)
    assert ra.merged_voice(tmp_path) == set()  # no git, no pruning
    git("init", "-q")
    git("add", ".")
    git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "m")
    git("update-ref", "refs/remotes/origin/main", "HEAD")
    history.unlink()  # the checkout's own file plays no part
    assert ra.merged_voice(tmp_path) == {ra._voice_identity(row)}


def test_pending_refuses_to_record_on_main(
    recording: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ra, "recording_branch", lambda root=ra.ROOT: None)
    write_report(recording["reports"], "eval_20261015_100000_000001.json")
    assert ra.record_pending([recording["reports"]]) == 1
    assert not ra.EVAL_HISTORY.exists()


RUN_AT = datetime(2026, 10, 15, 21, 30, tzinfo=ra.BERLIN)


def drill_row(value: str = "15") -> dict[str, str]:
    return {
        "run_at": "2026-10-15T19:30:00+00:00",
        "date": "2026-10-15",
        "package": "T-1.11",
        "metric": "Outage drills end at the team phone",
        "value": value,
        "unit": "of 15 points",
        "limit": "",
        "verdict": "go",
        "note": "",
    }


def test_the_drill_and_the_spike_record_their_points() -> None:
    day = RUN_AT
    drill = [{"point": "a", "ok": True}, {"point": "b", "ok": False}]
    spike = [{"point": "a", "passed": True}, {"point": "b", "passed": True}]
    assert ra.kit_rows("drill", drill, day) == [
        {**drill_row("1"), "unit": "of 2 points", "verdict": "no-go"}
    ]
    (row,) = ra.kit_rows("spike", spike, day)
    assert (row["package"], row["value"], row["unit"], row["verdict"]) == (
        "T-10.1 step 1",
        "2",
        "of 2 points",
        "go",
    )
    assert ra.kit_rows("drill", [], day) == []


def speech_report(**over: object) -> dict:
    data: dict = {
        "sentences": 100,
        "all_sentences": 100,
        "own_menu_dishes": 0,
        "wrong_limit": 0.02,
        "sampled": ["ear-c"],
        "ear": "ear-a",
        "go": True,
        "ears": [
            {"id": "ear-a", "wrong_share": 0.015, "dishes_share": 0.9},
            {"id": "ear-b", "wrong_share": 0.0241, "dishes_share": 0.95},
            {"id": "ear-c", "wrong_share": 0.0, "dishes_share": 1.0},
            {"id": "ear-d", "error": "heard nothing"},
        ],
    }
    data.update(over)
    return data


def test_the_speech_kit_records_each_judged_ear_and_the_best_dishes() -> None:
    rows = ra.kit_rows("speech", speech_report(), RUN_AT)
    got = [(r["metric"], r["value"], r["limit"], r["verdict"]) for r in rows]
    assert got == [
        ("Silent wrong numbers (ear-a)", "1.5", "2 %", "go"),
        ("Silent wrong numbers (ear-b)", "2.4", "2 %", "no-go"),
        ("Dish names heard right (ear-a)", "90.0", "", "go"),
    ]


def test_a_speech_trial_records_nothing_and_orders_get_their_own_names() -> None:
    day = RUN_AT
    assert ra.kit_rows("speech", speech_report(sentences=10), day) == []
    ears = [
        {
            "id": "ear-a",
            "wrong_share": 0.05,
            "order_dishes": 400,
            "order_dishes_right_share": 0.825,
        }
    ]
    rows = ra.kit_rows(
        "speech", speech_report(ears=ears, go=False, own_menu_dishes=173), day
    )
    assert [(r["metric"], r["value"], r["verdict"], r["note"]) for r in rows] == [
        (
            "Orders taken wrong silently (ear-a)",
            "5.0",
            "no-go",
            "own menu of 173 dishes",
        ),
        (
            "Dishes heard right in orders (ear-a)",
            "82.5",
            "no-go",
            "own menu of 173 dishes",
        ),
    ]


def test_a_kit_run_on_main_waits_in_the_pending_file(
    recording: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ra, "recording_branch", lambda root=ra.ROOT: None)
    ra.record_measurements([drill_row()], recording["reports"])
    assert not ra.VOICE_HISTORY.exists()
    _, rows = ra.pending([recording["reports"]], ra.EVAL_HISTORY, ra.VOICE_HISTORY)
    assert rows == [drill_row()]


def test_a_kit_run_records_only_its_own_fresh_report(
    recording: dict, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run = tmp_path / "run"
    run.mkdir()
    (run / "report.json").write_text('[{"point": "a", "ok": true}]')
    started = (run / "report.json").stat().st_mtime
    ra.record_kit_run("drill", ["--dry-run"], run, started)
    ra.record_kit_run("drill", [], run, started + 60)
    assert not ra.VOICE_HISTORY.exists()
    ra.record_kit_run("drill", ["--keep", "--run-dir", str(run)], tmp_path, started)
    assert [r.value for r in ra.read_voice_history(ra.VOICE_HISTORY)] == [1.0]
    # A report the recorder cannot read is a warning, never the kit's failure.
    (run / "report.json").write_text('{"sentences": 1}')
    ra.record_kit_run("speech", [], run, started)
    assert "warning" in capsys.readouterr().err


def test_recording_happens_on_a_branch_only(tmp_path: Path) -> None:
    def git(*args: str) -> None:
        ra.subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    # No repository, or no git at all (the api image has none): never a branch.
    assert ra.recording_branch(tmp_path) is None
    if ra.shutil.which("git") is None:
        pytest.skip("git is not installed here")
    git("init", "-q", "-b", "main")
    git(
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.com",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "x",
    )
    assert ra.recording_branch(tmp_path) is None
    git("checkout", "-q", "-b", "task/x")
    assert ra.recording_branch(tmp_path) == "task/x"
    git("checkout", "-q", "--detach")
    assert ra.recording_branch(tmp_path) is None


def test_a_row_both_sides_of_a_merge_appended_is_read_once(tmp_path: Path) -> None:
    path = tmp_path / "eval.csv"
    ra.record_report(report(), None, path=path, report_name="eval_a.json")
    line = path.read_text().splitlines()[-1]
    path.write_text(path.read_text() + line + "\n")
    assert len(ra.read_eval_history(path)) == 1
    voice = tmp_path / "voice.csv"
    ra.append_row(voice, ra.VOICE_FIELDS, drill_row())
    ra.append_row(voice, ra.VOICE_FIELDS, {**drill_row(), "value": "15.0"})
    assert len(ra.read_voice_history(voice)) == 1


def test_the_voice_chart_shows_the_latest_row_of_each_quantity() -> None:
    def row(day: int, metric: str, value: float) -> ra.VoiceRow:
        return ra.VoiceRow(
            date(2026, 10, day),
            "T-10.3",
            metric,
            value,
            "%",
            "",
            "go",
            shown=f"{value}",
        )

    facts = ra.Facts(
        "0.1.0",
        "Stage 0",
        [],
        [],
        None,
        0,
        [],
        [row(13, "A", 2.0), row(13, "B", 7.0), row(14, "A", 2.4)],
    )
    chart = ra.asset_voice_bench(facts)
    assert "2.4 %" in chart and "7.0 %" in chart and "2.0 %" not in chart


# --- README text from data (T-0.10) ---------------------------------------------


def facts(states: dict[str, str], gates: list[ra.Gate] | None = None) -> ra.Facts:
    block = ra.Block("0", "Scaffold", dict(states))
    return ra.Facts(
        "0.1.0",
        "Stage 0 (Foundation)",
        gates
        if gates is not None
        else [ra.Gate("P", True, "Plan"), ra.Gate("G0", False, "Budget · Rights")],
        [block],
        date(2026, 10, 14),
        0,
        [],
        [],
    )


def test_the_status_table_comes_from_the_status_and_the_task_list() -> None:
    assert ra.status_region(facts({})).splitlines()[2:] == [
        "| Version | 0.1.0 |",
        "| Stage | 0, Foundation |",
        "| Next Gate | G0: Budget · Rights |",
        "| As of | 2026-10-14 |",
    ]
    done = facts({}, [ra.Gate("P", True, "Plan")])
    assert "| Next Gate | none, every gate passed |" in ra.status_region(done)
    with pytest.raises(SystemExit, match="no gate rows"):
        ra.status_region(facts({}, []))


def test_a_capability_moves_to_what_works_when_all_its_tasks_are_done() -> None:
    caps = [
        ra.Capability("A works", ["T-0.1", "T-0.2"], missing="No A yet"),
        ra.Capability("B works", ["T-0.1"]),
        ra.Capability("C works", [], missing="No C yet", waits_for="C1"),
        ra.Capability("D works", []),
    ]
    half = ra.capabilities_region(facts({"T-0.1": "done", "T-0.2": "progress"}), caps)
    works, missing = half.split("**Not yet working:**")
    assert "- B works" in works and "- D works" in works
    assert "- No A yet" in missing and "- No C yet" in missing
    full = ra.capabilities_region(facts({"T-0.1": "done", "T-0.2": "done"}), caps)
    works, missing = full.split("**Not yet working:**")
    assert "- A works" in works
    # Waits for something outside the task list: stays until the line is removed.
    assert "- No C yet" in missing and "C works" not in full


def test_only_the_text_between_the_markers_is_written() -> None:
    text = "intro\n<!-- readme:status -->\nold\n<!-- /readme:status -->\nby hand\n"
    assert ra.fill_regions(text, {"status": "new\nrows"}) == (
        "intro\n<!-- readme:status -->\nnew\nrows\n<!-- /readme:status -->\nby hand\n"
    )
    with pytest.raises(SystemExit, match="readme:capabilities"):
        ra.fill_regions(text, {"capabilities": "x"})


def test_every_capability_names_tasks_that_exist() -> None:
    blocks, _ = ra.read_blocks(ra.WORKPACKAGES.read_text(encoding="utf-8"))
    known = {t for b in blocks for t in b.tasks}
    caps = ra.read_capabilities()
    assert caps
    for cap in caps:
        assert set(cap.tasks) <= known, cap.works
        assert cap.works.strip() and "\n" not in cap.works


# --- a new component cannot be forgotten (T-0.10) --------------------------------


def components() -> set[str]:
    """What a call can pass through: the core's tools, the Compose services and
    the adapters of the telephone line."""
    from api.agent.dispatch import TOOLS

    compose = ra.ROOT / "docker-compose.yml"
    services = yaml.safe_load(compose.read_text(encoding="utf-8"))["services"]
    folder = ra.ROOT / "api" / "telephony" / "adapters"
    # Modules and packages: an adapter of several files is a folder.
    adapters = [*folder.glob("*.py"), *(p.parent for p in folder.glob("*/__init__.py"))]
    return (
        {f"tool:{name}" for name in TOOLS}
        | {f"service:{name}" for name in services}
        | {f"adapter:{p.stem}" for p in adapters if p.stem != "__init__"}
    )


def not_in_the_picture(found: set[str]) -> list[str]:
    patterns = [c for node in ra.FLOW_NODES.values() for c in node.get("covers", [])]
    return sorted(
        c
        for c in found
        if c not in ra.NOT_DRAWN and not any(fnmatch(c, p) for p in patterns)
    )


def test_every_component_on_the_call_path_has_a_node_or_a_reason() -> None:
    missing = not_in_the_picture(components())
    assert not missing, (
        f"{missing}: give each a node in FLOW_NODES (`covers`) or a line in NOT_DRAWN "
        "with the reason (docs/15, Keeping them current)"
    )


def test_an_adapter_package_counts_as_a_component(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapters = tmp_path / "api" / "telephony" / "adapters"
    (adapters / "vendor").mkdir(parents=True)
    (adapters / "vendor" / "__init__.py").write_text("")
    (adapters / "asterisk.py").write_text("")
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")
    monkeypatch.setattr(ra, "ROOT", tmp_path)
    assert {"adapter:vendor", "adapter:asterisk"} <= components()


def test_a_new_tool_service_or_adapter_is_caught() -> None:
    found = components() | {"tool:refund", "service:cache", "adapter:stt_other"}
    # A new ear matches the ear's pattern; the rest has no node.
    assert not_in_the_picture(found) == ["service:cache", "tool:refund"]


def test_no_cover_and_no_reason_outlives_its_component() -> None:
    found = components()
    stale = [c for c in ra.NOT_DRAWN if c not in found]
    dead = [
        p
        for node in ra.FLOW_NODES.values()
        for p in node.get("covers", [])
        if not any(fnmatch(c, p) for c in found)
    ]
    assert not stale and not dead, (stale, dead)
    assert all(reason.strip() for reason in ra.NOT_DRAWN.values())


# --- Codex round 1 on fa42b15 ----------------------------------------------------


def test_two_drill_runs_of_one_day_with_the_same_result_stay_two_rows(
    recording: dict,
) -> None:
    first = ra.kit_rows("drill", [{"point": "a", "ok": True}], RUN_AT)
    later = ra.kit_rows("drill", [{"point": "a", "ok": True}], RUN_AT.replace(hour=23))
    ra.record_measurements(first)
    ra.record_measurements(first)
    ra.record_measurements(later)
    assert len(ra.read_voice_history(ra.VOICE_HISTORY)) == 2


def test_overlapping_runs_each_get_recorded(recording: dict) -> None:
    """A slow run that started first finishes last: its report is recorded too."""
    folder = recording["reports"]
    write_report(folder, "eval_20261015_100000_000001.json")
    write_report(
        folder, "eval_20261015_100500_000001.json", run_at="2026-10-15T10:05:00+00:00"
    )
    ra.auto_record("cases", 0, folder)
    assert len(ra.read_eval_history(ra.EVAL_HISTORY)) == 2


def test_the_main_folder_is_the_one_with_main_checked_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    listing = (
        f"worktree {tmp_path / 'primary'}\nHEAD 1\nbranch refs/heads/task/x\n\n"
        f"worktree {tmp_path / 'linked'}\nHEAD 2\nbranch refs/heads/main\n\n"
        f"worktree {tmp_path / 'other'}\nHEAD 3\ndetached\n"
    )
    monkeypatch.setattr(ra, "git", lambda *args, root=ra.ROOT: listing)
    assert ra.report_folders(tmp_path / "primary") == [
        tmp_path / "primary" / "evals" / "reports",
        tmp_path / "linked" / "evals" / "reports",
    ]


def test_a_tagged_run_on_main_names_no_command_and_add_refuses_main(
    recording: dict,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(ra, "recording_branch", lambda root=ra.ROOT: None)
    path = write_report(
        recording["reports"], "eval_20261015_100000_000001.json", tags=["menu"]
    )
    ra.auto_record("cases", 0, recording["reports"])
    assert "add" not in capsys.readouterr().out
    assert ra.main(["add", str(path), "--suite", "menu"]) == 1
    assert ra.main(["add-voice", "2026-10-15", "T-1.11", "m", "1", "u", "go"]) == 1
    assert not ra.EVAL_HISTORY.exists() and not ra.VOICE_HISTORY.exists()


def test_a_kit_on_main_waits_for_the_recorder_holding_the_lock(
    recording: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex on f25cb90: writer and recorder share one lock, so no row is lost."""
    monkeypatch.setattr(ra, "recording_branch", lambda root=ra.ROOT: None)
    folder = recording["reports"]
    kit = threading.Thread(target=ra.record_measurements, args=([drill_row()], folder))
    with ra.pending_lock(folder):
        kit.start()
        kit.join(0.3)
        assert kit.is_alive() and not (folder / ra.VOICE_PENDING).exists()
    kit.join(5)
    assert not kit.is_alive()
    _, rows = ra.pending([folder], ra.EVAL_HISTORY, ra.VOICE_HISTORY)
    assert rows == [drill_row()]


def test_pending_record_holds_the_lock_while_it_reads(
    recording: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = recording["reports"]
    ra.append_row(folder / ra.VOICE_PENDING, ra.VOICE_FIELDS, drill_row())
    held: list[bool] = []
    real_record_voice = ra.record_voice

    def while_recording(row: dict[str, str], path: Path = ra.VOICE_HISTORY) -> str:
        def take_the_lock() -> None:
            with ra.pending_lock(folder):
                pass

        probe = threading.Thread(target=take_the_lock, daemon=True)
        probe.start()
        probe.join(0.3)
        held.append(probe.is_alive())
        return real_record_voice(row, path)

    monkeypatch.setattr(ra, "record_voice", while_recording)
    ra.record_pending([folder])
    assert held == [True]


def test_voice_runs_sort_by_their_utc_time_across_the_repeated_hour(
    recording: dict,
) -> None:
    summer = datetime(2026, 10, 25, 0, 30, tzinfo=ra.UTC)  # 02:30 CEST
    winter = datetime(2026, 10, 25, 1, 15, tzinfo=ra.UTC)  # 02:15 CET, later
    ra.record_measurements(ra.kit_rows("drill", [{"point": "a", "ok": False}], winter))
    ra.record_measurements(ra.kit_rows("drill", [{"point": "a", "ok": True}], summer))
    runs = ra.read_voice_history(ra.VOICE_HISTORY)
    assert [r.verdict for r in runs] == ["go", "no-go"]
    assert {r.date for r in runs} == {date(2026, 10, 25)}


# --- Codex round 4 on fca329a ----------------------------------------------------


def test_recording_by_hand_redraws_the_images(recording: dict) -> None:
    path = write_report(recording["reports"], "eval_20261015_100000_000001.json")
    assert ra.main(["add", str(path)]) == 0
    assert ra.main(["add-voice", "2026-10-15", "T-1.11", "m", "1", "u", "go"]) == 0
    assert recording["renders"] == 2
    assert len(ra.read_eval_history(ra.EVAL_HISTORY)) == 1


def test_a_second_recorder_waits_until_the_first_has_drawn(
    recording: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Record and render are one step under the history lock."""
    write_report(recording["reports"], "eval_20261015_100000_000001.json")
    held: list[bool] = []

    def render() -> int:
        def take_the_lock() -> None:
            with ra.file_lock(ra.REPORTS / "history.lock"):
                pass

        probe = threading.Thread(target=take_the_lock, daemon=True)
        probe.start()
        probe.join(0.3)
        held.append(probe.is_alive())
        return 0

    monkeypatch.setattr(ra, "render", render)
    ra.auto_record("cases", 0, recording["reports"])
    assert held == [True]


# --- Codex round 5 on edb0de3 ----------------------------------------------------


def test_a_speech_trial_with_any_limit_records_nothing(
    recording: dict, tmp_path: Path
) -> None:
    run = tmp_path / "run"
    run.mkdir()
    (run / "report.json").write_text(ra.json.dumps(speech_report()))
    ra.record_kit_run("speech", ["--limit", "10000", "--run-dir", str(run)], run, 0)
    assert not ra.VOICE_HISTORY.exists()


def test_two_kits_ending_in_the_same_second_stay_two_rows(recording: dict) -> None:
    point = [{"point": "a", "ok": True}]
    ra.record_measurements(ra.kit_rows("drill", point, RUN_AT.replace(microsecond=1)))
    ra.record_measurements(ra.kit_rows("drill", point, RUN_AT.replace(microsecond=2)))
    assert len(ra.read_voice_history(ra.VOICE_HISTORY)) == 2


# --- the maintenance bot reports the images (T-0.11) ------------------------------


@pytest.fixture
def assets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for path in ra.ASSETS.iterdir():
        if path.is_file():
            (tmp_path / path.name).write_bytes(path.read_bytes())
    monkeypatch.setattr(ra, "ASSETS", tmp_path)
    return tmp_path


def test_nothing_open_still_names_the_heading(assets: Path) -> None:
    assert ra.mark_uploaded() == 0
    assert ra.image_findings() == {ra.IMAGES_HEADING: []}


def test_a_stale_svg_waits_for_the_weekly_update(assets: Path) -> None:
    ra.mark_uploaded()
    (assets / "progress.svg").write_text("<svg/>", encoding="utf-8")
    assert ra.image_findings() == {ra.IMAGES_HEADING: []}


def test_a_new_social_preview_is_reported_until_it_is_marked_uploaded(
    assets: Path,
) -> None:
    (assets / ra.UPLOADED).unlink(missing_ok=True)
    (social,) = ra.image_findings()[ra.IMAGES_HEADING]
    assert (
        social["id"]
        == f"social-preview.png:{ra.file_digest(assets / 'social-preview.png')}"
    )
    assert "mark-uploaded" in social["label"]
    ra.mark_uploaded()
    assert ra.image_findings()[ra.IMAGES_HEADING] == []
    # A PNG drawn again is a new upload, and also a PNG CI cannot vouch for.
    (assets / "social-preview.png").write_bytes(b"another png")
    ids = [e["id"] for e in ra.image_findings()[ra.IMAGES_HEADING]]
    assert f"social-preview.png:{ra.file_digest(assets / 'social-preview.png')}" in ids
    assert any(
        "render --png" in e["label"] for e in ra.image_findings()[ra.IMAGES_HEADING]
    )


def test_mark_uploaded_needs_the_png(assets: Path) -> None:
    (assets / "social-preview.png").unlink()
    # The copy carries the committed record of the upload; it must not be left.
    (assets / ra.UPLOADED).unlink(missing_ok=True)
    assert ra.mark_uploaded() == 1
    assert not (assets / ra.UPLOADED).exists()


def test_mark_uploaded_refuses_main(
    assets: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ra, "recording_branch", lambda root=ra.ROOT: None)
    (assets / ra.UPLOADED).unlink(missing_ok=True)
    assert ra.main(["mark-uploaded"]) == 1
    assert not (assets / ra.UPLOADED).exists()
