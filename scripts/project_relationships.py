"""Mirror explicit work-package parents and prerequisites; never remove relationships.

Run `python -m scripts.project_relationships` for a read-only plan, or add --fix.
Uses GITHUB_TOKEN (repository Issues write) and GITHUB_REPOSITORY.
Conflicts go to --findings-json for the existing maintenance decision issue.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from scripts.pr_metadata import WORKPACKAGES
from scripts.project_report import ProjectError, graphql

TASK = re.compile(r"T-\d+\.\d+")
RANGE = re.compile(r"T-(\d+)\.(\d+)\s*(?:…|\.{2,3}|\u2013|-)\s*T-(\d+)\.(\d+)")
GROUP = re.compile(r"(?:Sammel-Issue|group issue) #(\d+)", re.I)

ISSUES_QUERY = """
query($owner: String!, $name: String!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    issues(first: 100, after: $cursor) {
      pageInfo { hasNextPage endCursor }
      nodes {
        id number url state stateReason parent { id }
        blockedBy(first: 100) {
          pageInfo { hasNextPage endCursor }
          nodes { id }
        }
      }
    }
  }
}
"""
BLOCKERS_QUERY = """
query($id: ID!, $cursor: String) {
  node(id: $id) { ... on Issue { blockedBy(first: 100, after: $cursor) {
    pageInfo { hasNextPage endCursor }
    nodes { id }
  } } }
}
"""
MUTATIONS = {
    "parent": """mutation($source: ID!, $target: ID!) {
      addSubIssue(input: {issueId: $target, subIssueId: $source}) { clientMutationId }
    }""",
    "blocked_by": """mutation($source: ID!, $target: ID!) {
      addBlockedBy(input: {issueId: $source, blockingIssueId: $target}) { clientMutationId }
    }""",
}


@dataclass(frozen=True)
class Task:
    issue: int | None
    parent: int | None
    dependencies: frozenset[str]
    waived: frozenset[str]


def parse_tasks(text: str) -> dict[str, Task]:
    """Only explicit issue numbers count; a task without one is not its group."""
    tasks: dict[str, Task] = {}
    parent = None
    seen_issues: set[int] = set()
    for line in text.splitlines():
        if line.startswith("## "):
            match = GROUP.search(line)
            parent = int(match[1]) if match else None
        if not re.match(r"^\| T-\d+\.\d+\s*\|", line):
            continue
        cells = [cell.strip() for cell in re.split(r"(?<!\\)\|", line)[1:-1]]
        if len(cells) != 6:
            raise ProjectError(f"Malformed work-package row: {cells[0]}")
        task, issue_text, _, _, dependencies, status = cells
        if task in tasks or not re.fullmatch(r"#\d+|-", issue_text):
            raise ProjectError(f"Ambiguous work-package mapping: {task}")
        issue = int(issue_text[1:]) if issue_text.startswith("#") else None
        if issue in seen_issues:
            raise ProjectError(f"Issue #{issue} belongs to multiple tasks")
        if issue is not None:
            seen_issues.add(issue)

        def expand(match: re.Match, task_name: str = task) -> str:
            block, start, end_block, end = map(int, match.groups())
            if block != end_block or end < start or end - start > 100:
                raise ProjectError(f"Ambiguous dependency range in {task_name}")
            return ", ".join(f"T-{block}.{n}" for n in range(start, end + 1))

        deps = frozenset(TASK.findall(RANGE.sub(expand, dependencies)))
        waived = frozenset(re.findall(r"\bwithout\s+(T-\d+\.\d+)\b", status))
        tasks[task] = Task(issue, parent, deps, waived)
    if not tasks:
        raise ProjectError(
            "No work-package rows found; refusing an empty synchronization"
        )
    for name, task in tasks.items():
        if unknown := task.dependencies - tasks.keys():
            raise ProjectError(
                f"Unknown prerequisites of {name}: {', '.join(sorted(unknown))}"
            )
    return tasks


def fetch_issues(repo: str, token: str) -> dict[int, dict]:
    owner, _, name = repo.partition("/")
    if not owner or not name:
        raise ProjectError("GITHUB_REPOSITORY must be owner/name")
    issues = {}
    cursor = None
    while True:
        repository = graphql(
            ISSUES_QUERY, {"owner": owner, "name": name, "cursor": cursor}, token
        ).get("repository")
        if not repository:
            raise ProjectError(f"Repository {repo} is not readable")
        page = repository["issues"]
        for issue in page["nodes"]:
            blockers = issue["blockedBy"]
            info = blockers["pageInfo"]
            while info["hasNextPage"]:
                more = graphql(
                    BLOCKERS_QUERY,
                    {"id": issue["id"], "cursor": info["endCursor"]},
                    token,
                )["node"]["blockedBy"]
                blockers["nodes"].extend(more["nodes"])
                info = more["pageInfo"]
            issues[issue["number"]] = issue
        if not page["pageInfo"]["hasNextPage"]:
            return issues
        cursor = page["pageInfo"]["endCursor"]


def reaches(graph: dict[str, set[str]], source: str, target: str) -> bool:
    pending = [source]
    seen = set()
    while pending:
        current = pending.pop()
        if current == target:
            return True
        if current not in seen:
            seen.add(current)
            pending.extend(graph.get(current, set()))
    return False


def plan(
    tasks: dict[str, Task], issues: dict[int, dict]
) -> tuple[list[tuple[str, int, int]], dict]:
    """Plan additive changes against existing and already-planned edges."""
    changes: list[tuple[str, int, int]] = []
    findings: dict[str, list[dict]] = {}
    graphs = {
        "parent": {
            i["id"]: {i["parent"]["id"]} if i["parent"] else set()
            for i in issues.values()
        },
        "blocked_by": {
            i["id"]: {b["id"] for b in i["blockedBy"]["nodes"]} for i in issues.values()
        },
    }

    def report(reason: str, source: int, target: int) -> None:
        findings.setdefault(reason, []).append(
            {
                "id": f"{source}:{target}",
                "label": f"#{source} -> #{target}",
                "url": issues.get(source, {}).get("url"),
            }
        )

    for task in tasks.values():
        if task.issue is None:
            continue
        source = issues.get(task.issue)
        if source and source.get("stateReason") == "NOT_PLANNED":
            continue
        wanted: list[tuple[str, int | None]] = (
            [("parent", task.parent)] if task.parent else []
        )
        wanted += [
            ("blocked_by", tasks[dep].issue)
            for dep in sorted(task.dependencies - task.waived)
            if tasks[dep].issue
        ]
        for kind, number in wanted:
            assert number is not None
            target = issues.get(number)
            if source is None or target is None:
                report(
                    "Work-package issue is missing or unreadable", task.issue, number
                )
                continue
            graph = graphs[kind]
            if target["id"] in graph[source["id"]]:
                continue
            if kind == "parent" and source["parent"]:
                report("Existing parent differs from work package", task.issue, number)
                continue
            if kind == "blocked_by" and (
                target.get("stateReason") == "NOT_PLANNED"
                or (source["state"] == "CLOSED" and target["state"] == "OPEN")
            ):
                report(
                    "Completed issue has an unfinished prerequisite", task.issue, number
                )
                continue
            if reaches(graph, target["id"], source["id"]):
                report("Relationship would create a cycle", task.issue, number)
                continue
            changes.append((kind, task.issue, number))
            graph[source["id"]].add(target["id"])
    return changes, findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fix", action="store_true")
    parser.add_argument("--findings-json", type=Path)
    args = parser.parse_args()
    token = os.environ.get("GITHUB_TOKEN", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not token or not repo:
        print("GITHUB_TOKEN and GITHUB_REPOSITORY are required", file=sys.stderr)
        return 2
    try:
        tasks = parse_tasks(WORKPACKAGES.read_text(encoding="utf-8"))
        issues = fetch_issues(repo, token)
        changes, findings = plan(tasks, issues)
        for kind, source, target in changes:
            print(
                f"{'Add' if args.fix else 'Would add'} {kind}: #{source} -> #{target}"
            )
            if args.fix:
                graphql(
                    MUTATIONS[kind],
                    {"source": issues[source]["id"], "target": issues[target]["id"]},
                    token,
                )
        if args.findings_json:
            args.findings_json.write_text(
                json.dumps(
                    {
                        "Work-package relationships": [
                            {**item, "label": f"{reason}: {item['label']}"}
                            for reason, found in findings.items()
                            for item in found
                        ]
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
        print(
            f"{len(changes)} relationships; {sum(map(len, findings.values()))} conflicts."
        )
        for heading, found in findings.items():
            for item in found:
                print(f"{heading}: {item['label']}")
    except (ProjectError, OSError) as exc:
        print(f"Relationship reconciliation failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
