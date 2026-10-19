"""System-Prompt aus `prompts/system_vN.md` plus Menü-Index bauen (docs/05 §1, §5).

Version 2 kennt die Menü-Tools und die Abholung. Der Menü-Index (Nummer und Name,
nie die ganze Karte, CLAUDE.md §2 Regel 6) wird angehängt, sobald ein Aufrufer ihn
mitgibt.

`tools=True` adds the tool reference from `prompts/tools_vN.md`: the own core
does not use the function calling of a platform, so a real model learns the
tools and their arguments from the prompt (T-2.4).
"""

from pathlib import Path

PROMPT_VERSION = "v2"
PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"
# Budget in estimated tokens (characters / 4) for everything the own core sends
# as system message on every request: system prompt, tool reference and answer
# format (CLAUDE.md §2 rule 6). `system_vN.md` alone keeps its own budget of
# 800 (docs/05 §5).
CORE_TOKEN_BUDGET = 2600


def build_system_prompt(
    menu_index: str | None = None,
    *,
    version: str = PROMPT_VERSION,
    tools: bool = False,
) -> str:
    prompt = (PROMPTS_DIR / f"system_{version}.md").read_text(encoding="utf-8").strip()
    if tools:
        prompt = f"{prompt}\n\n# Tools\n{tool_reference(version)}"
    if not menu_index:
        return prompt
    return f"{prompt}\n\n# Menü\n{menu_index}"


def tool_reference(version: str = PROMPT_VERSION) -> str:
    """The sections of `tools_vN.md`, one per tool. The file also serves the
    voice platform: its head (HTTP, bearer token) and its tail after the rule
    (tools that do not exist yet) are not for the model."""
    text = (PROMPTS_DIR / f"tools_{version}.md").read_text(encoding="utf-8")
    sections = text[text.index("\n## ") :]
    return sections.split("\n---\n")[0].strip()
