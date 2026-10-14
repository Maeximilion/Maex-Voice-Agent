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

import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from api.tests.test_scripts_backup import BASH

HOOK = Path(__file__).resolve().parents[2] / ".claude" / "hooks" / "sync_main.sh"


@pytest.fixture
def repos(tmp_path, request):
    missing = "bash" if BASH is None else None if shutil.which("git") else "git"
    if missing:
        if os.environ.get("CI"):
            pytest.fail(f"sync hook tests need {missing}")
        pytest.skip(f"sync hook tests need {missing}, which the api container lacks")

    # No user or system git config: the result must not depend on the machine.
    empty_config = tmp_path / "gitconfig"
    empty_config.write_text("", encoding="utf-8")
    # A stand-in for gh: the head commits GitHub would list as merged are the lines of
    # a file, and there is none unless a test writes it. The real gh must never be
    # asked about a throwaway repository. A file next to it named `.fails` makes the
    # stand-in exit like a gh that is signed out; what it was asked lands in `.asked`.
    merged_heads = tmp_path / "merged_heads"
    fake_gh = tmp_path / "bin" / "gh"
    fake_gh.parent.mkdir()
    fake_gh.write_text(
        '#!/bin/sh\necho "$*" > "$FAKE_GH_MERGED.asked"\n'
        '[ -e "$FAKE_GH_MERGED.fails" ] && exit 4\n'
        'cat "$FAKE_GH_MERGED" 2>/dev/null\nexit 0\n',
        encoding="utf-8",
    )
    fake_gh.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{fake_gh.parent}{os.pathsep}{os.environ['PATH']}",
        "FAKE_GH_MERGED": str(merged_heads),
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
    main_checkout = tmp_path / getattr(request, "param", "main_checkout")
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

    def run_hook(*args: str) -> str:
        assert BASH is not None  # checked at the top of this fixture
        done = subprocess.run(
            [BASH, hook.as_posix(), *args],
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

    def finish_branch(name: str, *, squash: bool = False, merge: bool = True) -> None:
        """A branch of the main folder, pushed, landed on origin's main and
        deleted there, as GitHub leaves it after a merge. Without `merge` the
        remote branch is deleted and its work never landed."""
        git(main_checkout, "switch", "-q", "-c", name)
        (main_checkout / f"{name}.txt").write_text("work\n", encoding="utf-8")
        git(main_checkout, "add", f"{name}.txt")
        git(main_checkout, "commit", "-q", "-m", name)
        git(main_checkout, "push", "-q", "-u", "origin", name)
        git(upstream, "fetch", "-q", "origin")
        if merge:
            how = ["--squash"] if squash else ["--no-ff", "-m", f"merge {name}"]
            git(upstream, "merge", "-q", *how, f"origin/{name}")
            if squash:
                git(upstream, "commit", "-q", "-m", f"{name} squashed")
            git(upstream, "push", "-q", "origin", "main")
        git(upstream, "push", "-q", "origin", "--delete", name)

    def change_on_origin(file: str) -> None:
        """A later commit on origin's main that rewrites a file a branch added."""
        git(upstream, "pull", "-q", "origin", "main")
        (upstream / file).write_text("changed later\n", encoding="utf-8")
        git(upstream, "commit", "-q", "-am", f"{file} changed later")
        git(upstream, "push", "-q", "origin", "main")

    return SimpleNamespace(
        git=git,
        tmp_path=tmp_path,
        merged_heads=merged_heads,
        change_on_origin=change_on_origin,
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
    # The files that block it, so nobody has to go and look first.
    assert "1 files block it: M tracked.txt" in output
    assert repos.commits_behind() == 1
    assert tracked.read_text(encoding="utf-8") == "local edit\n"


def test_for_claude_code_the_warning_is_json_the_user_sees(repos):
    # 12 to 14.10.2026: plain output reached Claude only and the main folder fell
    # 192 commits behind. A file name git has to quote puts a backslash and a
    # quotation mark into the message, which the JSON has to survive.
    repos.land_on_origin("second")
    (repos.main_checkout / "tracked.txt").write_text("local edit\n", encoding="utf-8")
    (repos.main_checkout / "Notiz ä.txt").write_text("draft\n", encoding="utf-8")

    sent = json.loads(repos.run_hook("--json"))

    shown = sent["systemMessage"]
    assert "is 1 commits behind origin/main" in shown
    assert "2 files block it" in shown and "tracked.txt" in shown
    assert '?? "Notiz \\303\\244.txt"' in shown
    told = sent["hookSpecificOutput"]
    assert told["hookEventName"] == "SessionStart"
    assert told["additionalContext"].startswith(shown)
    assert "top of your first reply" in told["additionalContext"]


@pytest.mark.skipif(
    os.name == "nt", reason="Windows has no control character in a path"
)
@pytest.mark.parametrize("repos", ["main\x0ccheckout"], indirect=True)
def test_a_control_character_in_the_path_still_makes_valid_json(repos):
    # Codex PR #290: a form feed in the folder name went out raw and broke the JSON,
    # and Claude Code drops a hook output it cannot parse.
    repos.land_on_origin("second")
    (repos.main_checkout / "tracked.txt").write_text("local edit\n", encoding="utf-8")

    sent = json.loads(repos.run_hook("--json"))

    assert "main\x0ccheckout" in sent["systemMessage"]


def test_for_claude_code_a_main_folder_in_order_stays_silent(repos):
    repos.land_on_origin("second")

    assert repos.run_hook("--json") == ""
    assert repos.commits_behind() == 0


def test_a_clean_main_that_cannot_be_fast_forwarded_says_why(repos):
    # The old hook said only "git pull --ff-only failed".
    (repos.main_checkout / "local.txt").write_text("local\n", encoding="utf-8")
    repos.git(repos.main_checkout, "add", "local.txt")
    repos.git(repos.main_checkout, "commit", "-q", "-m", "local only")
    repos.land_on_origin("second")

    output = repos.run_hook()

    assert "is 1 commits behind origin/main and could not be fast-forwarded" in output
    assert "(git: " in output and "(git: )" not in output
    assert (repos.main_checkout / "local.txt").exists()


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


def test_a_squash_merged_branch_counts_as_finished(repos):
    # Codex PR #247: the commits never land, their changes do.
    repos.finish_branch("squashed", squash=True)

    assert repos.run_hook() == ""
    assert repos.git(repos.main_checkout, "branch", "--show-current") == "main"
    assert repos.commits_behind() == 0
    assert (repos.main_checkout / "squashed.txt").exists()


def test_a_squash_merged_branch_with_a_later_commit_is_left_alone(repos):
    repos.finish_branch("squashed", squash=True)
    (repos.main_checkout / "later.txt").write_text(
        "after the merge\n", encoding="utf-8"
    )
    repos.git(repos.main_checkout, "add", "later.txt")
    repos.git(repos.main_checkout, "commit", "-q", "-m", "work after the merge")

    repos.run_hook()

    assert repos.git(repos.main_checkout, "branch", "--show-current") == "squashed"


def test_a_branch_deleted_on_the_remote_without_a_merge_is_left_alone(repos):
    repos.finish_branch("dropped", merge=False)

    repos.run_hook()

    assert repos.git(repos.main_checkout, "branch", "--show-current") == "dropped"
    assert (repos.main_checkout / "dropped.txt").exists()


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


def test_a_squash_merge_that_main_has_changed_since_needs_githubs_word(repos):
    # Codex PR #247: main rewrote the same file after the squash merge, so merging the
    # old branch into today's main conflicts and proves nothing either way.
    repos.finish_branch("squashed", squash=True)
    repos.change_on_origin("squashed.txt")

    repos.run_hook()
    assert repos.git(repos.main_checkout, "branch", "--show-current") == "squashed"

    # A merged pull request with another head commit is not this branch.
    repos.merged_heads.write_text("0" * 40 + "\n", encoding="utf-8")
    repos.run_hook()
    assert repos.git(repos.main_checkout, "branch", "--show-current") == "squashed"

    tip = repos.git(repos.main_checkout, "rev-parse", "squashed")
    repos.merged_heads.write_text(f"{'0' * 40}\n{tip}\n", encoding="utf-8")
    assert repos.run_hook() == ""
    assert repos.git(repos.main_checkout, "branch", "--show-current") == "main"
    # Only a pull request merged into main counts: one merged into another branch
    # has the same head commit and has not reached main.
    asked = Path(f"{repos.merged_heads}.asked").read_text(encoding="utf-8")
    assert "--state merged --base main --head squashed" in asked
    changed = repos.main_checkout / "squashed.txt"
    assert changed.read_text(encoding="utf-8") == "changed later\n"


def test_main_checked_out_in_another_folder_is_reported_not_switched(repos):
    # Codex PR #247: git checks a branch out in one folder only, the switch would fail.
    repos.finish_branch("finished")
    elsewhere = repos.tmp_path / "main_elsewhere"
    repos.git(repos.main_checkout, "worktree", "add", "-q", str(elsewhere), "main")

    output = repos.run_hook()

    assert "is on the finished branch finished" in output
    assert "main_elsewhere" in output and "not switched" in output
    assert repos.git(repos.main_checkout, "branch", "--show-current") == "finished"
    # The folder that has main is synced all the same.
    assert repos.commits_behind() == 0
    assert (elsewhere / "finished.txt").exists()


@pytest.mark.skipif(os.name == "nt", reason="Windows has no line break in a path")
@pytest.mark.parametrize("repos", ["main\ncheckout"], indirect=True)
def test_a_main_folder_with_a_line_break_in_its_path_is_still_found(repos):
    # Codex PR #247: the plain worktree list breaks such a path over two lines.
    repos.finish_branch("finished")

    assert repos.run_hook() == ""
    assert repos.git(repos.main_checkout, "branch", "--show-current") == "main"
    assert repos.commits_behind() == 0


def test_a_gh_that_cannot_answer_is_reported_not_taken_as_not_merged(repos):
    # Codex PR #247: signed out, missing or too slow is no answer, and "left alone"
    # without a word would look like a main folder that is in order.
    repos.finish_branch("squashed", squash=True)
    repos.change_on_origin("squashed.txt")
    Path(f"{repos.merged_heads}.fails").touch()

    output = repos.run_hook()

    assert "GitHub could not be asked" in output and "squashed" in output
    assert repos.git(repos.main_checkout, "branch", "--show-current") == "squashed"


def test_gh_is_not_asked_where_git_knows_the_answer(repos):
    # A merge commit and a plain squash merge need no GitHub: a broken gh changes nothing.
    Path(f"{repos.merged_heads}.fails").touch()
    repos.finish_branch("finished")

    assert repos.run_hook() == ""
    assert repos.git(repos.main_checkout, "branch", "--show-current") == "main"


def test_a_branch_github_says_was_not_merged_stays_without_a_warning(repos):
    repos.finish_branch("dropped", merge=False)

    assert repos.run_hook() == ""
    assert repos.git(repos.main_checkout, "branch", "--show-current") == "dropped"


def test_a_conflicting_branch_is_not_taken_for_merged(repos):
    # Codex PR #247: the branch deletes a file that main has changed since. Merging it
    # conflicts, and git's provisional tree for that conflict is main's own.
    upstream = repos.tmp_path / "upstream"
    repos.git(repos.main_checkout, "switch", "-q", "-c", "deleter")
    repos.git(repos.main_checkout, "rm", "-q", "tracked.txt")
    repos.git(repos.main_checkout, "commit", "-q", "-m", "tracked.txt removed")
    repos.git(repos.main_checkout, "push", "-q", "-u", "origin", "deleter")
    repos.git(upstream, "push", "-q", "origin", "--delete", "deleter")
    repos.land_on_origin("second")

    assert repos.run_hook() == ""
    assert repos.git(repos.main_checkout, "branch", "--show-current") == "deleter"
    assert not (repos.main_checkout / "tracked.txt").exists()
