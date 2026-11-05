#!/usr/bin/env python3
"""Assignee und Label fuer Pull Requests nachtragen, wo sie fehlen.

Zweck : Jeder Pull Request bekommt den Repo-Eigentuemer als Assignee und ein
        Typ-Label aus seinem Titel (Conventional Commits: feat -> feature,
        fix -> bug, ...). Das deckt auch alte Pull Requests und solche von Bots ab.
        It also takes the stufe-N and priority label of the issues it closes,
        where it has none of that kind (docs/16 section 5).
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
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
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
KNOWN_TYPES = frozenset(TYPE_LABELS.values()) | {
    "enhancement",
    "documentation",
    "research",
}
GATE_TITLE = re.compile(r"^G(?P<stage>\d+)\b")

WORKPACKAGES = Path(__file__).resolve().parent.parent / "docs/07_WORKPACKAGES.md"
TASK_ROW = re.compile(r"^\| (?P<task>T-\d+\.\d+) \| (?:#(?P<issue>\d+)|-) \|")
GROUP_ISSUE = re.compile(r"^## .*(?:Sammel-Issue|group issue) #(?P<issue>\d+)")
# What a pull request takes over from the issues it closes, one label each.
INHERITED_PREFIXES = ("stufe-", "priority: ")
PRIORITY_RANK = {"priority: high": 0, "priority: medium": 1, "priority: low": 2}
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


def missing(
    pull: dict, owner: str, issues: dict[int, dict] | None = None
) -> dict[str, list[str]]:
    """Was an einem Pull Request fehlt. Leer, wenn nichts zu tun ist.

    `issues` by number: a pull request also takes the stage and the priority
    of the issues it closes, where it has none of that kind (as in MaexSocial,
    Maxi 2026-11-05)."""
    change: dict[str, list[str]] = {}
    if owner.casefold() not in {
        assignee["login"].casefold() for assignee in pull.get("assignees", [])
    }:
        change["assignees"] = [owner]
    labels = {label["name"] for label in pull.get("labels", [])}
    new = []
    if not labels & KNOWN_TYPES and (label := derive_label(pull["title"])):
        new.append(label)
    closed = [int(number) for number in closing_numbers(pull.get("body") or "")]
    for prefix in INHERITED_PREFIXES:
        if any(name.startswith(prefix) for name in labels):
            continue
        # The earliest stage and the highest priority come first.
        found = sorted(
            (
                label["name"]
                for number in closed
                for label in (issues or {}).get(number, {}).get("labels", [])
                if label["name"].startswith(prefix)
            ),
            key=lambda name: (PRIORITY_RANK.get(name, 0), name),
        )
        new += found[:1]
    if not any(name.startswith("area: ") for name in labels):
        new += [name for name in area_labels(pull["title"]) if name not in labels]
    if new:
        change["labels"] = list(dict.fromkeys(new))
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
    closed = [int(number) for number in closing_numbers(pull.get("body") or "")]
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
        batch = rest(
            "GET", f"{path}{'&' if '?' in path else '?'}per_page=100&page={page}", token
        )
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


CATALOG = Path(__file__).resolve().with_name("labels.json")
# Exact Conventional Commit scopes only. Text mentioning an area is not evidence.
SCOPE_AREAS = {
    "gui": "frontend",
    "ui": "frontend",
    "frontend": "frontend",
    "agent": "backend",
    "domain": "backend",
    "api": "backend",
    "tools": "backend",
    "voice": "voice",
    "telephony": "voice",
    "speech": "voice",
    "db": "data",
    "data": "data",
    "migrations": "data",
    "ci": "ci",
    "project": "ci",
    "hooks": "ci",
    "commands": "ci",
    "deploy": "deployment",
    "docker": "deployment",
    "backup": "deployment",
    "security": "security",
    "auth": "security",
}
PATH_AREAS = {
    "api/gui/": "frontend",
    "gui/": "frontend",
    "api/telephony/": "voice",
    "api/agent/": "backend",
    "api/domain/": "backend",
    "api/tools/": "backend",
    "api/core/": "backend",
    "api/models/": "data",
    "db/": "data",
    "deploy/": "deployment",
    "printbridge/": "deployment",
    ".github/": "ci",
    ".claude/": "ci",
    "scripts/": "ci",
}


def closing_numbers(body: str) -> list[str]:
    """Local closing references outside fenced examples and inline code."""
    text = re.sub(r"(?ms)^\s*(`{3,}|~{3,}).*?^\s*\1[^\n]*$", "", body)
    text = re.sub(r"`[^`\n]*`", "", text)
    return CLOSING.findall(text)


def area_labels(title: str, paths: list[str] | None = None) -> list[str]:
    labels = set()
    scope = re.match(r"^[a-z]+\(([^)]+)\)!?:", title)
    if scope and scope[1] in SCOPE_AREAS:
        labels.add("area: " + SCOPE_AREAS[scope[1]])
    for path in paths or []:
        labels.update(
            "area: " + area
            for prefix, area in PATH_AREAS.items()
            if path.startswith(prefix)
        )
        suffix = Path(path).suffix
        language = {
            ".py": "python",
            ".js": "javascript",
            ".ts": "typescript",
            ".tsx": "typescript",
        }.get(suffix)
        if language:
            labels.add(language)
    return sorted(labels)


def label_problems(item: dict) -> list[str]:
    """Report uncertainty instead of assigning a guessed priority or removing labels."""
    labels = {label["name"] for label in item.get("labels", [])}
    priorities = {name for name in labels if name.startswith("priority:")}
    problems = []
    if len(priorities) != 1 or not priorities <= PRIORITY_RANK.keys():
        problems.append("Priority needs a decision (exactly one high/medium/low)")
    types = {
        {"enhancement": "feature", "documentation": "docs"}.get(name, name)
        for name in labels & KNOWN_TYPES
    }
    if len(types) != 1:
        problems.append("Primary type needs a decision")
    if not any(name.startswith("area: ") for name in labels):
        problems.append("Area needs evidence")
    return problems


def content_fingerprint(item: dict) -> str:
    """Ignore comments/timestamps, but invalidate a reviewed plan after a scope edit."""
    value = json.dumps([item["title"], item.get("body") or ""], ensure_ascii=False)
    return hashlib.sha256(value.encode()).hexdigest()


def planned_labels(item: dict, entry: dict) -> list[str]:
    before, additions = set(entry["before"]), set(entry["add"])
    current = {label["name"] for label in item.get("labels", [])}
    if content_fingerprint(item) != entry["fingerprint"] or not (
        before - set(entry.get("remove", [])) <= current <= before | additions
    ):
        raise MetadataError(
            f"#{entry['number']}: content or labels changed; review the plan again"
        )
    return sorted(additions - current)


def load_catalog() -> list[dict]:
    data = json.loads(CATALOG.read_text(encoding="utf-8"))
    labels = data["labels"]
    if data.get("version") != 1 or len({x["name"] for x in labels}) != len(labels):
        raise MetadataError("Invalid label catalog version or duplicate name")
    return labels


def sync_catalog(repo: str, token: str, *, dry_run: bool) -> int:
    existing = {x["name"]: x for x in pages(f"/repos/{repo}/labels", token)}
    for label in load_catalog():
        old = existing.get(label["name"])
        if old and all((old.get(k) or "") == v for k, v in label.items()):
            continue
        print(f"Catalog: {label['name']}")
        if not dry_run:
            if old:
                name = urllib.parse.quote(label["name"], safe="")
                rest("PATCH", f"/repos/{repo}/labels/{name}", token, label)
            else:
                rest("POST", f"/repos/{repo}/labels", token, label)
            time.sleep(WRITE_PAUSE_SECONDS)
    return 0


def apply_label_plan(path: Path, repo: str, token: str, *, dry_run: bool) -> int:
    """Apply reviewed additive history metadata. Re-running resumes from live labels."""
    plan = json.loads(path.read_text(encoding="utf-8"))
    if plan.get("version") != 1 or plan.get("repository") != repo:
        raise MetadataError("Label plan version or repository does not match")
    allowed = {x["name"] for x in load_catalog()}
    numbers = set()
    for entry in plan["items"]:
        number = entry["number"]
        if not isinstance(number, int) or number < 1 or number in numbers:
            raise MetadataError("Invalid or duplicate issue number in label plan")
        numbers.add(number)
        reviewed_removals = set(entry.get("remove", []))
        if not reviewed_removals <= set(entry["before"]) & KNOWN_TYPES:
            raise MetadataError(
                f"#{number}: only reviewed primary-type conflicts may be removed"
            )
        after = (set(entry["before"]) - reviewed_removals) | set(entry["add"])
        priorities = {name for name in after if name.startswith("priority:")}
        if (
            not set(entry["add"]) <= allowed
            or len(priorities) != 1
            or not priorities <= PRIORITY_RANK.keys()
            or not entry.get("reason")
        ):
            raise MetadataError(
                f"#{number}: unknown label, invalid priority or missing evidence"
            )
    changed = 0
    for entry in plan["items"]:
        base = f"/repos/{repo}/issues/{entry['number']}"
        current = rest("GET", base, token)
        additions = planned_labels(current, entry)
        removals = sorted(
            set(entry.get("remove", []))
            & {x["name"] for x in current.get("labels", [])}
        )
        if not additions and not removals:
            continue
        print(
            f"#{entry['number']}: +{', '.join(additions)} -{', '.join(removals)} — {entry['reason']}",
            flush=True,
        )
        if not dry_run:
            if additions:
                rest("POST", f"{base}/labels", token, {"labels": additions})
                time.sleep(WRITE_PAUSE_SECONDS)
            for name in removals:
                # Recheck after each write, allowing only this plan's partial state.
                planned_labels(rest("GET", base, token), entry)
                rest(
                    "DELETE",
                    f"{base}/labels/{urllib.parse.quote(name, safe='')}",
                    token,
                )
                time.sleep(WRITE_PAUSE_SECONDS)
            verified = rest("GET", base, token)
            if planned_labels(verified, entry) or set(entry.get("remove", [])) & {
                x["name"] for x in verified.get("labels", [])
            }:
                raise MetadataError(f"#{entry['number']}: labels did not persist")
        changed += 1
    print(f"Label plan: {changed} items {'would change' if dry_run else 'changed'}")
    return 0


def write_missing(item: dict, change: dict, repo: str, token: str, *, dry_run: bool):
    for key, values in change.items():
        print(f"#{item['number']}: {key}={','.join(values)}")
        if not dry_run:
            rest(
                "POST",
                f"/repos/{repo}/issues/{item['number']}/{key}",
                token,
                {key: values},
            )
            time.sleep(WRITE_PAUSE_SECONDS)


def reconcile_event(path: Path, repo: str, token: str, *, dry_run: bool) -> int:
    """Target one live item; fork titles and paths remain API data, never commands."""
    event = json.loads(path.read_text(encoding="utf-8"))
    is_pull = "pull_request" in event
    number = event.get("pull_request", event.get("issue", {})).get("number")
    if not isinstance(number, int) or number < 1:
        raise MetadataError("Event has no valid issue or pull request number")
    base = f"/repos/{repo}"
    item = rest("GET", f"{base}/{'pulls' if is_pull else 'issues'}/{number}", token)
    issues = {}
    if is_pull:
        # A PR comment is not a local closing reference; fetch only referenced issues.
        for reference in set(closing_numbers(item.get("body") or "")):
            try:
                linked = rest("GET", f"{base}/issues/{int(reference)}", token)
            except MetadataError as exc:
                if "-> 404:" not in str(exc):
                    raise
                continue
            if "pull_request" not in linked:
                issues[int(reference)] = linked
    change = missing(item, repo.split("/")[0], issues)
    if is_pull:
        paths = [x["filename"] for x in pages(f"{base}/pulls/{number}/files", token)]
        existing = {x["name"] for x in item.get("labels", [])}
        added = set(change.get("labels", [])) | (
            set(area_labels(item["title"], paths)) - existing
        )
        if added:
            change["labels"] = sorted(added)
        if item.get("state") == "closed" and not item.get("merged_at"):
            change.pop("assignees", None)
    write_missing(item, change, repo, token, dry_run=dry_run)
    for problem in label_problems(
        {
            **item,
            "labels": item.get("labels", [])
            + [{"name": x} for x in change.get("labels", [])],
        }
    ):
        print(f"#{number}: {problem}; daily maintenance reports unresolved decisions")
    return 0


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
    parser.add_argument(
        "--event", type=Path, help="Reconcile only the issue/PR in this event file"
    )
    parser.add_argument(
        "--sync-labels", action="store_true", help="Sync the versioned catalog"
    )
    parser.add_argument(
        "--apply-label-plan",
        type=Path,
        help="Apply a reviewed additive historical plan",
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
        if args.sync_labels:
            return sync_catalog(repo, token, dry_run=args.dry_run)
        if args.apply_label_plan:
            return apply_label_plan(
                args.apply_label_plan, repo, token, dry_run=args.dry_run
            )
        if args.event:
            return reconcile_event(args.event, repo, token, dry_run=args.dry_run)
        issues = all_issues(repo, token)
        by_number = {issue["number"]: issue for issue in issues}
        every_pull = all_pulls(repo, token)
        pulls = [
            pull
            for pull in every_pull
            if pull.get("state") != "closed" or pull.get("merged_at")
        ]
        changes = [
            (pull, change)
            for pull in pulls
            if (change := missing(pull, owner, by_number))
        ] + [
            (issue, change)
            for issue in issues
            if (change := missing_on_issue(issue, owner))
        ]
        # Closed, unmerged PRs receive labels only, never assignees or milestones.
        for rejected in every_pull:
            if rejected.get("state") == "closed" and not rejected.get("merged_at"):
                labels = missing(rejected, owner, by_number).get("labels", [])
                if labels:
                    changes.append((rejected, {"labels": labels}))
        for pull, change in changes:
            write_missing(pull, change, repo, token, dry_run=args.dry_run)
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
            pending = {
                item["number"]: change.get("labels", []) for item, change in changes
            }
            unresolved = []
            for item in [*issues, *every_pull]:
                effective = {
                    **item,
                    "labels": item.get("labels", [])
                    + [{"name": name} for name in pending.get(item["number"], [])],
                }
                problems = label_problems(effective)
                if problems:
                    unresolved.append(
                        {
                            "id": str(item["number"]),
                            "label": f"#{item['number']} {item['title']}: {'; '.join(problems)}",
                            "url": item.get("html_url"),
                        }
                    )
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
