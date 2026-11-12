#!/usr/bin/env python3
"""Token usage of a Claude Code session per model, for split routing (CLAUDE.md
"Split routing", Maxi 2026-11-06).

Reads the session's transcript and those of its subagents under
~/.claude/projects/<folder>/, sums the usage of every model reply, and either
prints it, records the part since the last record of the same session as rows
of docs/metrics/routing_history.csv, or estimates what a split saved from the
rows recorded so far. `context` prints the size of the session's current
context, which /handover compares with its thresholds.

Usage:
    python3 scripts/session_usage.py show
    python3 scripts/session_usage.py context
    python3 scripts/session_usage.py record T-3.15 --split yes
    python3 scripts/session_usage.py estimate

The session is $CLAUDE_CODE_SESSION_ID, the folder the current directory;
--session and --projects-dir override both. Stdlib only.
"""

import argparse
import csv
import json
import os
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

HISTORY = (
    Path(__file__).resolve().parents[1] / "docs" / "metrics" / "routing_history.csv"
)
FIELDS = [
    "date",
    "task",
    "split",
    "session",
    "model",
    "calls",
    "input",
    "cache_write",
    "cache_write_1h",
    "cache_read",
    "output",
    "weighted",
]
COUNTS = ["calls", "input", "cache_write", "cache_write_1h", "cache_read", "output"]
USAGE_KEYS = {
    "input": "input_tokens",
    "cache_write": "cache_creation_input_tokens",
    "cache_read": "cache_read_input_tokens",
    "output": "output_tokens",
}
# Assumption: the API price structure relative to an input token of the same
# model (cache read 0.1, cache write 1.25 for five minutes and 2 for an hour,
# output 5). cache_write_1h is the part of cache_write kept for an hour, so it
# adds the difference. The weighting of the subscription's usage limit is not
# published; this ranks volume within one model, it is no cost in money and no
# ratio between models.
WEIGHTS = {
    "input": 1.0,
    "cache_write": 1.25,
    "cache_write_1h": 0.75,
    "cache_read": 0.1,
    "output": 5.0,
}
# The models a session builds on; every other model counts as handed off.
MAIN_MODEL_PREFIX = ("claude-opus", "claude-fable")


def project_folder(cwd: str) -> str:
    """Claude Code's folder name for a working directory: / and . become -."""
    return cwd.replace("/", "-").replace(".", "-")


def transcripts(projects_dir: Path, session: str) -> list[Path]:
    main = projects_dir / f"{session}.jsonl"
    subagents = sorted((projects_dir / session).glob("**/*.jsonl"))
    return ([main] if main.exists() else []) + subagents


def replies(path: Path):
    """(message, line) of every model reply in one transcript, in order."""
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        message = record.get("message") if isinstance(record, dict) else None
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        if not isinstance(message.get("usage"), dict) or message.get("model") in (
            None,
            "",
            "<synthetic>",
        ):
            continue
        yield message, line


def usage(paths: list[Path]) -> dict[str, dict[str, int]]:
    """Totals per model. One reply is written as several records with the same
    message id; it counts once."""
    totals: dict[str, dict[str, int]] = defaultdict(lambda: dict.fromkeys(COUNTS, 0))
    for path in paths:
        seen: set[str] = set()
        for message, line in replies(path):
            counted = message["usage"]
            key = message.get("id") or line
            if key in seen:
                continue
            seen.add(key)
            row = totals[message["model"]]
            row["calls"] += 1
            for field, source in USAGE_KEYS.items():
                row[field] += int(counted.get(source) or 0)
            ttl = counted.get("cache_creation")
            if isinstance(ttl, dict):
                row["cache_write_1h"] += int(ttl.get("ephemeral_1h_input_tokens") or 0)
    return dict(totals)


def context(path: Path) -> tuple[int, str]:
    """Tokens the last reply of the main transcript read: its whole context."""
    size, model = 0, ""
    for message, _ in replies(path):
        counted = message["usage"]
        size = sum(
            int(counted.get(USAGE_KEYS[k]) or 0)
            for k in ("input", "cache_write", "cache_read")
        )
        model = message["model"]
    return size, model


def weighted(row: dict[str, int]) -> int:
    return round(sum(row[field] * w for field, w in WEIGHTS.items()))


def read_history(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def since_last_record(
    totals: dict[str, dict[str, int]], history: list[dict[str, str]], session: str
) -> dict[str, dict[str, int]]:
    """Totals minus what earlier rows of this session recorded, so a session
    that works on several tasks gives each task only its own part."""
    done: dict[str, dict[str, int]] = defaultdict(lambda: dict.fromkeys(COUNTS, 0))
    for row in history:
        if row["session"] == session:
            for field in COUNTS:
                done[row["model"]][field] += int(row[field])
    part = {}
    for model, counts in totals.items():
        delta = {f: counts[f] - done[model][f] for f in COUNTS}
        if delta["calls"] > 0:
            part[model] = delta
    return part


def record(
    path: Path, task: str, split: str, session: str, part: dict, today: str
) -> list[dict]:
    rows = [
        {
            "date": today,
            "task": task,
            "split": split,
            "session": session,
            "model": model,
            **counts,
            "weighted": weighted(counts),
        }
        for model, counts in sorted(part.items())
    ]
    if not rows:
        return rows
    new = not path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS, lineterminator="\n")
        if new:
            writer.writeheader()
        writer.writerows(rows)
    return rows


def estimate(history: list[dict[str, str]]) -> str:
    """One line for the split question of /task and /start."""
    per_task: dict[tuple[str, str], dict[str, int]] = defaultdict(
        lambda: {"main": 0, "other": 0}
    )
    for row in history:
        side = "main" if row["model"].startswith(MAIN_MODEL_PREFIX) else "other"
        per_task[(row["task"], row["split"])][side] += int(row["weighted"])
    split = [v for (_, s), v in per_task.items() if s == "yes"]
    whole = [v for (_, s), v in per_task.items() if s == "no"]
    if not split:
        return "no data yet: no split task recorded"
    moved = sum(v["other"] for v in split) / max(1, sum(sum(v.values()) for v in split))
    line = (
        f"{len(split)} split task(s): {moved:.0%} of the weighted volume ran on a "
        f"cheaper model"
    )
    if whole:

        def avg(rows: list[dict[str, int]]) -> float:
            return sum(sum(v.values()) for v in rows) / len(rows)

        line += (
            f"; average weighted volume per task {avg(split):,.0f} split, "
            f"{avg(whole):,.0f} not split ({len(whole)} task(s))"
        )
    return line + " (weights are an assumption, docs/metrics/routing_history.csv)"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--session", default=os.environ.get("CLAUDE_CODE_SESSION_ID"))
    parser.add_argument("--projects-dir", type=Path)
    parser.add_argument("--history", type=Path, default=HISTORY)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("show")
    sub.add_parser("context")
    rec = sub.add_parser("record")
    rec.add_argument("task")
    rec.add_argument("--split", choices=["yes", "no"], required=True)
    sub.add_parser("estimate")
    args = parser.parse_args(argv)

    if args.command == "estimate":
        print(estimate(read_history(args.history)))
        return 0
    if not args.session:
        print(
            "no session: set CLAUDE_CODE_SESSION_ID or pass --session", file=sys.stderr
        )
        return 2
    projects = args.projects_dir or (
        Path.home() / ".claude" / "projects" / project_folder(os.getcwd())
    )
    paths = transcripts(projects, args.session)
    main_file = projects / f"{args.session}.jsonl"
    if not paths or (args.command == "context" and not main_file.exists()):
        print(f"no transcript of {args.session} under {projects}", file=sys.stderr)
        return 2
    if args.command == "context":
        size, model = context(main_file)
        print(f"context {size:,} tokens ({model or 'no reply yet'})")
        return 0
    totals = usage(paths)
    if args.command == "show":
        for model, row in sorted(totals.items()):
            print(model, json.dumps({**row, "weighted": weighted(row)}))
        return 0
    part = since_last_record(totals, read_history(args.history), args.session)
    rows = record(
        args.history,
        args.task,
        args.split,
        args.session,
        part,
        datetime.now(ZoneInfo("Europe/Berlin")).date().isoformat(),
    )
    for row in rows:
        print(f"recorded {row['task']} {row['model']} weighted {row['weighted']:,}")
    if not rows:
        print("nothing new since the last record of this session")
    return 0


if __name__ == "__main__":
    sys.exit(main())
