"""System-Prompt aus `prompts/system_vN.md` plus Menü-Index bauen (docs/05 §1, §5).

Der Menü-Index kommt erst mit `domain/menu` (Stufe 2, T-4.x); bis dahin bleibt der
Parameter ungenutzt, aber Teil der Signatur, damit `agent/loop.py` sich später nicht
ändern muss.
"""

from pathlib import Path

PROMPT_VERSION = "v1"
PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"


def build_system_prompt(
    menu_index: str | None = None, *, version: str = PROMPT_VERSION
) -> str:
    base = (PROMPTS_DIR / f"system_{version}.md").read_text(encoding="utf-8").strip()
    if not menu_index:
        return base
    return f"{base}\n\n# Menü\n{menu_index}"
