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

    def due(*args):
        git("update-ref", "refs/remotes/origin/main", "HEAD")
        return subprocess.run(
            [BASH, str(root / "scripts" / "audit_due.sh"), *args],
            env=env,
            check=True,
            capture_output=True,
            text=True,
        ).stdout

    git("init", "-q")
    return commit, log, due


def test_a_component_audit_keeps_the_baseline(repo):
    commit, log, due = repo
    full = commit("api/core/a.py")
    commit("api/tools/b.py")  # changed after the full audit
    component = commit("deploy/asterisk/c.conf")
    log((day(40), "full", full, "all"), (day(1), "component", component, "SIP"))
    out = due()
    assert f"baseline: {day(40)} {full}" in out
    assert "exposed files changed since: 2" in out
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
