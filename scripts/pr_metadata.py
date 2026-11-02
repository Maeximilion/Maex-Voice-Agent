#!/usr/bin/env python3
"""Assignee und Label fuer Pull Requests nachtragen, wo sie fehlen.

Zweck : Jeder Pull Request bekommt den Repo-Eigentuemer als Assignee und ein
        Typ-Label aus seinem Titel (Conventional Commits: feat -> feature,
        fix -> bug, ...). Das deckt auch alte Pull Requests und solche von Bots ab.
        Gesetzt wird nur, was fehlt; vorhandene Labels bleiben, ein zweiter Lauf
        aendert nichts.
        Every open issue without an assignee gets the owner too (Maxi,
        2026-10-28); its labels (type, stufe-N) are set at creation and not
        derived here. Every issue, open or closed, without a milestone gets the
        gate of its stufe-N label (stufe-1 -> "G1 ..."), so the roadmap can group
        by gate (Maxi, 2026-11-02). A milestone set by hand stays.
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
import urllib.error
import urllib.request

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
GATE_TITLE = re.compile(r"^G(?P<stage>\d+)\b")


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
    if not pull.get("assignees"):
        change["assignees"] = [owner]
    if not pull.get("labels") and (label := derive_label(pull["title"])):
        change["labels"] = [label]
    return change


def missing_on_issue(issue: dict, owner: str) -> dict[str, list[str]]:
    """What an open issue lacks: only the assignee."""
    return {} if issue.get("assignees") else {"assignees": [owner]}


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
        changes = [
            (pull, change)
            for pull in all_pulls(repo, token)
            if (change := missing(pull, owner))
        ] + [
            (issue, change)
            for issue in issues
            if issue["state"] == "open" and (change := missing_on_issue(issue, owner))
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
            if "labels" in change:
                rest("POST", f"{base}/labels", token, {"labels": change["labels"]})
        gates = gate_milestones(pages(f"/repos/{repo}/milestones?state=all", token))
        gated = [(i, gate) for i in issues if (gate := gate_of(i, gates))]
        for issue, gate in gated:
            print(f"#{issue['number']}: milestone={gate['title']}")
            if not args.dry_run:
                rest(
                    "PATCH",
                    f"/repos/{repo}/issues/{issue['number']}",
                    token,
                    {"milestone": gate["number"]},
                )
    except MetadataError as exc:
        print(f"PR-Metadaten fehlgeschlagen: {exc}", file=sys.stderr)
        return 1

    verb = "wuerden ergaenzt" if args.dry_run else "ergaenzt"
    print(f"{len(changes)} Pull Requests und Issues {verb}, {len(gated)} Milestones.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
