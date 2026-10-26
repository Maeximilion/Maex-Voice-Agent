#!/usr/bin/env python3
"""Where Codex's review of a pull request's head stands, optionally waiting for it.

/ship step 2 and every fix round (CLAUDE.md section 6 "Reviews"):

    python3 scripts/codex_review.py 371          # one look
    python3 scripts/codex_review.py 371 --wait   # until done, stalled or timed out

Prints one line, `codex <head>: <state>`, plus the unresolved findings when
there are any. Exit 0 clean, 1 findings, 2 not done yet (running, or no review
of this head started), 3 stalled, 4 an API call failed. Needs an authenticated
`gh`, stdlib only.

Codex starts by itself on "PR opened" and on every push ("New commits") and is
done in 4 to 8 minutes, so `@codex review` after a push only duplicates it. A
run can hang without any sign: on PR #371 (26.10.2026) two runs stayed
"running" with no review, comment or reaction, one of them with no manual
request at all. Both had thread replies (each a review on the head) posted
within seconds of the push; the runs without such replies finished. Hence:
answer review threads before the push, and treat "stalled" (running for more
than 15 minutes, or no review of the head started within 15 minutes of
waiting) as the one case for a single `@codex review`. This script never
comments.

How the result shows (seen on #370 and #371): the bot's summary comment carries
`codex-review-state:v1 {...}`; done is `"status":"completed"`, whose
`commit_sha` is the head, or null on a review with findings. Findings come as a
review by the bot whose `commit_id` is the head, with inline comments; a clean
review leaves `has_any_findings: false` and a +1 on the pull request.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

BOT = "chatgpt-codex-connector[bot]"
REPO = "repos/{owner}/{repo}"  # gh fills in the repository of the checkout
STALLED_AFTER = timedelta(minutes=15)
_STATE = re.compile(r"codex-review-state:v1 (\{.*?\}\}) -->")
_SINCE = re.compile(r'Running\*\* since <relative-time datetime="([^"]+)"')

EXIT = {"clean": 0, "findings": 1, "running": 2, "not started": 2, "stalled": 3}

Gh = Callable[[str], list]


class GhError(Exception):
    pass


def gh_api(path: str) -> list:
    """Every item of a REST list (all pages), or a single object as one item."""
    res = subprocess.run(
        [
            "gh",
            "api",
            "--paginate",
            path,
            "--jq",
            'if type == "array" then .[] else . end',
        ],
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        raise GhError(res.stderr.strip() or f"gh api {path} exited {res.returncode}")
    return [json.loads(line) for line in res.stdout.splitlines() if line.strip()]


def state(head: str, summary: str | None, reviews: list[dict], now: datetime) -> str:
    """Codex's state for `head` from its summary comment and its reviews."""
    if any(r["user"]["login"] == BOT and r["commit_id"] == head for r in reviews):
        return "findings"
    match = _STATE.search(summary or "")
    code = json.loads(match.group(1))["code_review"] if match else {}
    if code.get("commit_sha") != head:
        # A completed state with no sha belongs to the head of a review with
        # findings, and that review is checked above.
        return "not started"
    if code.get("status") == "completed":
        return "findings" if code.get("has_any_findings") else "clean"
    since = _SINCE.search(summary or "")
    started = datetime.fromisoformat(since.group(1)) if since else now
    return "stalled" if now - started > STALLED_AFTER else "running"


def look(
    pr: int, gh: Gh = gh_api, now: datetime | None = None
) -> tuple[str, str, list]:
    """Head sha, state and the unresolved findings of the head."""
    now = now or datetime.now(UTC)
    head = gh(f"{REPO}/pulls/{pr}")[0]["head"]["sha"]
    comments = [
        c
        for c in gh(f"{REPO}/issues/{pr}/comments?per_page=100")
        if c["user"]["login"] == BOT and _STATE.search(c["body"])
    ]
    summary = max(comments, key=lambda c: c["updated_at"])["body"] if comments else None
    reviews = gh(f"{REPO}/pulls/{pr}/reviews?per_page=100")
    current = state(head, summary, reviews, now)
    findings = []
    if current == "findings":
        ids = {
            r["id"]
            for r in reviews
            if r["user"]["login"] == BOT and r["commit_id"] == head
        }
        findings = [
            c
            for c in gh(f"{REPO}/pulls/{pr}/comments?per_page=100")
            if c.get("pull_request_review_id") in ids
        ]
    return head, current, findings


def _title(body: str) -> str:
    """The headline of a finding: its first line without the badge and bold
    (`**<sub><sub>![P2 Badge](...)</sub></sub>  Title**`), so P0 to P2 stay."""
    line = body.split("\n", 1)[0]
    badge = re.search(r"!\[(P\d) Badge\]", line)
    line = re.sub(r"!\[[^\]]*\]\([^)]*\)|</?sub>|\*\*", "", line).strip()
    return f"{badge.group(1)} {line}" if badge else line[:100]


def main(argv: list[str] | None = None, gh: Gh = gh_api) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("pr", type=int)
    parser.add_argument(
        "--wait", action="store_true", help="poll until done or stalled"
    )
    parser.add_argument(
        "--timeout", type=float, default=30.0, help="minutes, with --wait"
    )
    parser.add_argument("--interval", type=float, default=30.0, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    started = datetime.now(UTC)
    while True:
        try:
            head, current, findings = look(args.pr, gh)
        except (GhError, KeyError, IndexError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 4
        waited = datetime.now(UTC) - started
        if current == "not started" and waited > STALLED_AFTER:
            current = "stalled"
        done = current in ("clean", "findings", "stalled")
        if done or not args.wait or waited > timedelta(minutes=args.timeout):
            break
        time.sleep(args.interval)
    print(f"codex {head[:7]}: {current}")
    for c in findings:
        print(
            f"  {c['id']} {c['path']}:{c.get('line') or c.get('original_line')} {_title(c['body'])}"
        )
    if current == "stalled":
        print("  one comment `@codex review` (never a second one)")
    return EXIT[current]


if __name__ == "__main__":
    sys.exit(main())
