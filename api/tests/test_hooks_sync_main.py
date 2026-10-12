"""`.claude/hooks/sync_main.sh` against throwaway repositories.

The hook runs at session start, usually from a feature worktree while `main` is
checked out in another one. On 2026-10-06 a `main` with staged files sat eight
commits behind and no session said so: the hook only looked at the worktree it was
started from. Every test here builds a bare origin, a clone that has `main` checked
out and a second worktree on a feature branch, and runs the hook from that second
worktree. They need bash and git. The api container has no git and does not mount
`.claude/hooks`, so under `make test` they are skipped; on a full checkout with both
tools they run, and in CI a missing tool is a failure.
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
    missing = "bash" if BASH is None else None if shutil.which("git") else "git"
    if missing:
        if os.environ.get("CI"):
            pytest.fail(f"sync hook tests need {missing}")
        pytest.skip(f"sync hook tests need {missing}, which the api container lacks")

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

    def finish_branch(name: str, *, squash: bool = False) -> None:
        """A branch of the main folder, pushed, landed on origin's main and
        deleted there, as GitHub leaves it after a merge."""
        git(main_checkout, "switch", "-q", "-c", name)
        (main_checkout / f"{name}.txt").write_text("work\n", encoding="utf-8")
        git(main_checkout, "add", f"{name}.txt")
        git(main_checkout, "commit", "-q", "-m", name)
        git(main_checkout, "push", "-q", "-u", "origin", name)
        git(upstream, "fetch", "-q", "origin")
        how = ["--squash"] if squash else ["--no-ff", "-m", f"merge {name}"]
        git(upstream, "merge", "-q", *how, f"origin/{name}")
        if squash:
            git(upstream, "commit", "-q", "-m", f"{name} squashed")
        git(upstream, "push", "-q", "origin", "main")
        git(upstream, "push", "-q", "origin", "--delete", name)

    return SimpleNamespace(
        git=git,
        finish_branch=finish_branch,
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


def test_the_main_folder_on_a_finished_branch_goes_back_to_main(repos):
    repos.finish_branch("finished")

    assert repos.run_hook() == ""
    assert repos.git(repos.main_checkout, "branch", "--show-current") == "main"
    assert repos.commits_behind() == 0
    assert (repos.main_checkout / "finished.txt").exists()


def test_a_finished_branch_with_uncommitted_work_is_left_alone(repos):
    repos.finish_branch("finished")
    (repos.main_checkout / "notes.txt").write_text("mine\n", encoding="utf-8")

    repos.run_hook()

    assert repos.git(repos.main_checkout, "branch", "--show-current") == "finished"
    assert (repos.main_checkout / "notes.txt").read_text(encoding="utf-8") == "mine\n"


def test_a_branch_whose_commits_are_not_in_main_is_left_alone(repos):
    # A squash merge: the remote branch is gone, the commits themselves never landed.
    repos.finish_branch("squashed", squash=True)

    repos.run_hook()

    assert repos.git(repos.main_checkout, "branch", "--show-current") == "squashed"


def test_a_branch_that_still_exists_on_the_remote_is_left_alone(repos):
    repos.git(repos.main_checkout, "switch", "-q", "-c", "ongoing")
    repos.git(repos.main_checkout, "push", "-q", "-u", "origin", "ongoing")

    repos.run_hook()

    assert repos.git(repos.main_checkout, "branch", "--show-current") == "ongoing"


def test_main_checked_out_nowhere_is_still_moved_to_origin(repos):
    repos.git(repos.main_checkout, "switch", "-q", "-c", "ongoing")
    repos.land_on_origin("second")

    assert repos.run_hook() == ""
    assert repos.commits_behind() == 0
    assert repos.git(repos.main_checkout, "branch", "--show-current") == "ongoing"


def test_a_local_main_that_diverged_is_reported_and_left_alone(repos):
    (repos.main_checkout / "local.txt").write_text("local\n", encoding="utf-8")
    repos.git(repos.main_checkout, "add", "local.txt")
    repos.git(repos.main_checkout, "commit", "-q", "-m", "local only")
    local = repos.git(repos.main_checkout, "rev-parse", "main")
    repos.git(repos.main_checkout, "switch", "-q", "-c", "ongoing")
    repos.land_on_origin("second")

    assert "was not updated" in repos.run_hook()
    assert repos.git(repos.main_checkout, "rev-parse", "main") == local
