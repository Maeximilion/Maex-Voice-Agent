#!/usr/bin/env python3
"""Open security alerts and the open pull requests into main that fix them.

/start step 5a (CLAUDE.md section 6 "Security alerts"), one line per open alert:
a critical or high alert names the pull request that fixes it or blocks task
work; a lower one is listed with what to do with it. No output: no open alert.

    python3 scripts/alert_fixes.py

Needs an authenticated `gh`, stdlib only. A pull request fixes a Dependabot alert
when the dependency graph between its base and head commits removes the alert's
package with the alert's advisory, in any manifest (the comparison dependency
review uses; requirements-voice.txt takes its pins from requirements.txt). It
fixes a code-scanning alert when the alert has an instance in state "fixed" on
the pull request's head commit. A pull request that only changes the alert's
file is named as a candidate, never as the fix. Exit 1 when an API call failed:
the lines that could be worked out are printed anyway, the error goes to stderr,
and a source that failed is never reported as zero alerts.
"""

from __future__ import annotations

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


def _fixes(gh: Gh, pr: dict, alert: dict) -> bool:
    base, head = pr["base"]["sha"], pr["head"]["sha"]
    if alert["source"] == "dependabot":
        changes = gh(f"{REPO}/dependency-graph/compare/{base}...{head}")
        return any(
            c["change_type"] == "removed"
            and _package(c["name"]) == _package(alert["name"])
            and any(
                v["advisory_ghsa_id"] == alert["ghsa"] for v in c["vulnerabilities"]
            )
            for c in changes
        )
    instances = gh(
        f"{REPO}/code-scanning/alerts/{alert['number']}/instances"
        f"?ref=refs/pull/{pr['number']}/head&per_page=100"
    )
    return any(i["state"] == "fixed" and i["commit_sha"] == head for i in instances)


def _changes_file(gh: Gh, pr: dict, path: str) -> bool:
    files = gh(f"{REPO}/pulls/{pr['number']}/files?per_page=100")
    return any(f["filename"] == path for f in files)


def report(gh: Gh = gh_api) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    alerts = _alerts(gh, errors)
    prs: list[dict] = []
    if any(a["severity"] in BLOCKING for a in alerts):
        try:
            prs = gh(f"{REPO}/pulls?state=open&base=main&per_page=100")
        except GhError as exc:
            errors.append(f"open pull requests: {exc}")

    def ask(check: Callable[[Gh, dict, object], bool], pr: dict, arg: object) -> bool:
        try:
            return check(gh, pr, arg)
        except GhError as exc:
            errors.append(f"pull request #{pr['number']}: {exc}")
            return False

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
        fixed = [p["number"] for p in prs if ask(_fixes, p, a)]
        if fixed:
            lines.append(f"{head}: fixed by " + ", ".join(f"#{n}" for n in fixed))
            continue
        line = f"{head}: BLOCKS TASKS, no open PR into main fixes it"
        near = [p["number"] for p in prs if ask(_changes_file, p, a["path"])]
        if near:
            line += "; candidates that change the file: " + ", ".join(
                f"#{n}" for n in near
            )
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
