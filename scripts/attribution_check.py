#!/usr/bin/env python3
"""Fail a pull request whose commits or description credit an AI author.

Maxi wants no co-author trailer and no "Generated with" badge unless he asked
for the credit in that work (CLAUDE.md section 6 "Independent review requests",
section 8 Git); a harness reminder put one on PR #610. The check covers every
author, from any machine, in the required check `title` (pr-title.yml). The PR
label `attribution-approved` is the escape for credit Maxi asked for.

Usage: python3 scripts/attribution_check.py --event "$GITHUB_EVENT_PATH" BASE..HEAD
Deps:  standard library and git.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys

APPROVED_LABEL = "attribution-approved"
# Line starts only, so text that names the rule (in backticks, mid-sentence) passes.
CREDIT = re.compile(
    r"^\s*co-authored-by:.*$"
    r"|^\s*(?:\U0001F916\s*)?generated (?:with|by)\b.*$",
    re.IGNORECASE | re.MULTILINE,
)


def credits(text: str) -> list[str]:
    return [match.group(0).strip() for match in CREDIT.finditer(text or "")]


def commit_messages(revision_range: str) -> list[tuple[str, str]]:
    out = subprocess.run(
        ["git", "log", "--format=%h%x1f%B%x1e", revision_range],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    records = [record.strip("\n") for record in out.split("\x1e")]
    return [tuple(record.split("\x1f", 1)) for record in records if record]  # type: ignore[misc]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--event", required=True, help="the pull_request event JSON")
    parser.add_argument("range", help="the pull request's commits, BASE..HEAD")
    args = parser.parse_args(argv)

    with open(args.event, encoding="utf-8") as handle:
        pull = json.load(handle)["pull_request"]
    if APPROVED_LABEL in {label["name"] for label in pull.get("labels", [])}:
        print(f"label {APPROVED_LABEL}: credit allowed in this pull request")
        return 0

    found = [
        (sha, line)
        for sha, message in commit_messages(args.range)
        for line in credits(message)
    ]
    found += [("description", line) for line in credits(pull.get("body") or "")]
    for where, line in found:
        print(f"::error::AI attribution in {where}: {line}")
    if found:
        print(
            "Remove the line (reword the commit, edit the description), or add the label "
            f"{APPROVED_LABEL} when Maxi asked for the credit."
        )
        return 1
    print("no AI attribution in commits or description")
    return 0


if __name__ == "__main__":
    sys.exit(main())
