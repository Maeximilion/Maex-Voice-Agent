"""The generator of the README images (`scripts/readme_assets.py`)."""

from __future__ import annotations

import csv
import itertools
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
    real = ra.ASSETS
    for name in (*ra.PNGS, ra.PNG_SOURCES):
        (tmp_path / name).write_bytes((real / name).read_bytes())
    monkeypatch.setattr(ra, "ASSETS", tmp_path)
    assert ra.render() == 0
    assert ra.render(check=True) == 0
    (tmp_path / "progress.svg").write_text("<svg/>", encoding="utf-8")
    assert ra.render(check=True) == 1


def test_committed_assets_match_their_data() -> None:
    """The same check CI runs; a failure here means `make readme-assets`."""
    assert ra.render(check=True) == 0


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
