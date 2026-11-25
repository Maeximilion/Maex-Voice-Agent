#!/usr/bin/env python3
"""List tracked files hidden from `git status` by an index flag.

`git update-index --assume-unchanged` and `--skip-worktree` make git stop
looking at a tracked file, so the leftover scan of `/start` (step 2a) cannot
see an edit there, yet the edit blocks a later fast-forward that changes the
file (Codex on PR #289, docs/24). `git ls-files -v` shows the flags: a
lowercase tag is assume-unchanged, `S` is skip-worktree.

Usage: python3 scripts/hidden_index_flags.py [folder]   (default: the current folder)
Output: one line per file, `<flag> <path>`; nothing when none. Exit 0 either way.
Deps:  git only.
"""

from __future__ import annotations

import subprocess
import sys

NAMES = {"assume-unchanged": "assume-unchanged", "skip-worktree": "skip-worktree"}


def flag_of(tag: str) -> str | None:
    """Flag for a `git ls-files -v` tag, None for a normal file."""
    if tag.upper() == "S":  # `S` skip-worktree, `s` skip-worktree plus assume-unchanged
        return NAMES["skip-worktree"]
    if tag.islower():
        return NAMES["assume-unchanged"]
    return None


def hidden(folder: str) -> list[str]:
    # -z keeps a path with a line break whole.
    out = subprocess.run(
        ["git", "-C", folder, "ls-files", "-v", "-z"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    found = []
    for entry in out.split("\0"):
        flag = flag_of(entry[:1]) if entry else None
        if flag:
            found.append(f"{flag} {entry[2:]}")
    return found


def main(argv: list[str]) -> int:
    for line in hidden(argv[1] if len(argv) > 1 else "."):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
