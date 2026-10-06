"""agent/prompt.py: liest prompts/system_vN.md und hängt einen Menü-Index an, wenn einer da ist."""

import math
from pathlib import Path

from api.agent.dispatch import TOOLS
from api.agent.llm import OUTPUT_FORMAT
from api.agent.prompt import CORE_TOKEN_BUDGET, PROMPT_VERSION, build_system_prompt

SYSTEM_CURRENT = (
    (Path(__file__).resolve().parents[2] / "prompts" / f"system_{PROMPT_VERSION}.md")
    .read_text(encoding="utf-8")
    .strip()
)


def test_ohne_menue_index_kommt_der_reine_system_prompt():
    assert build_system_prompt() == SYSTEM_CURRENT


def test_mit_menue_index_wird_er_angehaengt():
    prompt = build_system_prompt("23 Frühlingsrollen · 47 Ente knusprig")

    assert prompt.startswith(SYSTEM_CURRENT)
    assert "# Menü" in prompt
    assert "23 Frühlingsrollen" in prompt


def test_aeltere_version_bleibt_ladbar():
    assert build_system_prompt(version="v1").startswith("# Rolle")


def test_leerer_menue_index_wird_wie_kein_index_behandelt():
    assert build_system_prompt("") == SYSTEM_CURRENT


# --- The tool reference for a real model (T-2.4 part 3) -------------------------


def test_tool_reference_names_every_tool_the_core_can_call():
    """A model can only call what it knows: every tool of `dispatch.TOOLS` with
    its arguments, after the system prompt."""
    prompt = build_system_prompt(tools=True)

    assert prompt.startswith(SYSTEM_CURRENT)
    for tool in TOOLS:
        assert f"## {tool}" in prompt


def test_tool_reference_leaves_out_what_is_not_for_the_model():
    """The file also serves the voice platform: its note on HTTP and the bearer
    token, and the tools that do not exist yet, stay out of the prompt."""
    prompt = build_system_prompt(tools=True)

    assert "Bearer" not in prompt
    assert "AGENT_API_TOKEN" not in prompt
    assert "find_customer" not in prompt
    assert "check_delivery" not in prompt


def test_menu_index_comes_after_the_tool_reference():
    prompt = build_system_prompt("23 Frühlingsrollen", tools=True)

    assert prompt.index("## confirm") < prompt.index("# Menü")
    assert prompt.endswith("23 Frühlingsrollen")


def test_what_the_core_sends_stays_inside_its_budget():
    """Rule 6 measured on what a model really gets on every request: system
    prompt, tool reference and answer format. The 800 of docs/05 §5 is the
    budget of `system_vN.md` alone (test_prompts_v2.py)."""
    sent = f"{build_system_prompt(tools=True)}\n\n{OUTPUT_FORMAT}"
    tokens = math.ceil(len(sent) / 4)

    assert tokens < CORE_TOKEN_BUDGET, f"~{tokens} tokens, budget {CORE_TOKEN_BUDGET}"
