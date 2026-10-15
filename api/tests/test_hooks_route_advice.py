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
HOOKS_PROMPT = "Check the hooks and the settings of this project, please"


@pytest.fixture
def run(tmp_path, request):
    missing = "bash" if BASH is None else None if shutil.which("jq") else "jq"
    if missing:
        if os.environ.get("CI"):
            pytest.fail(f"route advice tests need {missing}")
        pytest.skip(f"route advice tests need {missing}")

    def _run(
        prompt,
        model="claude-sonnet-5-5",
        effort="medium",
        sid=None,
        level=None,
        **extra,
    ):
        payload = {
            "session_id": sid or request.node.name,
            "hook_event_name": "UserPromptSubmit",
            "prompt": prompt,
            "model": model,
            **extra,
        }
        env = {
            k: v
            for k, v in os.environ.items()
            if k not in ("CLAUDE_EFFORT", "CLAUDE_CODE_EFFORT_LEVEL")
        }
        env |= {"HOME": str(tmp_path), "MSYS_NO_PATHCONV": "1"}
        if effort:
            env["CLAUDE_EFFORT"] = effort
        if level:
            env["CLAUDE_CODE_EFFORT_LEVEL"] = level
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


def message(out):
    return out["systemMessage"]


def test_default_work_gets_opus_high(run):
    out = run(HOOKS_PROMPT)
    assert "claude-opus-5-5 at effort high" in message(out)
    assert "decision" not in out


def test_trigger_words_raise_effort_to_xhigh(run):
    out = run("Fix the migration for the delivery zone", "claude-opus-5-5", "high")
    assert "effort xhigh" in message(out)


def test_gui_work_gets_sonnet_medium_and_a_stronger_session_a_thread(run):
    out = run("Adjust the GUI template for the order list", "claude-opus-5-5", "high")
    assert "claude-sonnet-5-5 at effort medium" in message(out)
    assert "fresh thread" in message(out)


def test_docs_only_prompt_stays_on_sonnet_despite_hard_rule_words(run):
    prompt = "Update docs/03_DATA_MODEL.md to correct a typo in the migration section"
    assert run(prompt) is None
    out = run(prompt + " and in db/migrations/007.py", sid="code")
    assert "effort xhigh" in message(out)


def test_pure_search_goes_to_haiku(run):
    out = run("Search the repository for every reference to route_advice")
    assert "claude-haiku-4-5 at effort none" in message(out)
    assert run("Find and fix every caller of the hook", sid="fix") is not None


def test_short_task_is_routed_and_answers_are_not(run):
    assert "effort xhigh" in message(run("Fix migration downgrade"))
    assert run("ok, weiter") is None


def test_slash_commands_without_their_own_check_are_routed(run):
    out = run('/bug "Migration fails while upgrading the schema"')
    assert "effort xhigh" in message(out)


def test_full_model_id_is_compared(run):
    out = run(HOOKS_PROMPT, model="claude-opus-4-6", effort="high")
    assert "claude-opus-5-5 at effort high" in message(out)
    assert run(HOOKS_PROMPT, "claude-opus-5-5[1m]", "high", sid="suffix") is None


def test_claude_code_effort_variable_is_read(run):
    out = run("Fix the migration downgrade", "claude-opus-5-5", effort="", level="high")
    assert "effort high" in message(out)


def test_unknown_effort_is_asked_about_only_for_xhigh(run):
    assert run(HOOKS_PROMPT, "claude-opus-5-5", effort="") is None
    out = run("Fix the migration downgrade", "claude-opus-5-5", "", sid="x")
    assert "effort unknown" in message(out)


BRIEF = "Route: claude-sonnet-5-5 medium\nUpdate the GUI screen for the migration."


def test_route_line_that_matches_the_thread_is_silent(run):
    # The brief names "migration", which alone would ask for xhigh.
    assert run(BRIEF, "claude-sonnet-5-5", "medium") is None


def test_mismatch_stops_once_then_carries_on_then_is_silent(run):
    first = run(BRIEF, "claude-opus-5-5", "xhigh")
    assert "the brief asks for claude-sonnet-5-5 at effort medium" in message(first)
    context = first["hookSpecificOutput"]["additionalContext"]
    assert "do not start the task" in context
    assert "ROUTE-MISMATCH want=claude-sonnet-5-5 medium" in context
    assert "have=claude-opus-5-5 xhigh" in context
    # The resend still on the wrong setting: no second stop, no loop.
    second = run(BRIEF, "claude-opus-5-5", "xhigh")
    assert "carry on" in second["hookSpecificOutput"]["additionalContext"]
    assert run(BRIEF, "claude-opus-5-5", "xhigh") is None


def test_switched_thread_gets_the_resent_brief_without_a_note(run):
    assert run(BRIEF, "claude-opus-5-5", "xhigh") is not None
    assert run(BRIEF, "claude-sonnet-5-5", "medium") is None


def test_route_line_is_read_after_a_wrapper_tag(run):
    out = run(f"<brief>\n{BRIEF}\n</brief>", "claude-opus-5-5", "high")
    assert "routing check" in message(out)


def test_route_line_further_down_is_only_text(run):
    prompt = f"Update this hook, the fixture reads:\nRoute: claude-haiku-4-5 none\n{HOOKS_PROMPT}"
    out = run(prompt, "claude-haiku-4-5", effort="")
    assert "routing advice: this prompt fits claude-opus-5-5" in message(out)


def test_route_line_without_effort_compares_the_model_only(run):
    brief = "Route: claude-haiku-4-5 none\nFind every caller of confirm."
    assert run(brief, "claude-haiku-4-5", effort="") is None
    assert "routing check" in message(run(brief, "claude-sonnet-5-5", sid="other"))


@pytest.mark.parametrize(
    "prompt, extra",
    [
        ("ja", {}),
        ("/task T-1.2 start it now without further questions", {}),
        ("/done", {}),
        ("<task-notification>done</task-notification> " + HOOKS_PROMPT, {}),
        (HOOKS_PROMPT, {"agent_id": "sub1"}),
    ],
)
def test_prompts_without_a_task_are_left_alone(run, prompt, extra):
    assert run(prompt, **extra) is None


def test_matching_session_is_silent_and_advice_is_given_once(run):
    assert run(HOOKS_PROMPT, "claude-opus-5-5", "high") is None
    assert run(HOOKS_PROMPT, sid="again") is not None
    assert run(HOOKS_PROMPT, sid="again") is None


def test_a_pure_search_for_a_hard_rule_term_stays_on_haiku(run):
    out = run("Search the repository for every menu_item_id reference")
    assert "claude-haiku-4-5 at effort none" in message(out)


@pytest.mark.parametrize(
    "prompt",
    [
        "Add tests for existing code behavior in the order list",
        "Fix the review comments on this pull request",
        "Confirm the existing GUI tests pass",
    ],
)
def test_sonnet_work_from_the_table_is_recognised(run, prompt):
    assert run(prompt) is None


def test_confirm_counts_only_with_an_order_booking_or_its_code(run):
    out = run("Change how confirm handles the order", "claude-opus-5-5", "high")
    assert "effort xhigh" in message(out)
    out = run("Make sure we confirm the order only after a yes", sid="order")
    assert "effort xhigh" in message(out)


def test_a_mixed_docs_and_code_task_keeps_the_higher_setting(run):
    prompt = "Update README and domain order logic to fix the migration downgrade"
    assert "effort xhigh" in message(run(prompt))


def test_a_python_import_is_not_a_data_import(run):
    out = run("Fix the circular import in core/config.py")
    assert "claude-opus-5-5 at effort high" in message(out)
    assert run("Run the menu import for the new register export", sid="menu") is None


@pytest.mark.parametrize(
    "brief",
    [
        "Route: claude-opus-5-5 xhgh\nUpdate the domain code.",
        "Route: claude-opus-5-5\nUpdate the domain code.",
        "Route: claude-opus-5-5 high please\nUpdate the domain code.",
    ],
)
def test_a_malformed_route_line_is_reported(run, brief):
    out = run(brief, "claude-opus-5-5", "high")
    assert "Route line is not" in message(out)
    assert "Carry on" in out["hookSpecificOutput"]["additionalContext"]
