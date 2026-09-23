"""prompts/system_v2.md + tools_v2.md: Menue-Tools und Abholung, Token-Budget eingehalten.

Die Liste der Tools kommt aus agent/dispatch.TOOLS, nicht aus einer Kopie: ein
Tool, das der Agent aufrufen kann, muss im Prompt stehen, und der Prompt darf
kein Tool versprechen, das es nicht gibt (CLAUDE.md §2 Regel 1).
"""

import math
from pathlib import Path

from api.agent.dispatch import TOOLS
from api.agent.prompt import PROMPT_VERSION

REPO_ROOT = Path(__file__).resolve().parents[2]
SYSTEM_PROMPT = REPO_ROOT / "prompts" / "system_v2.md"
TOOL_DESCRIPTIONS = REPO_ROOT / "prompts" / "tools_v2.md"
TOKEN_BUDGET = 800
NOT_YET = ("check_delivery", "find_customer")


def _approx_tokens(text: str) -> int:
    return math.ceil(len(text) / 4)


def test_v2_ist_die_aktuelle_version():
    assert PROMPT_VERSION == "v2"


def test_system_prompt_enthaelt_die_pflichtabschnitte():
    text = SYSTEM_PROMPT.read_text(encoding="utf-8")
    for heading in (
        "# Rolle",
        "# Pflicht zu Gesprächsbeginn",
        "# Harte Regeln",
        "# Ablauf",
    ):
        assert heading in text, f"Abschnitt {heading!r} fehlt in system_v2.md"


def test_system_prompt_nennt_jedes_tool_des_agenten():
    text = SYSTEM_PROMPT.read_text(encoding="utf-8")
    fehlend = [tool for tool in TOOLS if tool not in text]
    assert not fehlend, f"system_v2.md erwähnt diese Tools nicht: {fehlend}"


def test_tool_beschreibungen_haben_je_tool_eine_ueberschrift():
    text = TOOL_DESCRIPTIONS.read_text(encoding="utf-8")
    fehlend = [tool for tool in TOOLS if f"## {tool}" not in text]
    assert not fehlend, f"tools_v2.md hat keine Überschrift für: {fehlend}"


def test_noch_nicht_gebaute_tools_haben_keine_ueberschrift():
    text = TOOL_DESCRIPTIONS.read_text(encoding="utf-8")
    for tool in NOT_YET:
        assert f"## {tool}" not in text
        assert tool not in SYSTEM_PROMPT.read_text(encoding="utf-8")


def test_system_prompt_haelt_das_token_budget():
    tokens = _approx_tokens(SYSTEM_PROMPT.read_text(encoding="utf-8"))
    assert tokens < TOKEN_BUDGET, f"~{tokens} Tokens, Budget ist {TOKEN_BUDGET}"


def test_harte_regeln_der_bestellung_stehen_drin():
    """Die zwei Regeln, an denen eine falsche Bestellung haengt (CLAUDE.md §2)."""
    text = SYSTEM_PROMPT.read_text(encoding="utf-8")
    assert "menu_item_id" in text and "ambiguous" in text
    assert "readback" in text and "confirm" in text


def test_korrektur_nach_dem_vorlesen_je_ablauf():
    """Codex PR #127, P1: eine korrigierte Reservierung geht nicht ueber
    draft_order - das scheitert an der Pruefung, der alte Entwurf bliebe
    readback_pending, und das naechste Ja bestaetigte ihn. Jeder Ablauf nennt
    sein eigenes Tool fuer die Korrektur."""
    text = SYSTEM_PROMPT.read_text(encoding="utf-8")
    regel = next(z for z in text.splitlines() if "Ändert der Gast" in z)
    assert "`check_slot`" in regel and "`create_reservation`" in regel
    assert "`draft_order`" in regel
    tools = TOOL_DESCRIPTIONS.read_text(encoding="utf-8")
    reservierung = tools.split("## create_reservation")[1].split("## ")[0]
    assert "Änderung" in reservierung
