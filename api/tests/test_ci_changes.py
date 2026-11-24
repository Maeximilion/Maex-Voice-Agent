"""CI skips the Docker jobs only for pull requests that cannot reach the image (#609)."""

import re
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.ci_changes import asterisk_changed, image_changed

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


def test_the_script_prints_the_output_lines_ci_reads():
    run = subprocess.run(
        [sys.executable, "scripts/ci_changes.py"],
        cwd=ROOT,
        input="docs/a.md\n\n.claude/x\n",
        capture_output=True,
        text=True,
        check=True,
    )
    assert run.stdout == "image=false\nasterisk=false\n"


@pytest.mark.parametrize(
    "files",
    [
        ["deploy/asterisk/image/Dockerfile"],
        ["docs/20_VOICE_LAYER.md", "deploy/asterisk/image/patches/x.patch"],
        ["deploy/asterisk/conf/extensions.conf"],
        ["scripts/asterisk_image_check.sh"],
        [".github/workflows/ci.yml"],
        # A broken or empty diff never skips the build.
        [],
    ],
)
def test_a_change_that_reaches_the_asterisk_image_builds_it(files):
    assert asterisk_changed(files) is True


@pytest.mark.parametrize(
    "files",
    [
        ["api/telephony/router.py", "docs/20_VOICE_LAYER.md"],
        ["deploy/asterisk/docker-compose.drill.yml", "deploy/asterisk/render.sh"],
        ["docker-compose.yml"],
    ],
)
def test_anything_else_skips_the_asterisk_build(files):
    assert asterisk_changed(files) is False


def test_ci_runs_the_check_on_the_image_it_built():
    """The job asterisk-image builds deploy/asterisk/image and runs the check
    of #775 on that very tag, gated by the output this script prints."""
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    found = re.search(r"\n  asterisk-image:\n(.*?)(?=\n  [a-z-]+:\n|\Z)", ci, re.S)
    assert found, "ci.yml has no job asterisk-image"
    job = found.group(1)
    assert "asterisk: ${{ steps.diff.outputs.asterisk }}" in ci
    assert "needs.changes.outputs.asterisk != 'false'" in job
    assert "docker build --tag maex-asterisk:ci deploy/asterisk/image" in job
    assert "sh scripts/asterisk_image_check.sh maex-asterisk:ci" in job
