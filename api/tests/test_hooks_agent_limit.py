"""`.claude/hooks/agent_limit.sh`: today's row of the weekday agent table, never a block.

The hook reads the "Agents per weekday" table in CLAUDE.md, so the table stays the only
place the numbers live. Needs bash and jq; under `make test` (api container) the tests
are skipped, in CI a missing tool is a failure.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from api.tests.test_scripts_backup import BASH

ROOT = Path(__file__).resolve().parents[2]
HOOK = ROOT / ".claude" / "hooks" / "agent_limit.sh"
DAYS = (
    "Sunday",
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
)


@pytest.fixture
def run():
    missing = "bash" if BASH is None else None if shutil.which("jq") else "jq"
    if missing:
        if os.environ.get("CI"):
            pytest.fail(f"agent limit tests need {missing}")
        pytest.skip(f"agent limit tests need {missing}")

    def _run(day=None, hook=HOOK):
        assert BASH is not None
        env = dict(os.environ)
        env.pop("AGENT_LIMIT_DAY", None)
        if day is not None:
            env["AGENT_LIMIT_DAY"] = day
        out = subprocess.run(
            [BASH, str(hook)],
            input=json.dumps({"hook_event_name": "SessionStart"}),
            capture_output=True,
            text=True,
            env=env,
            check=True,
        ).stdout
        return json.loads(out) if out.strip() else None

    return _run


@pytest.mark.parametrize(
    ("day", "agents", "runs"),
    [
        ("Sunday", "no limit", "welcome"),
        ("Monday", "10", "welcome"),
        ("Wednesday", "5", "only when clearly worth it"),
        ("Saturday", "8", "when they help"),
    ],
)
def test_the_hook_names_todays_row_of_the_table(run, day, agents, runs):
    out = run(day)

    message = out["systemMessage"]
    assert f"({day}, CLAUDE.md): {agents} agents at once" in message
    assert message.endswith(f"review and audit agents: {runs}.")
    assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert message in out["hookSpecificOutput"]["additionalContext"]
    assert "decision" not in out


def test_every_day_of_the_week_has_its_row(run):
    for day in DAYS:
        assert run(day) is not None, day


def test_the_day_of_the_clock_is_one_of_the_rows(run):
    assert run()["systemMessage"].split("(")[1].split(",")[0] in DAYS


@pytest.mark.parametrize("table", ["", "| Monday |  |  |\n"])
def test_without_a_row_the_hook_says_nothing(run, tmp_path, table):
    """A CLAUDE.md without the table, or a row with an empty cell, is no limit
    the hook could name: it stays silent and never blocks the session."""
    hooks = tmp_path / ".claude" / "hooks"
    hooks.mkdir(parents=True)
    shutil.copy(HOOK, hooks)
    (tmp_path / "CLAUDE.md").write_text(f"# CLAUDE.md\n\n{table}", encoding="utf-8")

    assert run("Monday", hook=hooks / HOOK.name) is None
    assert run("Someday") is None


def test_a_quote_in_the_table_stays_valid_json(run, tmp_path):
    hooks = tmp_path / ".claude" / "hooks"
    hooks.mkdir(parents=True)
    shutil.copy(HOOK, hooks)
    (tmp_path / "CLAUDE.md").write_text(
        '| Monday | 3 | only when it\'s "worth it" |\n', encoding="utf-8"
    )

    out = run("Monday", hook=hooks / HOOK.name)

    assert out["systemMessage"].endswith('only when it\'s "worth it".')


def test_the_hook_runs_at_every_session_start():
    settings = json.loads((ROOT / ".claude" / "settings.json").read_text())
    commands = [
        hook["command"]
        for entry in settings["hooks"]["SessionStart"]
        if "matcher" not in entry
        for hook in entry["hooks"]
    ]
    assert any("agent_limit.sh" in command for command in commands)
