"""`.claude/hooks/route_advice.sh`: advice only, never a block.

The hook compares the session's model and effort with the Model Routing table in
CLAUDE.md. Needs bash and jq; under `make test` (api container) the tests are skipped,
in CI a missing tool is a failure.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from api.tests.test_scripts_backup import BASH

HOOK = Path(__file__).resolve().parents[2] / ".claude" / "hooks" / "route_advice.sh"
LONG = " bitte zuegig erledigen, danke"


@pytest.fixture
def run(tmp_path, request):
    missing = "bash" if BASH is None else None if shutil.which("jq") else "jq"
    if missing:
        if os.environ.get("CI"):
            pytest.fail(f"route advice tests need {missing}")
        pytest.skip(f"route advice tests need {missing}")

    def _run(prompt, model="claude-sonnet-5-5", effort="medium", sid=None, **extra):
        payload = {
            "session_id": sid or request.node.name,
            "hook_event_name": "UserPromptSubmit",
            "prompt": prompt,
            "model": model,
            **extra,
        }
        env = {
            **os.environ,
            "HOME": str(tmp_path),
            "CLAUDE_EFFORT": effort,
            "MSYS_NO_PATHCONV": "1",
        }
        out = subprocess.run(
            [BASH, str(HOOK)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env=env,
            check=True,
        ).stdout
        return json.loads(out) if out.strip() else None

    return _run


def test_default_work_gets_opus_high(run):
    out = run("Pruefe die Hooks und die Einstellungen im Projekt" + LONG)
    assert "Opus 5.5 at effort high" in out["systemMessage"]
    assert "decision" not in out


def test_trigger_words_raise_effort_to_xhigh(run):
    out = run(
        "Aendere die Migration fuer die Lieferzone",
        model="claude-opus-5-5",
        effort="high",
    )
    assert "effort xhigh" in out["systemMessage"]


def test_gui_and_docs_work_gets_sonnet_medium(run):
    out = run(
        "Passe das GUI Template und die Docs an" + LONG,
        model="claude-opus-5-5",
        effort="high",
    )
    assert "Sonnet 5.5 at effort medium" in out["systemMessage"]
    # A stronger session is pointed at a thread, not told to switch up.
    assert "fresh thread" in out["systemMessage"]


BRIEF = (
    "Route: claude-sonnet-5-5 medium\nUpdate docs/06_GUI.md for the migration screen."
)


def test_route_line_that_matches_the_thread_is_silent(run):
    # The brief names "migration", which alone would ask for xhigh.
    assert run(BRIEF, model="claude-sonnet-5-5", effort="medium") is None


def test_route_line_that_the_thread_did_not_get_is_reported(run):
    out = run(BRIEF, model="claude-opus-5-5", effort="xhigh")
    assert (
        "the brief asks for claude-sonnet-5-5 at effort medium" in out["systemMessage"]
    )
    assert "session is on claude-opus-5-5, effort xhigh" in out["systemMessage"]


def test_route_line_is_read_inside_a_tagged_brief(run):
    out = run(f"<brief>\n{BRIEF}\n</brief>", model="claude-opus-5-5", effort="high")
    assert "routing check" in out["systemMessage"]


def test_route_line_without_effort_compares_the_model_only(run):
    brief = "Route: claude-haiku-4-5 none\nFind every caller of confirm."
    assert run(brief, model="claude-haiku-4-5", effort="") is None
    assert "routing check" in run(brief, model="claude-sonnet-5-5")["systemMessage"]


@pytest.mark.parametrize(
    "prompt, extra",
    [
        ("ja weiter", {}),
        ("/task T-1.2 bitte jetzt sofort starten" + LONG, {}),
        ("<task-notification>done</task-notification>" + LONG, {}),
        ("Pruefe die Hooks im Projekt" + LONG, {"agent_id": "sub1"}),
    ],
)
def test_prompts_without_a_task_are_left_alone(run, prompt, extra):
    assert run(prompt, **extra) is None


def test_matching_session_is_silent_and_advice_is_given_once(run):
    prompt = "Pruefe die Hooks und die Einstellungen im Projekt" + LONG
    assert run(prompt, model="claude-opus-5-5", effort="high") is None
    assert run(prompt, sid="again") is not None
    assert run(prompt, sid="again") is None
