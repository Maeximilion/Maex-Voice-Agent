"""Whether a pull request's changed files reach the Docker images (#609).

Reads `git diff --no-renames --name-only` on stdin and prints `image=false` when
every file lies in docs/, .claude/, scripts/ or is Markdown at the top level,
else `image=true`. ci.yml skips docker-smoke and voice-image on false, since the
images build ./api only. An empty list prints true, so a broken diff never
skips them; `--no-renames` keeps the old side of a file moved out of api/.

Usage: git diff --no-renames --name-only BASE HEAD | python3 scripts/ci_changes.py
"""

import re
import sys

OUTSIDE_IMAGE = re.compile(r"^(docs/|\.claude/|scripts/|[^/]+\.md$)")


def image_changed(files: list[str]) -> bool:
    return not files or any(not OUTSIDE_IMAGE.match(path) for path in files)


if __name__ == "__main__":
    files = [line for line in sys.stdin.read().splitlines() if line]
    print(f"image={str(image_changed(files)).lower()}")
