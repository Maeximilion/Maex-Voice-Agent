"""Whether a pull request's changed files reach the Docker images (#609, #775).

Reads `git diff --no-renames --name-only` on stdin and prints two lines for
ci.yml. `image=false` when every file lies in docs/, .claude/, scripts/ or is
Markdown at the top level, else `image=true`: ci.yml skips docker-smoke and
voice-image on false, since those images build ./api only. `asterisk=true` when
a file reaches the own Asterisk image or its check (deploy/asterisk/image/,
deploy/asterisk/conf/, the check script or ci.yml), else false: the job
asterisk-image compiles Asterisk, minutes a pull request should spend only when
it can change the result. An empty list prints true for both, so a broken diff
never skips them; `--no-renames` keeps the old side of a moved file.

Usage: git diff --no-renames --name-only BASE HEAD | python3 scripts/ci_changes.py
"""

import re
import sys

OUTSIDE_IMAGE = re.compile(r"^(docs/|\.claude/|scripts/|[^/]+\.md$)")
ASTERISK_IMAGE = re.compile(
    r"^(deploy/asterisk/image/|deploy/asterisk/conf/"
    r"|scripts/asterisk_image_check\.sh$|\.github/workflows/ci\.yml$)"
)


def image_changed(files: list[str]) -> bool:
    return not files or any(not OUTSIDE_IMAGE.match(path) for path in files)


def asterisk_changed(files: list[str]) -> bool:
    return not files or any(ASTERISK_IMAGE.match(path) for path in files)


if __name__ == "__main__":
    files = [line for line in sys.stdin.read().splitlines() if line]
    print(f"image={str(image_changed(files)).lower()}")
    print(f"asterisk={str(asterisk_changed(files)).lower()}")
