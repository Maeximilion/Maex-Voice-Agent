"""Tests fuer Assignee und Label der Pull Requests (scripts/pr_metadata.py).

Geprueft wird die Ableitung ohne Netz: welches Label aus dem Titel folgt und was
an einem Pull Request fehlt.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_MODULPFAD = Path(__file__).resolve().parents[2] / "scripts" / "pr_metadata.py"
_spec = importlib.util.spec_from_file_location("pr_metadata", _MODULPFAD)
assert _spec and _spec.loader
pr_metadata = importlib.util.module_from_spec(_spec)
sys.modules["pr_metadata"] = pr_metadata
_spec.loader.exec_module(pr_metadata)

EIGENTUEMER = "Maeximilion"


def pull(titel: str, *, labels=(), assignees=()) -> dict:
    return {
        "number": 1,
        "title": titel,
        "labels": [{"name": name} for name in labels],
        "assignees": [{"login": login} for login in assignees],
    }


@pytest.mark.parametrize(
    ("titel", "label"),
    [
        ("feat(project): etwas Neues", "feature"),
        ("fix(gui): Knopf repariert", "bug"),
        ("docs(status): Stand nachgezogen", "docs"),
        ("chore(ci): Aktion aktualisiert", "chore"),
        ("refactor: Logger vereinfacht", "refactor"),
        ("test(evals): neue Faelle", "test"),
        ("ci: Cache", "chore"),
        ("feat!: bricht die Schnittstelle", "feature"),
    ],
)
def test_label_aus_conventional_commit(titel: str, label: str) -> None:
    assert pr_metadata.derive_label(titel) == label


@pytest.mark.parametrize(
    "titel",
    [
        "Merge pull request #119 from Maeximilion/fix/code-health",
        "Refactor configure_logging loop",  # kein Doppelpunkt-Praefix
        "wip: irgendwas",  # unbekannter Typ
    ],
)
def test_ohne_erkennbaren_typ_kein_label(titel: str) -> None:
    """Randfall: lieber kein Label als ein geratenes."""
    assert pr_metadata.derive_label(titel) is None


def test_fehlt_beides_wird_beides_gesetzt() -> None:
    aenderung = pr_metadata.missing(pull("feat(x): y"), EIGENTUEMER)
    assert aenderung == {"assignees": [EIGENTUEMER], "labels": ["feature"]}


def test_dependency_label_does_not_replace_the_type_label() -> None:
    """Adding chore uses the additive labels endpoint, preserving dependencies."""
    aenderung = pr_metadata.missing(
        pull("chore(deps): bump x", labels=["dependencies"]), EIGENTUEMER
    )
    assert aenderung == {"assignees": [EIGENTUEMER], "labels": ["chore"]}


def test_vollstaendiger_pr_braucht_nichts() -> None:
    """Idempotent: ein zweiter Lauf aendert nichts."""
    aenderung = pr_metadata.missing(
        pull("feat(x): y", labels=["feature"], assignees=[EIGENTUEMER]), EIGENTUEMER
    )
    assert aenderung == {}


def test_ohne_ableitbares_label_nur_assignee() -> None:
    aenderung = pr_metadata.missing(pull("Merge pull request #1"), EIGENTUEMER)
    assert aenderung == {"assignees": [EIGENTUEMER]}


def test_jedes_abgeleitete_label_existiert_im_repo() -> None:
    """Die Zuordnung darf nur auf Labels zeigen, die es gibt - sonst legt GitHub beim
    Setzen stillschweigend ein neues, graues Label an."""
    vorhanden = {"feature", "bug", "docs", "chore", "refactor", "test"}
    assert set(pr_metadata.TYPE_LABELS.values()) <= vorhanden


def test_an_issue_without_assignee_gets_the_owner_and_nothing_else() -> None:
    issue = pull("T-8.6 Backup and restore", labels=["stufe-5", "chore"])
    assert pr_metadata.missing_on_issue(issue, EIGENTUEMER) == {
        "assignees": [EIGENTUEMER]
    }
    # A recognizable title supplies a missing type on issues too.
    assert pr_metadata.missing_on_issue(pull("fix: x"), EIGENTUEMER) == {
        "assignees": [EIGENTUEMER],
        "labels": ["bug"],
    }
    done = pull("Daily code review", assignees=[EIGENTUEMER])
    assert pr_metadata.missing_on_issue(done, EIGENTUEMER) == {}


def test_owner_is_added_alongside_another_assignee() -> None:
    item = pull("fix: example", labels=["bug"], assignees=["contributor"])
    assert pr_metadata.missing(item, EIGENTUEMER) == {"assignees": [EIGENTUEMER]}
    item["assignees"].append({"login": EIGENTUEMER.lower()})
    assert pr_metadata.missing(item, EIGENTUEMER) == {}


def test_existing_type_and_legacy_enhancement_are_preserved() -> None:
    for label in ("feature", "enhancement"):
        item = pull("fix: example", labels=[label], assignees=[EIGENTUEMER])
        assert pr_metadata.missing(item, EIGENTUEMER) == {}


def test_closed_issues_are_included_in_the_metadata_run(monkeypatch) -> None:
    item = {**pull("fix: example"), "state": "closed", "milestone": None}
    calls = []
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setattr(sys, "argv", ["pr_metadata.py"])
    monkeypatch.setattr(pr_metadata, "all_issues", lambda *a: [item])
    monkeypatch.setattr(pr_metadata, "all_pulls", lambda *a: [])
    monkeypatch.setattr(pr_metadata, "pages", lambda *a: [])
    monkeypatch.setattr(pr_metadata.time, "sleep", lambda *a: None)
    monkeypatch.setattr(pr_metadata, "rest", lambda *a: calls.append(a))
    assert pr_metadata.main() == 0
    assert [c[1] for c in calls] == [
        "/repos/owner/repo/issues/1/assignees",
        "/repos/owner/repo/issues/1/labels",
    ]


@pytest.mark.parametrize(
    ("kind", "state", "merged_at", "labels", "expected_finding", "expected_writes"),
    [
        ("issue", "open", None, ["code-review"], False, 1),
        ("issue", "closed", None, ["code-review"], False, 1),
        ("issue", "closed", None, [], True, 1),
        ("pull", "closed", None, [], False, 0),
        ("pull", "closed", "2026-09-23T10:00:00Z", [], True, 1),
        ("pull", "open", None, [], True, 1),
    ],
)
def test_metadata_findings_ignore_review_parents_and_rejected_prs(
    monkeypatch,
    tmp_path,
    kind,
    state,
    merged_at,
    labels,
    expected_finding,
    expected_writes,
) -> None:
    item = {
        **pull("Unclassified work", labels=labels),
        "head": {"ref": "test-branch"},
        "state": state,
        "merged_at": merged_at,
        "milestone": None,
    }
    output = tmp_path / "findings.json"
    calls = []
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setattr(sys, "argv", ["pr_metadata.py", "--findings-json", str(output)])
    monkeypatch.setattr(
        pr_metadata, "all_issues", lambda *a: [item] if kind == "issue" else []
    )
    monkeypatch.setattr(
        pr_metadata, "all_pulls", lambda *a: [item] if kind == "pull" else []
    )
    monkeypatch.setattr(pr_metadata, "pages", lambda *a: [])
    monkeypatch.setattr(pr_metadata.time, "sleep", lambda *a: None)
    monkeypatch.setattr(pr_metadata, "rest", lambda *a: calls.append(a))
    assert pr_metadata.main() == 0
    findings = json.loads(output.read_text(encoding="utf-8"))["Repository metadata"]
    assert [finding["id"] for finding in findings] == (
        ["1"] if expected_finding else []
    )
    assert [call[1] for call in calls] == [
        "/repos/owner/repo/issues/1/assignees"
    ] * expected_writes


def test_all_issues_leave_out_pull_requests_over_every_page(monkeypatch) -> None:
    first: list[dict] = [{"number": n} for n in range(100)]
    first[0]["pull_request"] = {"url": "x"}
    calls = []

    def rest(method, path, token, payload=None):
        calls.append(path)
        return first if path.endswith("page=1") else [{"number": 100}]

    monkeypatch.setattr(pr_metadata, "rest", rest)
    issues = pr_metadata.all_issues("o/r", "t")
    assert [i["number"] for i in issues] == list(range(1, 101))
    assert calls == [
        "/repos/o/r/issues?state=all&per_page=100&page=1",
        "/repos/o/r/issues?state=all&per_page=100&page=2",
    ]


GATES = pr_metadata.gate_milestones(
    [
        {"number": 1, "title": "G0 Go/No-Go"},
        {"number": 2, "title": "G1 Reservation through-cut"},
        {"number": 9, "title": "Backlog ideas"},
    ]
)


def issue(*labels: str, milestone=None) -> dict:
    return {"labels": [{"name": n} for n in labels], "milestone": milestone}


def test_gates_are_matched_by_their_title_only() -> None:
    assert sorted(GATES) == ["stufe-0", "stufe-1"]


def test_an_issue_gets_the_gate_of_its_stage_label() -> None:
    assert pr_metadata.gate_of(issue("feature", "stufe-1"), GATES)["number"] == 2


def test_a_milestone_set_by_hand_stays() -> None:
    assert pr_metadata.gate_of(issue("stufe-1", milestone={"number": 1}), GATES) is None


def test_no_stage_label_or_no_such_gate_means_no_guess() -> None:
    assert pr_metadata.gate_of(issue("feature"), GATES) is None
    assert pr_metadata.gate_of(issue("stufe-7"), GATES) is None


def test_two_stage_labels_take_the_earlier_gate() -> None:
    assert pr_metadata.gate_of(issue("stufe-1", "stufe-0"), GATES)["number"] == 1


WORKPACKAGES = """## Block 1 - Stage 1  ·  Sammel-Issue #6

| ID | Issue | Task |
|---|---|---|
| T-1.1 | #30 | first |
| T-1.2 | - | no issue of its own |

## Block 1c - Stage 1  ·  group issue #394

| T-10.1 | #396 | spike |
"""
TASKS = pr_metadata.task_issues(WORKPACKAGES)
GATE_0 = {"number": 1, "title": "G0 Go/No-Go"}
GATE_1 = {"number": 2, "title": "G1 Reservation through-cut"}
GATE_2 = {"number": 3, "title": "G2 Pickup"}
ISSUE_GATES = {30: GATE_1, 6: GATE_2, 396: GATE_0}
BY_TITLE = {
    "Bugs": {"number": 7, "title": "Bugs"},
    "Features": {"number": 8, "title": "Features"},
    "Etcetera": {"number": 9, "title": "Etcetera"},
}


def pr(title: str, *, body=None, branch="fix/x", milestone=None) -> dict:
    return {
        "title": title,
        "body": body,
        "head": {"ref": branch},
        "milestone": milestone,
    }


def milestone(pull: dict) -> dict | None:
    return pr_metadata.milestone_of_pull(pull, ISSUE_GATES, TASKS, BY_TITLE)


def test_a_task_takes_its_own_issue_else_the_group_issue_of_its_block() -> None:
    assert TASKS == {"T-1.1": 30, "T-1.2": 6, "T-10.1": 396}


def test_a_milestone_on_a_pull_request_stays() -> None:
    assert milestone(pr("fix: x", milestone=GATE_1)) is None


def test_the_closed_issue_wins_over_the_named_task() -> None:
    assert milestone(pr("feat: T-1.1 x", body="Closes #396")) == GATE_0


@pytest.mark.parametrize("word", ["Closes", "closed", "Fixes", "fix", "resolves"])
def test_every_closing_keyword_counts(word: str) -> None:
    assert milestone(pr("chore: x", body=f"{word} #30")) == GATE_1


def test_a_mention_of_an_issue_is_no_closing() -> None:
    assert milestone(pr("chore: x", body="See #30")) == BY_TITLE["Etcetera"]


def test_a_task_counts_from_the_title_or_the_branch() -> None:
    assert milestone(pr("feat: T-10.1 spike")) == GATE_0
    assert milestone(pr("feat: spike", branch="task/10-1-spike")) == GATE_0
    assert milestone(pr("feat: spike", branch="task/T-1.2-x")) == GATE_2


def test_two_tasks_take_the_earlier_gate() -> None:
    assert milestone(pr("feat: T-1.1 and T-10.1")) == GATE_0


def test_a_closed_issue_without_a_gate_falls_through_to_the_task() -> None:
    assert milestone(pr("feat: T-1.1", body="Closes #999")) == GATE_1


@pytest.mark.parametrize(
    ("title", "wanted"),
    [
        ("feat(gui): x", "Features"),
        ("fix(gui): x", "Bugs"),
        ("chore(deps): bump x", "Etcetera"),
        ("docs: x", "Etcetera"),
        ("Merge something", "Etcetera"),
    ],
)
def test_without_a_gate_the_title_decides(title: str, wanted: str) -> None:
    assert milestone(pr(title)) == BY_TITLE[wanted]


def test_without_the_type_milestones_nothing_is_written() -> None:
    assert pr_metadata.milestone_of_pull(pr("fix: x"), ISSUE_GATES, TASKS, {}) is None


def test_the_real_task_list_is_parsed() -> None:
    tasks = pr_metadata.task_issues(pr_metadata.WORKPACKAGES.read_text("utf-8"))
    assert tasks["T-10.1"] == 396
    assert tasks["T-10.4"] == 394
