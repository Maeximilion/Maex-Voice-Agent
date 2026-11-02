#!/usr/bin/env python3
"""Assignee und Label fuer Pull Requests nachtragen, wo sie fehlen.

Zweck : Jeder Pull Request bekommt den Repo-Eigentuemer als Assignee und ein
        Typ-Label aus seinem Titel (Conventional Commits: feat -> feature,
        fix -> bug, ...). Das deckt auch alte Pull Requests und solche von Bots ab.
        Gesetzt wird nur, was fehlt; vorhandene Labels bleiben, ein zweiter Lauf
        aendert nichts.
        Every issue, open or closed, gets the owner alongside existing assignees.
        A missing type is derived from a conventional title even when other
        labels exist; existing types, including enhancement, stay. Ambiguous
        types go to --findings-json. Every issue without a milestone gets the
        gate of its stufe-N label (stufe-1 -> "G1 ..."), so the roadmap can group
        by gate (Maxi, 2026-11-02). A milestone set by hand stays. A pull request
        takes the gate of the issue it closes, else of the task it names
        (docs/07_WORKPACKAGES.md), else Bugs, Features or Etcetera by its title.
Aufruf: python scripts/pr_metadata.py [--dry-run]
Umgeb.: GITHUB_TOKEN       Token mit Schreibrecht auf Issues und Pull Requests;
                           in der Action der eingebaute
        GITHUB_REPOSITORY  owner/name, in einer Action automatisch gesetzt
Abhaeng.: nur Standardbibliothek.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

API_URL = "https://api.github.com"

# Conventional-Commit-Typ -> vorhandenes Typ-Label des Repos. Nur Labels, die es gibt:
# ein unbekanntes legte GitHub beim Setzen stillschweigend neu an.
TYPE_LABELS = {
    "feat": "feature",
    "fix": "bug",
    "docs": "docs",
    "chore": "chore",
    "ci": "chore",
    "build": "chore",
    "refactor": "refactor",
    "perf": "refactor",
    "test": "test",
}
TYPE_PREFIX = re.compile(r"^(?P<type>[a-z]+)(\([^)]*\))?!?:")
KNOWN_TYPES = frozenset(TYPE_LABELS.values()) | {"enhancement"}
GATE_TITLE = re.compile(r"^G(?P<stage>\d+)\b")

WORKPACKAGES = Path(__file__).resolve().parent.parent / "docs/07_WORKPACKAGES.md"
TASK_ROW = re.compile(r"^\| (?P<task>T-\d+\.\d+) \| (?:#(?P<issue>\d+)|-) \|")
GROUP_ISSUE = re.compile(r"^## .*(?:Sammel-Issue|group issue) #(?P<issue>\d+)")
CLOSING = re.compile(r"(?i)\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)")
TASK_IN_TITLE = re.compile(r"\bT-\d+\.\d+\b")
TASK_BRANCH = re.compile(r"^task/(?:T-)?(?P<block>\d+)[-.](?P<task>\d+)\b")
# What names no gate. A title without a known type lands in the last one.
TYPE_MILESTONES = {"feat": "Features", "fix": "Bugs"}
OTHER_MILESTONE = "Etcetera"
# GitHub's limit for writes in a burst is 80 a minute; a run over it fails halfway.
WRITE_PAUSE_SECONDS = 1


class MetadataError(RuntimeError):
    """GitHub liess sich nicht lesen oder schreiben."""


def derive_label(title: str) -> str | None:
    """Das Typ-Label aus dem Titel. None, wenn der Titel keinen bekannten Typ traegt -
    lieber kein Label als ein geratenes."""
    match = TYPE_PREFIX.match(title.strip())
    return TYPE_LABELS.get(match.group("type")) if match else None


def missing(pull: dict, owner: str) -> dict[str, list[str]]:
    """Was an einem Pull Request fehlt. Leer, wenn nichts zu tun ist."""
    change: dict[str, list[str]] = {}
    if owner.casefold() not in {
        assignee["login"].casefold() for assignee in pull.get("assignees", [])
    }:
        change["assignees"] = [owner]
    labels = {label["name"] for label in pull.get("labels", [])}
    if not labels & KNOWN_TYPES and (label := derive_label(pull["title"])):
        change["labels"] = [label]
    return change


def missing_on_issue(issue: dict, owner: str) -> dict[str, list[str]]:
    """Fill only an absent owner or a type evidenced by a conventional title."""
    return missing(issue, owner)


def gate_milestones(milestones: list[dict]) -> dict[str, dict]:
    """The gate milestones by the stufe-N label they belong to."""
    return {
        f"stufe-{match['stage']}": milestone
        for milestone in milestones
        if (match := GATE_TITLE.match(milestone["title"]))
    }


def gate_of(issue: dict, gates: dict[str, dict]) -> dict | None:
    """The milestone an issue lacks, None if it has one or carries no stufe-N label.
    With two stufe labels the earlier gate wins: that is the one it holds up."""
    if issue.get("milestone"):
        return None
    labels = sorted(label["name"] for label in issue.get("labels", []))
    return next((gates[name] for name in labels if name in gates), None)


def task_issues(text: str) -> dict[str, int]:
    """The issue of each task in docs/07: its own, else the group issue of its block."""
    tasks: dict[str, int] = {}
    group = None
    for line in text.splitlines():
        if heading := GROUP_ISSUE.match(line):
            group = int(heading["issue"])
        elif row := TASK_ROW.match(line):
            issue = int(row["issue"]) if row["issue"] else group
            if issue:
                tasks[row["task"]] = issue
    return tasks


def named_tasks(pull: dict) -> set[str]:
    """The tasks in the title, else the task in the branch name."""
    tasks = set(TASK_IN_TITLE.findall(pull["title"]))
    branch = TASK_BRANCH.match(pull["head"]["ref"])
    return tasks or ({f"T-{branch['block']}.{branch['task']}"} if branch else set())


def milestone_of_pull(
    pull: dict,
    issue_gates: dict[int, dict],
    tasks: dict[str, int],
    milestones: dict[str, dict],
) -> dict | None:
    """The milestone a pull request lacks, None if it has one. The earliest gate wins,
    as for an issue. Missing type milestones mean no write, not a guess."""
    if pull.get("milestone"):
        return None
    closed = [int(number) for number in CLOSING.findall(pull.get("body") or "")]
    named = [tasks[task] for task in named_tasks(pull) if task in tasks]
    for numbers in (closed, named):
        gates = [issue_gates[number] for number in numbers if number in issue_gates]
        if gates:
            return min(gates, key=lambda gate: gate["title"])
    match = TYPE_PREFIX.match(pull["title"].strip())
    kind = match.group("type") if match else ""
    return milestones.get(TYPE_MILESTONES.get(kind, OTHER_MILESTONE))


def rest(method: str, path: str, token: str, payload: dict | None = None):
    request = urllib.request.Request(
        f"{API_URL}{path}",
        data=json.dumps(payload).encode() if payload is not None else None,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "maex-voice-agent-pr-metadata",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:300]
        raise MetadataError(f"{method} {path} -> {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise MetadataError(f"GitHub nicht erreichbar: {exc.reason}") from exc
    return json.loads(raw) if raw else {}


def pages(path: str, token: str) -> list[dict]:
    items: list[dict] = []
    page = 1
    while True:
        batch = rest("GET", f"{path}&per_page=100&page={page}", token)
        items.extend(batch)
        if len(batch) < 100:
            return items
        page += 1


def all_pulls(repo: str, token: str) -> list[dict]:
    return pages(f"/repos/{repo}/pulls?state=all", token)


def all_issues(repo: str, token: str) -> list[dict]:
    # The issue list also returns pull requests; those are handled above.
    items = pages(f"/repos/{repo}/issues?state=all", token)
    return [i for i in items if "pull_request" not in i]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true", help="Nur zeigen, was gesetzt wuerde."
    )
    parser.add_argument(
        "--findings-json",
        type=Path,
        help="Report types that cannot be inferred safely.",
    )
    args = parser.parse_args()

    token = os.environ.get("GITHUB_TOKEN", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not token or not repo:
        print(
            "GITHUB_TOKEN und GITHUB_REPOSITORY muessen gesetzt sein.", file=sys.stderr
        )
        return 2
    owner = repo.split("/")[0]

    try:
        issues = all_issues(repo, token)
        pulls = [
            pull
            for pull in all_pulls(repo, token)
            if pull.get("state") != "closed" or pull.get("merged_at")
        ]
        changes = [
            (pull, change) for pull in pulls if (change := missing(pull, owner))
        ] + [
            (issue, change)
            for issue in issues
            if (change := missing_on_issue(issue, owner))
        ]
        for pull, change in changes:
            summary = ", ".join(
                f"{key}={','.join(values)}" for key, values in change.items()
            )
            print(f"#{pull['number']}: {summary}")
            if args.dry_run:
                continue
            # Labels und Assignees laufen bei Pull Requests ueber die Issue-Schnittstelle.
            base = f"/repos/{repo}/issues/{pull['number']}"
            if "assignees" in change:
                rest(
                    "POST",
                    f"{base}/assignees",
                    token,
                    {"assignees": change["assignees"]},
                )
                time.sleep(WRITE_PAUSE_SECONDS)
            if "labels" in change:
                rest("POST", f"{base}/labels", token, {"labels": change["labels"]})
                time.sleep(WRITE_PAUSE_SECONDS)
        milestones = pages(f"/repos/{repo}/milestones?state=all", token)
        gates = gate_milestones(milestones)
        gated = [(i, gate) for i in issues if (gate := gate_of(i, gates))]
        issue_gates = {i["number"]: i["milestone"] for i in issues if i["milestone"]}
        issue_gates |= {i["number"]: gate for i, gate in gated}
        tasks = task_issues(WORKPACKAGES.read_text(encoding="utf-8"))
        by_title = {milestone["title"]: milestone for milestone in milestones}
        gated += [
            (pull, milestone)
            for pull in pulls
            if (milestone := milestone_of_pull(pull, issue_gates, tasks, by_title))
        ]
        for item, milestone in gated:
            print(f"#{item['number']}: milestone={milestone['title']}")
            if not args.dry_run:
                rest(
                    "PATCH",
                    f"/repos/{repo}/issues/{item['number']}",
                    token,
                    {"milestone": milestone["number"]},
                )
                time.sleep(WRITE_PAUSE_SECONDS)
        if args.findings_json:
            unresolved = [
                {
                    "id": str(item["number"]),
                    "label": f"Type label needs a decision: #{item['number']} {item['title']}",
                    "url": item.get("html_url"),
                }
                for item in [*issues, *pulls]
                if not {label["name"] for label in item.get("labels", [])}
                & (KNOWN_TYPES | {"projektpflege", "code-review"})
                and derive_label(item["title"]) is None
            ]
            args.findings_json.write_text(
                json.dumps(
                    {"Repository metadata": unresolved},
                    indent=2,
                ),
                encoding="utf-8",
            )
    except MetadataError as exc:
        print(f"PR-Metadaten fehlgeschlagen: {exc}", file=sys.stderr)
        return 1

    verb = "wuerden ergaenzt" if args.dry_run else "ergaenzt"
    print(f"{len(changes)} Pull Requests und Issues {verb}, {len(gated)} Milestones.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
