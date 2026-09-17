"""prompts/system_v1.md + tools_v1.md: Inhalt vollständig, Token-Budget eingehalten (T-1.10).

Reiner Inhaltstest ohne DB: die Dateien werden von agent/prompt.py erst ab T-2.1 geladen,
hier geht es nur darum, dass Version 1 vollständig und im Budget ist, bevor sie das tut.
"""

import math
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SYSTEM_PROMPT = REPO_ROOT / "prompts" / "system_v1.md"
TOOL_DESCRIPTIONS = REPO_ROOT / "prompts" / "tools_v1.md"

# Stufe 1 ist fertig gebaut mit genau diesen sechs Tools (T-1.3 bis T-1.9). Ein Tool, das
# hier fehlt, ist eines, das der Agent nicht nutzen kann; eines zu viel behauptet eine
# Fähigkeit, die es noch nicht gibt (CLAUDE.md §2 Regel 1: der Code entscheidet, nicht das Modell).
LIVE_TOOLS = (
    "get_service_status",
    "check_slot",
    "create_reservation",
    "confirm",
    "create_callback",
    "transfer_to_team",
)

# Grobe Näherung (Zeichen / 4), solange evals/ noch keinen echten Tokenizer mitbringt
# (T-5.1). docs/05 §5 setzt das Budget auf unter 800 Tokens für den System-Prompt.
TOKEN_BUDGET = 800


def _approx_tokens(text: str) -> int:
    return math.ceil(len(text) / 4)


def test_system_prompt_enthaelt_die_pflichtabschnitte():
    text = SYSTEM_PROMPT.read_text(encoding="utf-8")

    for heading in (
        "# Rolle",
        "# Pflicht zu Gesprächsbeginn",
        "# Harte Regeln",
        "# Ablauf",
    ):
        assert heading in text, f"Abschnitt {heading!r} fehlt in system_v1.md"


def test_system_prompt_nennt_jedes_gebaute_tool():
    text = SYSTEM_PROMPT.read_text(encoding="utf-8")

    fehlend = [tool for tool in LIVE_TOOLS if tool not in text]
    assert not fehlend, f"system_v1.md erwähnt diese gebauten Tools nicht: {fehlend}"


def test_system_prompt_haelt_das_token_budget():
    text = SYSTEM_PROMPT.read_text(encoding="utf-8")

    tokens = _approx_tokens(text)
    assert tokens < TOKEN_BUDGET, (
        f"~{tokens} Tokens, Budget ist {TOKEN_BUDGET} (docs/05 §5)"
    )


def test_system_prompt_erwaehnt_noch_nicht_gebaute_tools_nicht():
    """Menü und Bestellung existieren erst ab Stufe 2 (T-4.x) — ein Prompt, der sie schon
    erwähnt, verspricht dem Gast eine Fähigkeit, die der Code nicht hat."""
    text = SYSTEM_PROMPT.read_text(encoding="utf-8")

    for tool in ("search_menu", "draft_order", "get_item_details", "check_delivery"):
        assert tool not in text, (
            f"system_v1.md erwähnt {tool!r}, das ist erst Stufe 2/3"
        )


def test_tool_beschreibungen_nennen_jedes_gebaute_tool_mit_ueberschrift():
    text = TOOL_DESCRIPTIONS.read_text(encoding="utf-8")

    fehlend = [tool for tool in LIVE_TOOLS if f"## {tool}" not in text]
    assert not fehlend, f"tools_v1.md hat keine Überschrift für: {fehlend}"


def test_tool_beschreibungen_markieren_zukuenftige_tools_getrennt():
    text = TOOL_DESCRIPTIONS.read_text(encoding="utf-8")

    assert "Noch nicht verfügbar" in text
    for tool in ("search_menu", "draft_order", "get_item_details", "check_delivery"):
        assert f"## {tool}" not in text, (
            f"{tool!r} hat noch keine eigene Tool-Überschrift"
        )
