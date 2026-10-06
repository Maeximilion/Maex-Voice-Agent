"""`.claude/hooks/sync_main.sh` against throwaway repositories.

The hook runs at session start, usually from a feature worktree while `main` is
checked out in another one. On 2026-10-06 a `main` with staged files sat eight
commits behind and no session said so: the hook only looked at the worktree it was
started from. Every test here builds a bare origin, a clone that has `main` checked
out and a second worktree on a feature branch, and runs the hook from that second
worktree. The hook is bash; without bash the tests are skipped locally and fail in CI.
"""

import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from api.tests.test_scripts_backup import BASH

HOOK = Path(__file__).resolve().parents[2] / ".claude" / "hooks" / "sync_main.sh"


@pytest.fixture
def repos(tmp_path):
    if BASH is None:
        if os.environ.get("CI"):
            pytest.fail("sync hook tests need bash")
        pytest.skip("sync hook tests need bash")

    # No user or system git config: the result must not depend on the machine.
    empty_config = tmp_path / "gitconfig"
    empty_config.write_text("", encoding="utf-8")
    env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": str(empty_config),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }

    def git(cwd: Path, *args: str) -> str:
        done = subprocess.run(
            ["git", *args],
            cwd=cwd,
            env=env,
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
        return done.stdout.strip()

    origin = tmp_path / "origin.git"
    upstream = tmp_path / "upstream"
    main_checkout = tmp_path / "main_checkout"
    feature = tmp_path / "feature"

    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    git(tmp_path, "init", "-q", "-b", "main", str(upstream))
    git(upstream, "remote", "add", "origin", str(origin))

    def land_on_origin(name: str) -> None:
        (upstream / "tracked.txt").write_text(f"{name}\n", encoding="utf-8")
        git(upstream, "add", "tracked.txt")
        git(upstream, "commit", "-q", "-m", name)
        git(upstream, "push", "-q", "origin", "main")

    land_on_origin("first")
    git(tmp_path, "clone", "-q", str(origin), str(main_checkout))
    git(main_checkout, "worktree", "add", "-q", "-b", "feature", str(feature))
    # The hook finds its repository through its own path, so it has to sit inside one.
    hook = feature / ".claude" / "hooks" / "sync_main.sh"
    hook.parent.mkdir(parents=True)
    shutil.copyfile(HOOK, hook)

    def run_hook() -> str:
        done = subprocess.run(
            [BASH, hook.as_posix()],
            cwd=feature,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert done.returncode == 0, done.stderr
        return done.stdout.strip()

    def commits_behind() -> int:
        return int(git(main_checkout, "rev-list", "--count", "main..origin/main"))

    return SimpleNamespace(
        main_checkout=main_checkout,
        land_on_origin=land_on_origin,
        run_hook=run_hook,
        commits_behind=commits_behind,
    )


def test_clean_main_in_another_worktree_is_fast_forwarded(repos):
    repos.land_on_origin("second")

    assert repos.run_hook() == ""
    assert repos.commits_behind() == 0
    tracked = repos.main_checkout / "tracked.txt"
    assert tracked.read_text(encoding="utf-8") == "second\n"


def test_dirty_main_behind_origin_is_reported_and_left_alone(repos):
    repos.land_on_origin("second")
    tracked = repos.main_checkout / "tracked.txt"
    tracked.write_text("local edit\n", encoding="utf-8")

    output = repos.run_hook()

    assert "has uncommitted changes" in output
    assert "is 1 commits behind origin/main" in output
    assert repos.main_checkout.name in output
    assert repos.commits_behind() == 1
    assert tracked.read_text(encoding="utf-8") == "local edit\n"


def test_dirty_main_that_is_up_to_date_stays_silent(repos):
    tracked = repos.main_checkout / "tracked.txt"
    tracked.write_text("local edit\n", encoding="utf-8")

    assert repos.run_hook() == ""
    assert tracked.read_text(encoding="utf-8") == "local edit\n"
