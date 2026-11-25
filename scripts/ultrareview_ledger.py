#!/usr/bin/env python3
"""Keep the review candidates of auto mode in one open issue, ticked once reviewed.

/done step 7a "Review ledger" (Maxi, 2026-11-14): a PR with an `xhigh` trigger
whose `/code-review max` could not run before the PR opened, because Claude
worked in auto mode, gets a line; a line is ticked when its review ran, with
the level and the issues that hold the findings not fixed at once. `/start` and
`/gate` list the open lines. A closed ledger is finished: the next `add` opens
a new issue.

    python3 scripts/ultrareview_ledger.py add 544 "migration 029, a booking"
    python3 scripts/ultrareview_ledger.py done 544 --level max --findings 601 602
    python3 scripts/ultrareview_ledger.py done 544 --level ultra   # nothing left open
    python3 scripts/ultrareview_ledger.py list
    ... --dry-run   print the new body instead of writing it

Needs an authenticated `gh`; stdlib only. The issue is found by its marker and
made on the first `add`.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

TITLE = "Review candidates from auto mode"
LABELS = ("chore", "code-review", "priority: medium")
MARKER = "<!-- ultrareview-ledger:v1 -->"
INTRO = (
    "Pull requests with an `xhigh` trigger whose `/code-review max` could not run "
    "before the PR opened, because Claude worked in auto mode (/done step 1 and "
    '7a "Review ledger"). A line is ticked once its review ran, with the level. '
    "Findings that were not fixed at once each get an issue of their own, named on "
    "the line. Kept by `scripts/ultrareview_ledger.py`; edit by hand only to correct "
    "a line. Close the issue when every line is ticked; the next candidate opens a "
    "new one."
)
LEVELS = ("ultra", "max", "xhigh", "high")
LINE = re.compile(r"^- \[(?P<mark>[ x])\] #(?P<pr>\d+) (?P<text>.*)$")


def today() -> str:
    return datetime.now(ZoneInfo("Europe/Berlin")).date().isoformat()


def fresh_body() -> str:
    return f"{INTRO}\n\n{MARKER}\n"


def _lines(body: str) -> list[str]:
    return body.rstrip("\n").split("\n")


def find(body: str, pr: int) -> int | None:
    """Index of the line of `pr`, or None."""
    for i, line in enumerate(_lines(body)):
        m = LINE.match(line)
        if m and int(m["pr"]) == pr:
            return i
    return None


def add(body: str, pr: int, reason: str, day: str) -> str:
    """Append an open line for `pr`; a PR already listed stays as it is."""
    if find(body, pr) is not None:
        return body
    reason = " ".join(reason.split()) or "no reason given"
    return "\n".join([*_lines(body), f"- [ ] #{pr} {reason} · named {day}"]) + "\n"


def tick(body: str, pr: int, level: str, day: str, findings: list[int]) -> str:
    """Tick the line of `pr` with its review; a PR never named gets a ticked line."""
    if level not in LEVELS:
        raise ValueError(f"level must be one of {', '.join(LEVELS)}")
    left = ", ".join(f"#{n}" for n in findings) if findings else "none left open"
    note = f" · reviewed {day} ({level}) · findings: {left}"
    lines = _lines(body)
    i = find(body, pr)
    if i is None:
        return (
            "\n".join([*lines, f"- [x] #{pr} reviewed without a candidate line{note}"])
            + "\n"
        )
    m = LINE.match(lines[i])
    assert m is not None
    if m["mark"] == "x":
        # A second review of the same PR adds to the line, it never rewrites it.
        lines[i] = f"{lines[i]}{note}"
    else:
        lines[i] = f"- [x] #{pr} {m['text']}{note}"
    return "\n".join(lines) + "\n"


def open_items(body: str) -> list[str]:
    return [line for line in _lines(body) if line.startswith("- [ ] #")]


def _gh(*args: str, stdin: str | None = None) -> str:
    return subprocess.run(
        ["gh", *args],
        input=stdin,
        capture_output=True,
        text=True,
        # gh reads and prints UTF-8, whatever the locale codepage (cp1252 on Windows).
        encoding="utf-8",
        check=True,
    ).stdout


def load() -> tuple[int | None, str]:
    """The ledger issue's number and body; None and a fresh body before the first add."""
    # The label list, not `--search`: the search index lags a new issue by
    # seconds, and a second `add` in that time would open a duplicate.
    # Open ledgers only: a closed one is finished, the next `add` starts a new one.
    out = _gh(
        "issue", "list", "--state", "open", "--label", LABELS[1],
        "--json", "number,title,body", "--limit", "200",
    )  # fmt: skip
    for issue in json.loads(out):
        if MARKER in (issue.get("body") or ""):
            return issue["number"], issue["body"]
    return None, fresh_body()


def save(number: int | None, body: str) -> int:
    if number is None:
        url = _gh(
            "issue",
            "create",
            "--title",
            TITLE,
            "--body-file",
            "-",
            *(arg for label in LABELS for arg in ("--label", label)),
            stdin=body,
        ).strip()
        return int(url.rsplit("/", 1)[-1])
    # `gh issue edit` fails in this repo (projects classic); REST works.
    _gh(
        "api",
        "-X",
        "PATCH",
        f"repos/{{owner}}/{{repo}}/issues/{number}",
        "-f",
        f"body={body}",
    )
    return number


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--dry-run", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_add = sub.add_parser("add")
    p_add.add_argument("pr", type=int)
    p_add.add_argument("reason")
    p_done = sub.add_parser("done")
    p_done.add_argument("pr", type=int)
    p_done.add_argument("--level", required=True, choices=LEVELS)
    p_done.add_argument("--findings", type=int, nargs="*", default=[])
    sub.add_parser("list")
    args = parser.parse_args(argv)

    try:
        number, body = load()
    except (subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        print(f"ultrareview ledger not readable: {exc}", file=sys.stderr)
        return 1
    if args.cmd == "list":
        items = open_items(body)
        where = f" (#{number})" if number else ""
        print(f"{len(items)} open review candidates{where}")
        print("\n".join(items))
        return 0
    new = (
        add(body, args.pr, args.reason, today())
        if args.cmd == "add"
        else tick(body, args.pr, args.level, today(), args.findings)
    )
    if new == body:
        print(f"#{args.pr} already listed, nothing to change")
        return 0
    if args.dry_run:
        print(new)
        return 0
    try:
        number = save(number, new)
    except subprocess.CalledProcessError as exc:
        print(f"ultrareview ledger not written: {exc.stderr or exc}", file=sys.stderr)
        return 1
    print(f"ledger #{number}: {args.cmd} #{args.pr}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
