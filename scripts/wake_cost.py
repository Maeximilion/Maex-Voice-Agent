"""What a wake after an idle gap costs, read from Claude Code transcripts.

Usage: python scripts/wake_cost.py <transcript.jsonl>...

Every model request of a transcript (deduplicated by message id; streamed
chunks repeat it) is grouped by the gap since the previous request of the same
transcript. Cost units weight tokens by list price relative to fresh input:
cache read 0.1, cache write with 5-minute lifetime 1.25, with 1-hour lifetime
2.0, output 5. The table shows whether a wake after a wait pays more than a
working request, and from which gap on the prompt cache is written again.
First run and findings: 2026-11-23, `docs/HISTORY.md` "chain.md (archive, replaced 25.11.2026)", section 5.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

BUCKETS = (
    (0, 120, "< 2 min (working)"),
    (120, 600, "2-10 min"),
    (600, 1200, "10-20 min"),
    (1200, 2400, "20-40 min"),
    (2400, 3600, "40-60 min"),
    (3600, float("inf"), "> 60 min"),
)


def rows(path: Path) -> list[tuple[datetime, dict[str, Any], str]]:
    """Requests of one transcript in time order: time, last usage, model per message id."""
    seen: dict[str, tuple[str, dict[str, Any], str]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        msg = row.get("message") or {}
        if row.get("type") != "assistant" or not msg.get("usage") or not msg.get("id"):
            continue
        seen[msg["id"]] = (row["timestamp"], msg["usage"], msg.get("model") or "")
    ordered = sorted(seen.values(), key=lambda item: item[0])
    return [
        (datetime.fromisoformat(t.replace("Z", "+00:00")), u, m) for t, u, m in ordered
    ]


def requests(path: Path) -> list[tuple[datetime, dict[str, Any]]]:
    """Requests of one transcript in time order, the last usage per message id."""
    return [(when, usage) for when, usage, _ in rows(path)]


def cost(usage: dict[str, Any]) -> tuple[float, int, int]:
    """Cost units of one request and its cache writes (5-minute, 1-hour)."""
    split = usage.get("cache_creation") or {}
    write_5m = split.get("ephemeral_5m_input_tokens", 0)
    write_1h = split.get("ephemeral_1h_input_tokens", 0)
    if not split:
        write_5m = usage.get("cache_creation_input_tokens", 0)
    units = (
        usage.get("input_tokens", 0)
        + 0.1 * usage.get("cache_read_input_tokens", 0)
        + 1.25 * write_5m
        + 2.0 * write_1h
        + 5 * usage.get("output_tokens", 0)
    )
    return units, write_5m, write_1h


def by_gap(paths: Iterable[Path]) -> dict[str, list[tuple[float, int, int, int]]]:
    """Per gap bucket: cost units, cache read, 5-minute write, 1-hour write."""
    stats: dict[str, list[tuple[float, int, int, int]]] = {
        label: [] for *_, label in BUCKETS
    }
    for path in paths:
        previous = None
        for when, usage in requests(path):
            gap = (when - previous).total_seconds() if previous else 0.0
            previous = when
            units, write_5m, write_1h = cost(usage)
            label = next(lab for low, high, lab in BUCKETS if low <= gap < high)
            stats[label].append(
                (units, usage.get("cache_read_input_tokens", 0), write_5m, write_1h)
            )
    return stats


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    stats = by_gap(Path(arg) for arg in argv)
    print(
        f"{'gap before request':20} {'n':>5} {'cost units':>11} {'cache read':>11} {'5m write':>9} {'1h write':>9}"
    )
    for *_, label in BUCKETS:
        rows = stats[label]
        if rows:
            n = len(rows)
            avg = [sum(row[i] for row in rows) / n for i in range(4)]
            print(
                f"{label:20} {n:5} {avg[0]:11.0f} {avg[1]:11.0f} {avg[2]:9.0f} {avg[3]:9.0f}"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
