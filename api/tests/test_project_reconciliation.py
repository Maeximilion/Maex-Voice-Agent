"""Exercise API writes, pagination and partial-report ownership without a network."""

import json
import sys
from datetime import UTC, datetime

import pytest

from scripts import pr_metadata, project_relationships, project_report


@pytest.mark.parametrize("fix", [False, True])
def test_historical_intake_is_explicit_and_idempotent(monkeypatch, fix):
    item = project_report.Item(
        node_id="",
        content_id="CONTENT",
        title="Completed issue",
        number=101,
        kind="Issue",
        state="CLOSED",
        updated_at=datetime(2026, 9, 22, tzinfo=UTC),
        url="https://github.com/owner/repo/issues/101",
    )
    calls = []

    def graphql(query, variables, token):
        calls.append((query, variables))
        if "mutation" in query:
            return {
                "addProjectV2ItemById": {"item": {"id": "NEW", "isArchived": False}}
            }
        return {
            "node": {"projectItems": {"nodes": [], "pageInfo": {"hasNextPage": False}}}
        }

    monkeypatch.setattr(project_report, "graphql", graphql)
    monkeypatch.setattr(project_report.time, "sleep", lambda _: None)
    board = []
    added = project_report.reconcile_history("PROJECT", board, [item], "token", fix=fix)
    assert len(added) == int(fix)
    assert len(calls) == 1 + int(fix)
    if fix:
        assert calls[-1][1] == {"projectId": "PROJECT", "contentId": "CONTENT"}
        assert (
            project_report.reconcile_history(
                "PROJECT", board, [item], "token", fix=True
            )
            == []
        )
        assert len(calls) == 2


def test_late_page_of_linked_prs_can_contain_the_first_implementation(monkeypatch):
    content = {
        "id": "ISSUE",
        "__typename": "Issue",
        "state": "CLOSED",
        "closedByPullRequestsReferences": {
            "nodes": [{"state": "MERGED", "createdAt": "2026-11-02T10:00:00Z"}],
            "pageInfo": {"hasNextPage": True, "endCursor": "NEXT"},
        },
    }

    def graphql(query, variables, token):
        assert variables == {"id": "ISSUE", "cursor": "NEXT"}
        return {
            "node": {
                "closedByPullRequestsReferences": {
                    "nodes": [{"state": "MERGED", "createdAt": "2026-10-12T10:00:00Z"}],
                    "pageInfo": {"hasNextPage": False},
                }
            }
        }

    monkeypatch.setattr(project_report, "graphql", graphql)
    project_report.complete_closing_refs(content, "token")
    item = project_report.parse_item(
        {"id": "ITEM", "updatedAt": "2026-11-02T12:00:00Z", "content": content}
    )
    assert item.work_started == datetime(2026, 10, 12, 10, tzinfo=UTC)


@pytest.mark.parametrize("fix", [False, True])
def test_relationship_cli_uses_additive_mutations_and_owned_report(
    monkeypatch, tmp_path, fix
):
    source = {
        "id": "CHILD",
        "number": 101,
        "state": "OPEN",
        "stateReason": None,
        "url": "https://example.com/101",
        "parent": None,
        "blockedBy": {"nodes": []},
    }
    target = {**source, "id": "PARENT", "number": 100}
    issues = {100: target, 101: source}
    tasks = {"T-1.1": project_relationships.Task(101, 100, frozenset(), frozenset())}
    output = tmp_path / "relationships.json"
    calls = []
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setattr(
        sys,
        "argv",
        ["relationships", "--findings-json", str(output), *(["--fix"] if fix else [])],
    )
    monkeypatch.setattr(project_relationships, "parse_tasks", lambda _: tasks)
    monkeypatch.setattr(project_relationships, "fetch_issues", lambda *a: issues)
    monkeypatch.setattr(project_relationships, "graphql", lambda *a: calls.append(a))
    assert project_relationships.main() == 0
    assert len(calls) == int(fix)
    if fix:
        assert "replaceParent" not in calls[0][0]
        assert calls[0][1] == {"source": "CHILD", "target": "PARENT"}
        assert "issueId: $target, subIssueId: $source" in calls[0][0]
    assert json.loads(output.read_text()) == {"Work-package relationships": []}


def test_unknown_type_is_reported_without_a_guessed_label(monkeypatch, tmp_path):
    item = {
        "number": 101,
        "title": "A decision",
        "labels": [],
        "assignees": [{"login": "owner"}],
        "milestone": None,
        "html_url": "https://example.com/101",
    }
    output = tmp_path / "metadata.json"
    calls = []
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setattr(
        sys, "argv", ["metadata", "--dry-run", "--findings-json", str(output)]
    )
    monkeypatch.setattr(pr_metadata, "all_issues", lambda *a: [item])
    monkeypatch.setattr(pr_metadata, "all_pulls", lambda *a: [])
    monkeypatch.setattr(pr_metadata, "pages", lambda *a: [])
    monkeypatch.setattr(pr_metadata, "rest", lambda *a: calls.append(a))
    assert pr_metadata.main() == 0
    assert not calls
    findings = json.loads(output.read_text())["Repository metadata"]
    assert len(findings) == 1 and "Type label needs a decision" in findings[0]["label"]


def test_relationship_failure_does_not_publish_an_empty_success_report(
    monkeypatch, tmp_path
):
    output = tmp_path / "relationships.json"
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setattr(sys, "argv", ["relationships", "--findings-json", str(output)])

    def fail(*args):
        raise project_relationships.ProjectError("API unavailable")

    monkeypatch.setattr(project_relationships, "fetch_issues", fail)
    assert project_relationships.main() == 1
    assert not output.exists()
