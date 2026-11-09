"""CI skips the Docker jobs only for pull requests that cannot reach the image (#609)."""

import subprocess
import sys
from pathlib import Path

import pytest

from scripts.ci_changes import image_changed

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    "files",
    [
        ["docs/01_STATUS.md", "docs/HISTORY.md"],
        ["CLAUDE.md", "AGENTS.md", "README.md"],
        [".claude/hooks/merge_gate.sh", "scripts/status_bump.py"],
    ],
)
def test_a_change_outside_the_image_skips_it(files):
    assert image_changed(files) is False


@pytest.mark.parametrize(
    "files",
    [
        ["docs/01_STATUS.md", "api/domain/menu/search.py"],
        ["api/prompts/system_v3.md"],
        ["api/Dockerfile"],
        [".github/workflows/ci.yml"],
        ["deploy/docker-compose.prod.yml"],
        # git quotes a path with unusual characters; unknown means build.
        ['"docs/\\303\\244.md"'],
        # A broken or empty diff never skips the images.
        [],
    ],
)
def test_anything_else_builds_the_image(files):
    assert image_changed(files) is True


def test_a_file_moved_out_of_api_still_builds_the_image():
    # With --no-renames git lists both sides of the move.
    assert image_changed(["api/old_notes.py", "docs/old_notes.py"]) is True


def test_the_script_prints_the_output_line_ci_reads():
    run = subprocess.run(
        [sys.executable, "scripts/ci_changes.py"],
        cwd=ROOT,
        input="docs/a.md\n\n.claude/x\n",
        capture_output=True,
        text=True,
        check=True,
    )
    assert run.stdout == "image=false\n"
