"""The generator of the README images (`scripts/readme_assets.py`)."""

from __future__ import annotations

import csv
import xml.dom.minidom
from datetime import date
from pathlib import Path

import pytest

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
            {"passed": True, "error": None, "pending": None},
            {"passed": True, "error": None, "pending": None},
            {"passed": False, "error": "Timeout", "pending": None},
            {"passed": False, "error": None, "pending": "T-6.5"},
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
    for a, b, _t0, _color in ra.FLOW_EDGES:
        assert a in ra.FLOW_NODES and b in ra.FLOW_NODES


def test_read_status_finds_stage_and_gates() -> None:
    stage, gates = ra.read_status(STATUS)
    assert stage == "Stage 0 (Foundation)"
    assert [(g.key, g.passed) for g in gates] == [
        ("P", True),
        ("G0", False),
        ("G1", False),
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
    monkeypatch.setattr(ra, "ASSETS", tmp_path)
    assert ra.render() == 0
    assert ra.render(check=True) == 0
    (tmp_path / "progress.svg").write_text("<svg/>", encoding="utf-8")
    assert ra.render(check=True) == 1


def test_committed_assets_match_their_data() -> None:
    """The same check CI runs; a failure here means `make readme-assets`."""
    assert ra.render(check=True) == 0
