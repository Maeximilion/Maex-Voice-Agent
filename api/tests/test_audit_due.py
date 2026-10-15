"""`scripts/audit_due.sh` against a throwaway repository with its own audit log.

The script decides when a security audit is due (docs/09_OPERATIONS_LEGAL.md
section 5). Needs bash and git; the api container has no git, so the tests are
skipped there and fail in CI when a tool is missing.
"""

import datetime as dt
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from api.tests.test_scripts_backup import BASH

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "audit_due.sh"
HEADER = "## 5. Security audit\n\n### Audit log\n\n| Date | Kind | Commit | Scope | Result |\n|---|---|---|---|---|\n"


class Repo(tuple):
    """commit, log, due to unpack; git and run as attributes."""


def day(days_ago: int) -> str:
    return (dt.datetime.now(dt.UTC).date() - dt.timedelta(days=days_ago)).strftime(
        "%d.%m.%Y"
    )


@pytest.fixture
def repo(tmp_path):
    missing = "bash" if BASH is None else None if shutil.which("git") else "git"
    if missing:
        if os.environ.get("CI"):
            pytest.fail(f"audit tests need {missing}")
        pytest.skip(f"audit tests need {missing}, which the api container lacks")
    empty = tmp_path / "gitconfig"
    empty.write_text("", encoding="utf-8")
    env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": str(empty),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }
    root = tmp_path / "r"
    (root / "scripts").mkdir(parents=True)
    (root / "docs").mkdir()
    shutil.copy(SCRIPT, root / "scripts" / "audit_due.sh")

    def git(*args):
        return subprocess.run(
            ["git", *args],
            cwd=root,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def commit(path: str) -> str:
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"{path} {os.urandom(4).hex()}\n", encoding="utf-8")
        git("add", "-A")
        git("commit", "-qm", path)
        return git("rev-parse", "--short", "HEAD")

    def log(*rows):
        text = HEADER + "".join(
            f"| {d} | {k} | `{c}` | {s} | ok |\n" for d, k, c, s in rows
        )
        (root / "docs" / "09_OPERATIONS_LEGAL.md").write_text(text, encoding="utf-8")
        git("add", "docs")
        git("commit", "-qm", "audit log")

    def tasks(text):
        (root / "docs" / "07_WORKPACKAGES.md").write_text(text, encoding="utf-8")
        git("add", "docs")
        git("commit", "-qm", "tasks")

    def run(*args, main="HEAD"):
        git("update-ref", "refs/remotes/origin/main", main)
        return subprocess.run(
            [BASH, str(root / "scripts" / "audit_due.sh"), *args],
            env=env,
            capture_output=True,
            text=True,
        )

    def due(*args):
        result = run(*args)
        assert result.returncode == 0, result.stderr
        return result.stdout

    git("init", "-q")
    handles = Repo((commit, log, due))
    handles.git, handles.run, handles.tasks = git, run, tasks
    return handles


def test_a_component_audit_keeps_the_baseline(repo):
    commit, log, due = repo
    full = commit("api/core/a.py")
    commit("api/tools/b.py")  # changed after the full audit
    component = commit("deploy/asterisk/c.conf")
    log((day(40), "full", full, "all"), (day(1), "component", component, "SIP"))
    out = due()
    assert f"baseline: {day(40)} {full}" in out
    assert "exposed files changed since: 2" in out
    # The files are named, so a scoped audit knows its scope.
    assert "  api/tools/b.py\n  deploy/asterisk/c.conf\n" in out
    assert "audit due: yes, scoped" in out


def test_no_scoped_audit_before_30_days_or_without_changes(repo):
    commit, log, due = repo
    base = commit("api/core/a.py")
    commit("api/agent/loop.py")
    log((day(5), "full", base, "all"))
    assert "audit due: no" in due()
    log((day(31), "scoped", base, "diff"))
    assert "audit due: yes, scoped" in due()
    commit("api/tests/test_x.py")
    commit("docs/notes.md")
    head = commit("README.md")
    log((day(31), "full", head, "all"))
    assert "exposed files changed since: 0" in due()


def test_gate_is_due_on_any_change_and_full_after_30_days(repo):
    commit, log, due = repo
    base = commit("api/core/a.py")
    commit("n8n/flow.json")
    log((day(3), "full", base, "all"))
    assert "audit due: yes, scoped" in due("--gate")
    log((day(45), "full", base, "all"))
    assert "audit due: yes, full" in due("--gate")


def test_task_audit_matches_the_exact_task_id(repo):
    commit, log, due = repo
    base = commit("api/core/a.py")
    log((day(1), "component", base, "SIP fixes of T-10.13"))
    assert "T-10.13: recorded" in due("--task", "T-10.13")
    assert "T-10.1: due" in due("--task", "T-10.1")


def test_a_task_audit_is_due_again_after_a_change(repo):
    commit, log, due = repo
    audited = commit("deploy/asterisk/extensions.conf")
    log((day(2), "component", audited, "first outside call, T-10.1"))
    assert "audit for T-10.1: recorded" in due("--task", "T-10.1")
    commit("deploy/asterisk/extensions.conf")
    out = due("--task", "T-10.1")
    assert "audit for T-10.1: due again" in out
    assert "  deploy/asterisk/extensions.conf\n" in out


def test_a_commit_that_is_not_on_main_is_refused(repo):
    commit, log, _ = repo
    commit("api/core/a.py")
    repo.git("checkout", "-qb", "feature")
    gone = commit("api/core/b.py")
    repo.git("checkout", "-q", "-")
    log((day(1), "full", gone, "all"))
    result = repo.run()
    assert result.returncode == 2
    assert f"commit {gone} of {day(1)} is not on origin/main" in result.stderr


def test_an_uncommitted_edit_makes_a_task_audit_due_again(repo, tmp_path):
    commit, log, due = repo
    audited = commit("deploy/Caddyfile")
    log((day(1), "component", audited, "production server, T-9.2"))
    assert "audit for T-9.2: recorded" in due("--task", "T-9.2")
    (tmp_path / "r" / "deploy" / "Caddyfile").write_text("new path\n", encoding="utf-8")
    (tmp_path / "r" / "deploy" / "new.conf").write_text("port\n", encoding="utf-8")
    out = due("--task", "T-9.2")
    assert "  deploy/Caddyfile\n  deploy/new.conf\n" in out
    assert "due again" in out


def test_a_long_row_naming_many_commits_reads_the_commit_column(repo):
    commit, log, due = repo
    base = commit("api/core/a.py")
    many = " ".join(f"`{n:07x}`" for n in range(300))
    log((day(1), "full", base, f"all, see {many}"))
    assert f"baseline: {day(1)} {base}" in due()


def test_tasks_checks_only_open_rows_with_the_note(repo):
    commit, log, due = repo
    base = commit("api/core/a.py")
    log((day(1), "component", base, "T-9.2"))
    note = "Security audit before go-live (docs/09 §5)"
    repo.tasks(
        f"| T-9.2 | - | server. {note} | 13 | - | open |\n"
        f"| T-10.1 | - | router. {note} | 20 | - | in progress, step 1 done |\n"
        f"| T-10.12 | - | trunk. {note} | 20 | - | deferred |\n"
        "| T-3.1 | - | GUI | 06 | - | open |\n"
    )
    out = due("--tasks")
    assert "audit for T-9.2: recorded" in out
    assert "audit for T-10.1: due, none recorded" in out
    assert "T-10.12" not in out
    assert "T-3.1" not in out


def test_tasks_judges_main_from_a_worktree_that_is_behind(repo):
    commit, log, _ = repo
    audited = commit("api/core/a.py")
    log((day(1), "component", audited, "T-9.2"))
    note = "Security audit before go-live (docs/09 §5)"
    repo.tasks(f"| T-9.2 | - | server. {note} | 13 | - | open |\n")
    newer = commit("api/tools/b.py")
    # The worktree is back at the audited commit, without the docs main has.
    repo.git("checkout", "-q", audited)
    out = repo.run("--tasks", main=newer).stdout
    assert "audit for T-9.2: due again" in out
    assert "  api/tools/b.py\n" in out


def test_prompt_markdown_counts_and_readmes_do_not(repo):
    commit, log, due = repo
    base = commit("api/core/a.py")
    log((day(40), "full", base, "all"))
    commit("n8n/README.md")
    commit("docs/notes.md")
    assert "exposed files changed since: 0" in due()
    commit("prompts/system_v2.md")
    out = due()
    assert "exposed files changed since: 1" in out
    assert "  prompts/system_v2.md\n" in out


def test_a_staged_change_counts_when_the_worktree_is_restored(repo, tmp_path):
    commit, log, due = repo
    audited = commit("deploy/Caddyfile")
    log((day(1), "component", audited, "T-9.2"))
    caddyfile = tmp_path / "r" / "deploy" / "Caddyfile"
    original = caddyfile.read_text(encoding="utf-8")
    caddyfile.write_text("public path\n", encoding="utf-8")
    repo.git("add", "deploy/Caddyfile")
    caddyfile.write_text(original, encoding="utf-8")
    out = due("--task", "T-9.2")
    assert "  deploy/Caddyfile\n" in out
    assert "due again" in out


def test_main_judges_with_its_own_copy_of_the_script(repo, tmp_path):
    commit, log, _ = repo
    behind = commit("api/core/a.py")
    log((day(1), "full", behind, "all"))
    script = tmp_path / "r" / "scripts" / "audit_due.sh"
    text = script.read_text(encoding="utf-8")
    script.write_text(
        text.replace("set -euo pipefail\n", "set -euo pipefail\necho 'main copy'\n", 1),
        encoding="utf-8",
    )
    repo.git("commit", "-qam", "main changes the checker")
    newer = repo.git("rev-parse", "--short", "HEAD")
    repo.git("checkout", "-q", behind)
    out = repo.run(main=newer).stdout
    assert out.startswith("main copy\n")
    assert f"baseline: {day(1)} {behind}" in out
