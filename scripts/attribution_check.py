#!/usr/bin/env python3
"""Fail a pull request whose commits or description credit an AI author.

Maxi wants no co-author trailer, no "Generated with" badge and no AI as a
commit's author or committer unless he asked for the credit in that work
(CLAUDE.md section 6 "Independent review requests", section 8 Git); a harness
reminder put a trailer on PR #610, and a cloud session committed as Claude on
PR #915, which makes GitHub list Claude among the contributors. The check covers every
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


# An AI vendor's address, or an AI's name as the whole name, in "name <email>".
AI_IDENTITY = re.compile(
    r"@(?:[\w.-]+\.)?(?:anthropic\.com|openai\.com)>"
    r"|^(?:claude|codex|chatgpt)(?:\[bot\])? <",
    re.IGNORECASE,
)


def credits(text: str) -> list[str]:
    return [match.group(0).strip() for match in CREDIT.finditer(text or "")]


def commits(revision_range: str) -> list[list[str]]:
    """Per commit: short sha, "author <email>", "committer <email>", message."""
    out = subprocess.run(
        [
            "git",
            "log",
            "--format=%h%x1f%an <%ae>%x1f%cn <%ce>%x1f%B%x1e",
            revision_range,
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    records = [record.strip("\n") for record in out.split("\x1e")]
    return [record.split("\x1f", 3) for record in records if record]


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

    found = []
    for sha, author, committer, message in commits(args.range):
        found += [(sha, line) for line in credits(message)]
        found += [
            (sha, f"{role} {who}")
            for role, who in (("author", author), ("committer", committer))
            if AI_IDENTITY.search(who)
        ]
    found += [("description", line) for line in credits(pull.get("body") or "")]
    for where, line in found:
        print(f"::error::AI attribution in {where}: {line}")
    if found:
        print(
            "Remove the line (reword the commit, edit the description), make Maxi the "
            "commit's author and committer (git commit --amend --reset-author), or add the label "
            f"{APPROVED_LABEL} when Maxi asked for the credit."
        )
        return 1
    print("no AI attribution in commits or description")
    return 0


if __name__ == "__main__":
    sys.exit(main())
