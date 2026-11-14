"""`.claude/hooks/graphify_ready.sh`: a missing code graph is built at session start.

`graphify-out/` is gitignored, so every new checkout and worktree starts without a graph.
The hook builds it for the session's working folder and for the project folder, which
differ in a desktop app worktree session. A stub stands in for the `graphify` CLI. Needs
bash and jq; under `make test` (api container) the tests are skipped, in CI a missing
tool is a failure.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from api.tests.test_scripts_backup import BASH

ROOT = Path(__file__).resolve().parents[2]
HOOK = ROOT / ".claude" / "hooks" / "graphify_ready.sh"
STUB = """#!/usr/bin/env bash
echo "$PWD" >> "$STUB_LOG"
mkdir -p graphify-out && echo '{}' > graphify-out/graph.json
"""


def _checkout(path: Path) -> Path:
    path.mkdir(parents=True)
    (path / "CLAUDE.md").write_text("rules\n")
    (path / ".git").write_text("gitdir: elsewhere\n")
    return path


@pytest.fixture
def run(tmp_path):
    missing = "bash" if BASH is None else None if shutil.which("jq") else "jq"
    if missing:
        if os.environ.get("CI"):
            pytest.fail(f"graphify ready tests need {missing}")
        pytest.skip(f"graphify ready tests need {missing}")
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    stub = stub_dir / "graphify"
    stub.write_text(STUB, newline="\n")
    stub.chmod(0o755)
    log = tmp_path / "calls.log"

    def _run(cwd: Path, project: Path | None = None, installed: bool = True):
        assert BASH is not None
        env = dict(os.environ)
        # A real graphify on the machine must not answer for the stub or for its absence.
        dirs = [
            d
            for d in env.get("PATH", "").split(os.pathsep)
            if d and not shutil.which("graphify", path=d)
        ]
        env["PATH"] = os.pathsep.join(([str(stub_dir)] if installed else []) + dirs)
        env["STUB_LOG"] = str(log)
        env["GRAPHIFY_READY_WAIT"] = "1"
        env.pop("CLAUDE_PROJECT_DIR", None)
        if project is not None:
            env["CLAUDE_PROJECT_DIR"] = str(project)
        out = subprocess.run(
            [BASH, str(HOOK)],
            input=json.dumps({"hook_event_name": "SessionStart", "cwd": str(cwd)}),
            capture_output=True,
            text=True,
            env=env,
            cwd=project or cwd,
            check=True,
        ).stdout
        built = log.read_text().split() if log.exists() else []
        return (json.loads(out) if out.strip() else None), built

    return _run


def test_a_missing_graph_is_built_and_announced(run, tmp_path):
    wt = _checkout(tmp_path / "wt")
    out, built = run(wt)
    assert (wt / "graphify-out" / "graph.json").is_file()
    assert len(built) == 1
    assert not (wt / "graphify-out" / ".building").exists()
    note = out["hookSpecificOutput"]["additionalContext"]
    assert "building it in the background" in note
    assert out["systemMessage"] == note
    assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart"


def test_an_existing_graph_is_left_alone_in_silence(run, tmp_path):
    wt = _checkout(tmp_path / "wt")
    (wt / "graphify-out").mkdir()
    (wt / "graphify-out" / "graph.json").write_text("{}")
    assert run(wt) == (None, [])


def test_the_working_folder_and_the_project_folder_both_get_a_graph(run, tmp_path):
    # Desktop app worktree session: the guards run in the main checkout.
    main = _checkout(tmp_path / "main")
    wt = _checkout(main / ".claude" / "worktrees" / "wt")
    out, built = run(wt, project=main)
    assert (wt / "graphify-out" / "graph.json").is_file()
    assert (main / "graphify-out" / "graph.json").is_file()
    assert len(built) == 2
    assert out is not None


def test_only_the_folder_without_a_graph_is_built(run, tmp_path):
    main = _checkout(tmp_path / "main")
    (main / "graphify-out").mkdir()
    (main / "graphify-out" / "graph.json").write_text("{}")
    wt = _checkout(main / ".claude" / "worktrees" / "wt")
    _, built = run(wt, project=main)
    assert len(built) == 1
    assert (wt / "graphify-out" / "graph.json").is_file()


def test_a_running_build_is_not_started_twice(run, tmp_path):
    wt = _checkout(tmp_path / "wt")
    (wt / "graphify-out" / ".building").mkdir(parents=True)
    assert run(wt) == (None, [])
    assert not (wt / "graphify-out" / "graph.json").exists()


def test_a_folder_that_is_no_checkout_gets_no_graph(run, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert run(plain) == (None, [])
    assert not (plain / "graphify-out").exists()


def test_without_graphify_it_names_the_install_steps_and_builds_nothing(run, tmp_path):
    wt = _checkout(tmp_path / "wt")
    out, built = run(wt, installed=False)
    assert built == []
    assert not (wt / "graphify-out").exists()
    assert "docs/19_CLAUDE_CODE_SETUP.md" in out["systemMessage"]
