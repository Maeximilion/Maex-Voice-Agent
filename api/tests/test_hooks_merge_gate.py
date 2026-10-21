"""`.claude/hooks/merge_gate.sh` against a throwaway repository and a stand-in gh.

PR #339 was merged straight after a fix round without the status bump (CLAUDE.md §6),
so the gate blocks `gh pr merge` until CI, the sync with `main`, the bump and a clean
review hold on the head commit. Every test builds a bare origin whose `main` carries a
status version and a pull ref `refs/pull/7/head` for the branch, and a stand-in gh that
answers `pr view` and the comments call from files. Needs bash, git and jq; under
`make test` (api container) the tests are skipped, in CI a missing tool is a failure.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from api.tests.test_scripts_backup import BASH

HOOK = Path(__file__).resolve().parents[2] / ".claude" / "hooks" / "merge_gate.sh"
CODEX = "chatgpt-codex-connector[bot]"
GREEN = [
    {"name": "test", "status": "COMPLETED", "conclusion": "SUCCESS"},
    {"name": "docs", "status": "COMPLETED", "conclusion": "SKIPPED"},
]


@pytest.fixture
def gate(tmp_path):
    missing = next((t for t in ("git", "jq") if not shutil.which(t)), None)
    missing = "bash" if BASH is None else missing
    if missing:
        if os.environ.get("CI"):
            pytest.fail(f"merge gate tests need {missing}")
        pytest.skip(f"merge gate tests need {missing}")

    empty_config = tmp_path / "gitconfig"
    empty_config.write_text("", encoding="utf-8")
    fake = tmp_path / "fake"
    fake.mkdir()
    gh = tmp_path / "bin" / "gh"
    gh.parent.mkdir()
    gh.write_text(
        '#!/bin/sh\necho "$*" >> "$FAKE/asked"\n'
        'case "$1" in pr) cat "$FAKE/pr.json" ;; api) cat "$FAKE/comments.json" ;; esac\n',
        encoding="utf-8",
    )
    gh.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{gh.parent}{os.pathsep}{os.environ['PATH']}",
        "FAKE": str(fake),
        "GIT_CONFIG_GLOBAL": str(empty_config),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }

    def git(cwd: Path, *args: str) -> str:
        done = subprocess.run(
            ["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True
        )
        return done.stdout.strip()

    origin, work, checkout = tmp_path / "origin.git", tmp_path / "work", tmp_path / "co"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    git(tmp_path, "init", "-q", "-b", "main", str(work))
    git(work, "remote", "add", "origin", str(origin))

    def commit(name: str, version: str | None = None) -> str:
        status = work / "docs" / "01_STATUS.md"
        status.parent.mkdir(exist_ok=True)
        if version:
            status.write_text(
                f"> Status: 21.10.2026 · Status version: {version}\n", encoding="utf-8"
            )
        (work / "work.txt").write_text(f"{name}\n", encoding="utf-8")
        git(work, "add", "-A")
        git(work, "commit", "-q", "-m", name)
        return git(work, "rev-parse", "HEAD")

    def land_on_main(name: str) -> None:
        git(work, "switch", "-q", "main")
        commit(name)
        git(work, "push", "-q", "origin", "main")

    base = commit("first", "1.0.0")
    git(work, "push", "-q", "origin", "main")
    git(work, "switch", "-q", "-c", "feature")
    head = commit("feature", "1.0.1")
    git(work, "push", "-q", "origin", "HEAD:refs/pull/7/head")

    def side_branch(number: int, *files: str, version: str | None = None) -> str:
        """A branch off main that changes only `files`, and the status version if given."""
        git(work, "switch", "-q", "-c", f"side-{number}", "main")
        for name in files:
            (work / name).parent.mkdir(parents=True, exist_ok=True)
            (work / name).write_text(f"{number}\n", encoding="utf-8")
        if version:
            header = f"> Status: 21.10.2026 · Status version: {version}\n"
            if (
                version == "prepended"
            ):  # a higher number in a note above the real header
                header = "<!-- Status version: 9.0.0 -->\n> Status: 21.10.2026 · Status version: 1.0.0\n"
            (work / "docs" / "01_STATUS.md").write_text(header, encoding="utf-8")
        git(work, "add", "-A")
        git(work, "commit", "-q", "-m", f"side {number}")
        git(work, "push", "-q", "origin", f"HEAD:refs/pull/{number}/head")
        sha = git(work, "rev-parse", "HEAD")
        git(work, "switch", "-q", "feature")
        return sha

    generated = side_branch(8, "README.md", ".github/assets/flow.svg")
    code = side_branch(9, "README.md", "api/app.py")
    versions = {
        v: (n, side_branch(n, "api/app.py", version=v))
        for n, v in ((10, "1.0.00"), (11, "1.0.0.1"), (12, "1.0.10"), (13, "prepended"))
    }
    git(tmp_path, "clone", "-q", str(origin), str(checkout))
    hook = checkout / ".claude" / "hooks" / "merge_gate.sh"
    hook.parent.mkdir(parents=True)
    shutil.copyfile(HOOK, hook)

    current = {"sha": head}

    def pr(checks=GREEN, branch="feature", sha=None, number=7, base="main") -> None:
        current["sha"] = sha or head
        body = {
            "number": number,
            "url": f"{origin.as_posix()}/pull/{number}",
            "headRefOid": current["sha"],
            "headRefName": branch,
            "baseRefName": base,
            "statusCheckRollup": checks,
        }
        (fake / "pr.json").write_text(json.dumps(body), encoding="utf-8")

    def comments(*items: dict) -> None:
        (fake / "comments.json").write_text(json.dumps(list(items)), encoding="utf-8")

    def codex(sha: str, open_findings: bool = False, at: str = "08:00") -> dict:
        state = {
            "code_review": {
                "commit_sha": sha,
                "has_unresolved_findings": open_findings,
                "status": "completed",
            }
        }
        body = f"<!-- codex-review-state:v1 {json.dumps(state)} -->\n\n## Codex"
        return {
            "user": {"login": CODEX},
            "author_association": "NONE",
            "updated_at": f"2026-10-21T{at}:00Z",
            "body": body,
        }

    def run(command: str | None = None, folder: Path = checkout):
        """`folder` is the hook input's cwd, where the command would run."""
        assert BASH is not None  # checked at the top of this fixture
        sha = current["sha"]
        command = (command or "gh pr merge 7 --merge --match-head-commit {sha}").format(
            sha=sha
        )
        payload = json.dumps(
            {
                "tool_name": "Bash",
                "tool_input": {"command": command},
                "cwd": str(folder),
            }
        )
        return subprocess.run(
            [BASH, hook.as_posix()],
            input=payload,
            cwd=tmp_path,
            env=env,
            capture_output=True,
            encoding="utf-8",
            timeout=60,
        )

    pr()
    comments(codex(head))
    return SimpleNamespace(
        git=git,
        checkout=checkout,
        base=base,
        head=head,
        generated=generated,
        code=code,
        versions=versions,
        pr=pr,
        comments=comments,
        codex=codex,
        run=run,
        land_on_main=land_on_main,
        asked=fake / "asked",
    )


def test_a_ready_pr_passes(gate):
    assert gate.run().returncode == 0


def test_other_commands_pass_without_asking_github(gate):
    for command in (
        "git status",
        "git merge origin/main",
        "gh pr view 7 && git merge origin/main",
        "gh pr view 7 --json mergeable,mergeStateStatus",
    ):
        assert gate.run(command).returncode == 0
    mention = gate.run('git commit -m "docs: gh pr merge needs a gate"')
    assert mention.returncode == 2
    assert "gh pr merg[e]" in mention.stderr
    assert not gate.asked.exists()


def test_a_pending_or_failed_check_blocks(gate):
    gate.pr(
        checks=[*GREEN, {"name": "lint", "status": "IN_PROGRESS", "conclusion": ""}]
    )
    done = gate.run()
    assert done.returncode == 2
    assert "lint: in_progress" in done.stderr

    gate.pr(checks=[{"context": "ci/legacy", "state": "FAILURE"}])
    assert "ci/legacy: failure" in gate.run().stderr

    gate.pr(checks=[])
    assert "no check reported yet" in gate.run().stderr


def test_a_check_that_ran_twice_counts_by_the_run_that_started_last(gate):
    def run(conclusion: str, started: str, completed: str) -> dict:
        return {
            "name": "title",
            "workflowName": "PR title",
            "status": "COMPLETED",
            "conclusion": conclusion,
            "startedAt": f"2026-10-21T{started}:00Z",
            "completedAt": f"2026-10-21T{completed}:00Z",
        }

    replaced = run("CANCELLED", "08:00", "08:03")  # cancelled after its successor won
    gate.pr(checks=[*GREEN, run("SUCCESS", "08:01", "08:02"), replaced])
    assert gate.run().returncode == 0

    gate.pr(
        checks=[
            *GREEN,
            run("SUCCESS", "08:00", "08:01"),
            run("FAILURE", "08:02", "08:03"),
        ]
    )
    done = gate.run()
    assert done.returncode == 2
    assert "title: failure" in done.stderr

    # The same job name in another workflow is a check of its own.
    other = {**run("FAILURE", "07:00", "07:01"), "workflowName": "Nightly"}
    gate.pr(checks=[*GREEN, run("SUCCESS", "08:01", "08:02"), other])
    assert gate.run().returncode == 2

    # A rerun still queued has no start time yet and is the newest run.
    queued = {"name": "title", "workflowName": "PR title", "status": "QUEUED"}
    gate.pr(checks=[*GREEN, run("SUCCESS", "08:00", "08:01"), queued])
    done = gate.run()
    assert done.returncode == 2
    assert "title: queued" in done.stderr

    # A run cancelled before it started is the oldest, not the newest.
    unstarted = {**queued, "status": "COMPLETED", "conclusion": "CANCELLED"}
    gate.pr(checks=[*GREEN, unstarted, run("SUCCESS", "08:00", "08:01")])
    assert gate.run().returncode == 0

    # Two runs that started in the same second go by their job id, in either order.
    def job(conclusion: str, number: int) -> dict:
        url = f"https://github.com/o/r/actions/runs/1/job/{number}"
        return {**run(conclusion, "08:00", "08:02"), "detailsUrl": url}

    for checks in (
        [job("SUCCESS", 900), job("CANCELLED", 899)],
        [job("CANCELLED", 899), job("SUCCESS", 900)],
    ):
        gate.pr(checks=[*GREEN, *checks])
        assert gate.run().returncode == 0
    gate.pr(checks=[*GREEN, job("SUCCESS", 899), job("FAILURE", 900)])
    assert gate.run().returncode == 2


def test_a_branch_behind_main_blocks(gate):
    gate.land_on_main("later")
    done = gate.run()
    assert done.returncode == 2
    assert "behind main" in done.stderr


def test_the_gate_fetches_from_the_repository_gh_picked(gate, tmp_path):
    # A fork clone whose origin knows neither main's history nor the pull ref,
    # while gh's default repository is the upstream named in the PR's URL.
    fork = tmp_path / "fork.git"
    gate.git(tmp_path, "init", "-q", "--bare", str(fork))
    gate.git(gate.checkout, "remote", "set-url", "origin", str(fork))
    assert gate.run().returncode == 0


def test_the_gate_checks_the_folder_the_command_runs_in(gate, tmp_path):
    # Not a clone of the repository: the head cannot be fetched there.
    (tmp_path / "elsewhere").mkdir()
    done = gate.run(folder=tmp_path / "elsewhere")
    assert done.returncode == 2
    assert "could not be fetched" in done.stderr


def test_an_unbumped_head_blocks_except_on_the_weekly_readme_branch(gate):
    # A head with main's own status version: main's commit itself.
    gate.pr(sha=gate.base)
    gate.comments(gate.codex(gate.base))
    done = gate.run()
    assert done.returncode == 2
    assert "not bumped (1.0.0 on main, 1.0.0" in done.stderr

    weekly = "chore/readme-weekly-2026-10-25"
    gate.pr(sha=gate.generated, number=8, branch=weekly)
    gate.comments(gate.codex(gate.generated))
    assert gate.run().returncode == 0

    # The name alone does not exempt a branch that changes more than generated files.
    gate.pr(sha=gate.code, number=9, branch=weekly)
    gate.comments(gate.codex(gate.code))
    done = gate.run()
    assert done.returncode == 2
    assert "not bumped" in done.stderr


@pytest.mark.parametrize(
    ("version", "bumped"),
    [("1.0.00", False), ("1.0.0.1", False), ("1.0.10", True), ("prepended", False)],
)
def test_the_bump_compares_three_numbers(gate, version, bumped):
    number, sha = gate.versions[version]
    gate.pr(sha=sha, number=number)
    gate.comments(gate.codex(sha))
    done = gate.run()
    assert (done.returncode == 0) == bumped, done.stderr
    assert ("not bumped" in done.stderr) != bumped


def test_a_pr_into_another_base_blocks(gate):
    gate.pr(base="release")
    done = gate.run()
    assert done.returncode == 2
    assert "targets release" in done.stderr


def test_the_review_must_be_clean_on_the_head(gate):
    gate.comments(gate.codex("0" * 40))
    done = gate.run()
    assert done.returncode == 2
    assert f'"Merge gate: {gate.head}"' in done.stderr

    gate.comments(gate.codex(gate.head, open_findings=True))
    assert gate.run().returncode == 2

    # Only Codex's newest state counts, whatever order the comments come in.
    clean, later = gate.codex(gate.head), gate.codex(gate.head, True, at="09:00")
    gate.comments(later, clean)
    assert gate.run().returncode == 2
    gate.comments({**clean, "updated_at": "2026-10-21T10:00:00Z"}, later)
    assert gate.run().returncode == 0


def test_a_merge_gate_comment_counts_only_from_the_owner_or_a_collaborator(gate):
    note = f"Merge gate: {gate.head}, own review: Codex is out of quota."
    outsider = {
        "user": {"login": "someone"},
        "author_association": "NONE",
        "created_at": "2026-10-21T11:00:00Z",
        "body": note,
    }
    gate.comments(outsider)
    assert gate.run().returncode == 2

    owner = {**outsider, "user": {"login": "maxi"}, "author_association": "OWNER"}
    gate.comments(outsider, owner)
    assert gate.run().returncode == 0

    quoted = {**owner, "body": f"The rule says to post\n{note}"}
    gate.comments(quoted)
    assert gate.run().returncode == 2

    # Seven characters could match another commit; the marker names the full sha.
    for body in (f"Merge gate: {gate.head[:7]}", f"Merge gate: {gate.head}0"):
        gate.comments({**owner, "body": body})
        assert gate.run().returncode == 2

    # A Codex state with findings that came after the marker wins over it.
    findings = gate.codex(gate.head, True, at="12:00")
    gate.comments(owner, findings)
    assert gate.run().returncode == 2
    # A marker after those findings is the review loop limits decision and counts.
    gate.comments(findings, {**owner, "created_at": "2026-10-21T13:00:00Z"})
    assert gate.run().returncode == 0


ONE_FORM = "the gate takes only"


@pytest.mark.parametrize(
    ("command", "reason"),
    [
        ("gh pr merge feature --merge --match-head-commit {sha}", ONE_FORM),
        ("gh pr merge --merge 7 --match-head-commit {sha}", ONE_FORM),
        ("gh pr merge 7 --merge", ONE_FORM),
        ("gh pr merge 7 --merge --match-head-commit abc1234", ONE_FORM),
        ("gh pr merge 7 --auto --merge --match-head-commit {sha}", ONE_FORM),
        ("gh pr merge 7 --squash --match-head-commit {sha}", ONE_FORM),
        ("gh pr merge 7 --merge --match-head-commit {sha} --rebase", ONE_FORM),
        ("if gh pr merge 7 --merge --match-head-commit {sha}; then :; fi", ONE_FORM),
        ("command gh pr merge 7 --merge --match-head-commit {sha}", ONE_FORM),
        ("gh pr \\\nmerge 7 --merge --match-head-commit {sha}", ONE_FORM),
        (
            "cd .\ngh pr merge 8 --merge\ntrue && "
            "gh pr merge 7 --merge --match-head-commit {sha}",
            ONE_FORM,
        ),
        (
            "gh pr merge https://github.com/other/repo/pull/7 "
            "--merge --match-head-commit {sha}",
            ONE_FORM,
        ),
        ("GH pr merge 7 --merge --match-head-commit {sha}", ONE_FORM),
        ("gh pr `\nmerge 7 --squash", ONE_FORM),
        ("gh -R someone/fork pr merge 7 --merge --match-head-commit {sha}", ONE_FORM),
        ("gh pr -R someone/fork merge 7 --merge --match-head-commit {sha}", ONE_FORM),
        ("gh --repo a/b pr merge 7 --merge --match-head-commit {sha}", ONE_FORM),
        ("cd ../other && gh pr merge 7 --merge --match-head-commit {sha}", ONE_FORM),
        ("gh.exe pr merge 7 --squash", ONE_FORM),
        ("gh pr merge 7 --merge --match-head-commit " + "0" * 40, "the PR's head is"),
    ],
)
def test_the_command_must_pin_the_pr_and_its_head(gate, command, reason):
    done = gate.run(command)
    assert done.returncode == 2
    assert reason in done.stderr


def test_a_stalled_gate_blocks_but_a_missing_script_does_not(gate, tmp_path):
    """onFailure "block" turns a timeout or crash into a block. A session whose
    CLAUDE_PROJECT_DIR points elsewhere (moved after start) has no script there;
    that must not block every shell command."""
    settings = json.loads((HOOK.parents[1] / "settings.json").read_text("utf-8"))
    [entry] = [
        hook
        for group in settings["hooks"]["PreToolUse"]
        for hook in group["hooks"]
        if "merge_gate.sh" in hook["command"]
    ]
    assert entry["onFailure"] == "block"
    done = subprocess.run(
        [BASH, "-c", entry["command"]],
        input='{"tool_input": {"command": "gh pr merge 7 --merge"}}',
        env={**os.environ, "CLAUDE_PROJECT_DIR": str(tmp_path / "elsewhere")},
        capture_output=True,
        encoding="utf-8",
        timeout=60,
    )
    assert done.returncode == 0, done.stderr
