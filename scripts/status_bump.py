#!/usr/bin/env python3
"""Version, Datum und Changelog-Zeile in docs/01_STATUS.md automatisch fortschreiben."""

from __future__ import annotations

import argparse
import re
import sys
from datetime import date
from pathlib import Path

HEADER_RE = re.compile(
    r"(Stand:\s*)(\d{2}\.\d{2}\.\d{4})(.*?Status-Version:\s*)(\d+)\.(\d+)\.(\d+)"
)
CHANGELOG_RE = re.compile(r"(## Changelog\n\n)")


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
    parser.add_argument("message", help="eine Zeile für den Changelog-Eintrag")
    parser.add_argument(
        "--file",
        default=Path("docs/01_STATUS.md"),
        type=Path,
        help="Zieldatei (Default: docs/01_STATUS.md)",
    )
    args = parser.parse_args(argv)

    if not args.file.exists():
        print(f"Fehler: {args.file} existiert nicht.", file=sys.stderr)
        return 1

    text = args.file.read_text(encoding="utf-8")

    header_match = HEADER_RE.search(text)
    if not header_match:
        print(
            f"Fehler: keine Kopfzeile 'Stand: DD.MM.YYYY … Status-Version: X.Y.Z' in {args.file} gefunden.",
            file=sys.stderr,
        )
        return 1

    if "## Changelog" not in text:
        print(
            f"Fehler: keine Überschrift '## Changelog' in {args.file} gefunden.",
            file=sys.stderr,
        )
        return 1

    old_version = tuple(int(g) for g in header_match.groups()[3:6])
    new_version = bump(old_version, args.level)
    new_version_str = ".".join(str(part) for part in new_version)
    today = date.today().strftime("%d.%m.%Y")

    text = (
        text[: header_match.start()]
        + f"Stand: {today}"
        + header_match.group(3)
        + new_version_str
        + text[header_match.end() :]
    )

    changelog_entry = f"- **v{new_version_str} · {today}:** {args.message}\n"
    text, n = CHANGELOG_RE.subn(lambda m: m.group(1) + changelog_entry, text, count=1)
    if n == 0:
        print(
            f"Fehler: Changelog-Eintrag konnte nicht eingefügt werden in {args.file}.",
            file=sys.stderr,
        )
        return 1

    args.file.write_text(text, encoding="utf-8")
    old_version_str = ".".join(str(part) for part in old_version)
    print(f"docs-status: v{old_version_str} -> v{new_version_str} ({today})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
