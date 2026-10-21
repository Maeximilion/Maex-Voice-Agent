#!/usr/bin/env python3
"""Open security alerts and the open pull requests into main that fix them.

/start step 5a (CLAUDE.md section 6 "Security alerts"), one line per open alert:
a critical or high alert names the pull request that fixes it or blocks task
work; a lower one is listed with what to do with it. No output: no open alert.

    python3 scripts/alert_fixes.py

Needs an authenticated `gh`, stdlib only.

A pull request fixes a Dependabot alert when the dependency graph between its
base and head commits (the comparison dependency review uses) removes the
alert's package with the alert's advisory from the alert's manifest and adds no
version there that still carries it. It fixes a code-scanning alert when the
alert has an instance in state "fixed" on the pull request's current head commit
(`refs/pull/N/head`, this repository's CodeQL workflow) or its current test merge
commit (`refs/pull/N/merge`, GitHub's default setup).

Anything short of that is at most a candidate, named next to the blocking alert
and never taken for the fix: a pull request that removes the advisory from
another manifest (requirements-voice.txt takes its pins from requirements.txt),
or one that changes the alert's file under its new or old name while no scan has
confirmed it.

Exit 1 when an API call failed: the lines that could be worked out are printed
anyway, the error goes to stderr, and a source that failed is never reported as
zero alerts.
"""

from __future__ import annotations

import functools
import json
import re
import subprocess
import sys
from collections.abc import Callable

BLOCKING = {"critical", "high"}
REPO = "repos/{owner}/{repo}"  # gh fills in the repository of the checkout

Gh = Callable[[str], list]


class GhError(Exception):
    pass


def gh_api(path: str) -> list:
    """Every item of a REST list, all pages."""
    res = subprocess.run(
        ["gh", "api", "--paginate", path, "--jq", ".[]"], capture_output=True, text=True
    )
    if res.returncode != 0:
        raise GhError(
            res.stderr.strip() or f"gh api {path} exited with {res.returncode}"
        )
    return [json.loads(line) for line in res.stdout.splitlines() if line.strip()]


def _package(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()  # PEP 503: Foo_Bar is foo-bar


def _alerts(gh: Gh, errors: list[str]) -> list[dict]:
    alerts = []
    try:
        for a in gh(f"{REPO}/dependabot/alerts?state=open&per_page=100"):
            alerts.append(
                {
                    "source": "dependabot",
                    "number": a["number"],
                    "severity": a["security_advisory"]["severity"],
                    "name": a["dependency"]["package"]["name"],
                    "path": a["dependency"]["manifest_path"],
                    "ghsa": a["security_advisory"]["ghsa_id"],
                }
            )
    except GhError as exc:
        errors.append(f"dependabot alerts: {exc}")
    try:
        for a in gh(f"{REPO}/code-scanning/alerts?state=open&per_page=100"):
            rule = a["rule"]
            alerts.append(
                {
                    "source": "code-scanning",
                    "number": a["number"],
                    # Only a security severity can block; a quality rule has none.
                    "severity": rule.get("security_severity_level")
                    or rule.get("severity"),
                    "name": rule["id"],
                    "path": a["most_recent_instance"]["location"]["path"],
                }
            )
    except GhError as exc:
        errors.append(f"code-scanning alerts: {exc}")
    return alerts


def _carrying(gh: Gh, pr: dict, alert: dict) -> list[dict]:
    """Dependency changes of the pull request that carry the alert's advisory."""
    changes = gh(
        f"{REPO}/dependency-graph/compare/{pr['base']['sha']}...{pr['head']['sha']}"
    )
    return [
        c
        for c in changes
        if _package(c["name"]) == _package(alert["name"])
        and any(v["advisory_ghsa_id"] == alert["ghsa"] for v in c["vulnerabilities"])
    ]


def _fixes(gh: Gh, pr: dict, alert: dict) -> bool:
    if alert["source"] == "dependabot":
        kinds = {
            c["change_type"]
            for c in _carrying(gh, pr, alert)
            if c["manifest"] == alert["path"]
        }
        return kinds == {"removed"}
    for ref, sha in (
        ("head", pr["head"]["sha"]),
        ("merge", pr.get("merge_commit_sha")),
    ):
        instances = gh(
            f"{REPO}/code-scanning/alerts/{alert['number']}/instances"
            f"?ref=refs/pull/{pr['number']}/{ref}&per_page=100"
        )
        if any(i["state"] == "fixed" and i["commit_sha"] == sha for i in instances):
            return True
    return False


def _candidate(gh: Gh, pr: dict, alert: dict) -> bool:
    if alert["source"] == "dependabot" and any(
        c["change_type"] == "removed" for c in _carrying(gh, pr, alert)
    ):
        return True
    files = gh(f"{REPO}/pulls/{pr['number']}/files?per_page=100")
    return any(
        alert["path"] in (f["filename"], f.get("previous_filename")) for f in files
    )


def report(gh: Gh = gh_api) -> tuple[list[str], list[str]]:
    gh = functools.cache(gh)  # one compare and one file list per pull request
    errors: list[str] = []
    alerts = _alerts(gh, errors)
    prs: list[dict] = []
    if any(a["severity"] in BLOCKING for a in alerts):
        try:
            prs = gh(f"{REPO}/pulls?state=open&base=main&per_page=100")
        except GhError as exc:
            errors.append(f"open pull requests: {exc}")

    def which(check: Callable[[Gh, dict, dict], bool], alert: dict) -> list[str]:
        found = []
        for pr in prs:
            try:
                if check(gh, pr, alert):
                    found.append(f"#{pr['number']}")
            except GhError as exc:
                msg = f"pull request #{pr['number']}: {exc}"
                if msg not in errors:  # the fix and the candidate check share a call
                    errors.append(msg)
        return found

    lines = []
    alerts.sort(key=lambda a: (a["severity"] not in BLOCKING, a["source"], a["number"]))
    for a in alerts:
        head = f"{a['source']} #{a['number']} {a['severity']} {a['name']} {a['path']}"
        if a["severity"] not in BLOCKING:
            what = (
                "below high: the pin rides in the next feature PR"
                if a["source"] == "dependabot"
                else "below high: a suggestion next to the tasks"
            )
            lines.append(f"{head}: {what}")
            continue
        fixed = which(_fixes, a)
        if fixed:
            lines.append(f"{head}: fixed by {', '.join(fixed)}")
            continue
        line = f"{head}: BLOCKS TASKS, no open PR into main fixes it"
        near = which(_candidate, a)
        if near:
            line += f"; candidates, not confirmed as the fix: {', '.join(near)}"
        lines.append(line)
    return lines, errors


def main(gh: Gh = gh_api) -> int:
    lines, errors = report(gh)
    for line in lines:
        print(line)
    for err in errors:
        print(f"error: {err}", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
