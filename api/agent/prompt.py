"""System-Prompt aus `prompts/system_vN.md` plus Menü-Index bauen (docs/05 §1, §5).

Version 2 kennt die Menü-Tools und die Abholung. Der Menü-Index (Nummer und Name,
nie die ganze Karte, CLAUDE.md §2 Regel 6) wird angehängt, sobald ein Aufrufer ihn
mitgibt.
"""

from pathlib import Path

PROMPT_VERSION = "v2"
PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"


def build_system_prompt(
    menu_index: str | None = None, *, version: str = PROMPT_VERSION
) -> str:
    base = (PROMPTS_DIR / f"system_{version}.md").read_text(encoding="utf-8").strip()
    if not menu_index:
        return base
    return f"{base}\n\n# Menü\n{menu_index}"
