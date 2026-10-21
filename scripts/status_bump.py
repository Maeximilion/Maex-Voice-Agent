#!/usr/bin/env python3
"""Bump the version, date and changelog line in docs/01_STATUS.md automatically."""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

HEADER_RE = re.compile(
    r"(Status:\s*)(\d{2}\.\d{2}\.\d{4})(.*?Status version:\s*)(\d+)\.(\d+)\.(\d+)"
)
CHANGELOG_RE = re.compile(r"(## Changelog\n\n)")
# The date in the docs is local time, not UTC (CLAUDE.md §8).
BERLIN = ZoneInfo("Europe/Berlin")


def bump(version: tuple[int, int, int], level: str) -> tuple[int, int, int]:
    major, minor, patch = version
    if level == "major":
        return (major + 1, 0, 0)
    if level == "minor":
        return (major, minor + 1, 0)
    return (major, minor, patch + 1)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("level", choices=["patch", "minor", "major"])
    parser.add_argument("message", help="one line for the changelog entry")
    parser.add_argument(
        "--file",
        default=Path("docs/01_STATUS.md"),
        type=Path,
        help="target file (default: docs/01_STATUS.md)",
    )
    args = parser.parse_args(argv)

    if not args.file.exists():
        print(f"Error: {args.file} does not exist.", file=sys.stderr)
        return 1

    text = args.file.read_text(encoding="utf-8")

    header_match = HEADER_RE.search(text)
    if not header_match:
        print(
            f"Error: no header line 'Status: DD.MM.YYYY ... Status version: X.Y.Z' found in {args.file}.",
            file=sys.stderr,
        )
        return 1

    if "## Changelog" not in text:
        print(
            f"Error: no '## Changelog' heading found in {args.file}.",
            file=sys.stderr,
        )
        return 1

    major, minor, patch = (int(g) for g in header_match.groups()[3:6])
    old_version = (major, minor, patch)
    new_version = bump(old_version, args.level)
    new_version_str = ".".join(str(part) for part in new_version)
    today = datetime.now(BERLIN).strftime("%d.%m.%Y")

    text = (
        text[: header_match.start()]
        + f"Status: {today}"
        + header_match.group(3)
        + new_version_str
        + text[header_match.end() :]
    )

    changelog_entry = f"- **v{new_version_str} · {today}:** {args.message}\n"
    text, n = CHANGELOG_RE.subn(lambda m: m.group(1) + changelog_entry, text, count=1)
    if n == 0:
        print(
            f"Error: could not insert the changelog entry into {args.file}.",
            file=sys.stderr,
        )
        return 1

    args.file.write_text(text, encoding="utf-8")
    old_version_str = ".".join(str(part) for part in old_version)
    print(f"docs-status: v{old_version_str} -> v{new_version_str} ({today})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
