"""Work-package relationships use explicit issues, never guessed group dependencies."""

import pytest

from scripts import project_relationships as relationships

TABLE = """## Example · group issue #100
| ID | Issue | Task | Spec | Depends on | Status |
|---|---|---|---|---|---|
| T-1.1 | #101 | One | 01 | - | done |
| T-1.2 | #102 | Two | 01 | T-1.1 | done |
| T-1.3 | #103 | Three | 01 | T-1.1…T-1.2 | open |
| T-1.4 | - | Four | 01 | T-1.3 | open |
"""


def issue(number, *, state="OPEN", parent=None, blockers=(), reason=None):
    return {
        "id": f"I{number}",
        "number": number,
        "url": f"https://example.com/{number}",
        "state": state,
        "stateReason": reason,
        "parent": parent,
        "blockedBy": {"nodes": [{"id": f"I{n}"} for n in blockers]},
    }


def test_ranges_parents_and_idempotence():
    tasks = relationships.parse_tasks(TABLE)
    issues = {n: issue(n) for n in (100, 101, 102, 103)}
    changes, findings = relationships.plan(tasks, issues)
    assert not findings
    assert set(changes) == {
        ("parent", 101, 100),
        ("parent", 102, 100),
        ("parent", 103, 100),
        ("blocked_by", 102, 101),
        ("blocked_by", 103, 101),
        ("blocked_by", 103, 102),
    }
    for kind, source, target in changes:
        if kind == "parent":
            issues[source]["parent"] = {"id": f"I{target}"}
        else:
            issues[source]["blockedBy"]["nodes"].append({"id": f"I{target}"})
    assert relationships.plan(tasks, issues) == ([], {})


def test_manual_parent_conflict_and_cycle_are_reported():
    issues = {n: issue(n) for n in (100, 101, 102, 103)}
    issues[101]["parent"] = {"id": "manual-parent"}
    issues[101]["blockedBy"]["nodes"] = [{"id": "I102"}]
    changes, findings = relationships.plan(relationships.parse_tasks(TABLE), issues)
    assert ("parent", 101, 100) not in changes
    assert ("blocked_by", 102, 101) not in changes
    assert "Existing parent differs from work package" in findings
    assert "Relationship would create a cycle" in findings


def test_waived_dependency_and_not_planned_work_are_not_added():
    text = TABLE.replace("T-1.1 | done", "T-1.1 | done (started without T-1.1)")
    issues = {n: issue(n) for n in (100, 101, 102, 103)}
    issues[102]["state"] = "CLOSED"
    issues[103].update(state="CLOSED", stateReason="NOT_PLANNED")
    changes, findings = relationships.plan(relationships.parse_tasks(text), issues)
    assert ("blocked_by", 102, 101) not in changes
    assert not any(source == 103 for _, source, _ in changes)
    assert not findings


def test_closed_issue_is_not_newly_blocked_by_open_work():
    issues = {n: issue(n) for n in (100, 101, 102, 103)}
    issues[102]["state"] = "CLOSED"
    changes, findings = relationships.plan(relationships.parse_tasks(TABLE), issues)
    assert ("blocked_by", 102, 101) not in changes
    assert "Completed issue has an unfinished prerequisite" in findings


@pytest.mark.parametrize(
    "text",
    [
        TABLE + "\n| T-1.1 | #104 | Duplicate | 01 | - | open |",
        TABLE.replace("T-1.1…T-1.2", "T-1.1…T-2.2"),
        TABLE.replace("T-1.1…T-1.2", "T-1.9"),
    ],
)
def test_ambiguous_source_fails_before_any_write(text):
    with pytest.raises(relationships.ProjectError):
        relationships.parse_tasks(text)
