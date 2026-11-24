"""The /start leftover scan reports tracked files hidden by an index flag."""

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "hidden_index_flags.py"


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True)


def run(repo: Path) -> list[str]:
    out = subprocess.run(
        [sys.executable, str(SCRIPT), str(repo)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return out.splitlines()


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.email", "t@example.com")
    git(tmp_path, "config", "user.name", "T")
    for name in ("a.txt", "b.txt", "c.txt"):
        (tmp_path / name).write_text("x", encoding="utf-8")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-q", "-m", "base")
    return tmp_path


def test_clean_repo_reports_nothing(repo):
    assert run(repo) == []


def test_flagged_files_are_reported_and_status_misses_them(repo):
    git(repo, "update-index", "--assume-unchanged", "a.txt")
    git(repo, "update-index", "--skip-worktree", "b.txt")
    (repo / "a.txt").write_text("edited", encoding="utf-8")
    (repo / "b.txt").write_text("edited", encoding="utf-8")
    status = subprocess.run(
        ["git", "-C", str(repo), "status", "--short"], capture_output=True, text=True
    ).stdout
    assert status == ""  # the gap the scan closes
    assert run(repo) == ["assume-unchanged a.txt", "skip-worktree b.txt"]


def test_flag_removed_is_not_reported(repo):
    git(repo, "update-index", "--assume-unchanged", "c.txt")
    git(repo, "update-index", "--no-assume-unchanged", "c.txt")
    assert run(repo) == []
