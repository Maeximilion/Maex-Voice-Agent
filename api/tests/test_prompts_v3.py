"""prompts/system_v3.md + tools_v3.md: Menue-Tools, Abholung, Lieferung, Token-Budget eingehalten.

Die Liste der Tools kommt aus agent/dispatch.TOOLS, nicht aus einer Kopie: ein
Tool, das der Agent aufrufen kann, muss im Prompt stehen, und der Prompt darf
kein Tool versprechen, das es nicht gibt (CLAUDE.md §2 Regel 1).
"""

import math
from pathlib import Path

from api.agent.dispatch import TOOLS
from api.agent.prompt import PROMPT_VERSION

REPO_ROOT = Path(__file__).resolve().parents[2]
SYSTEM_PROMPT = REPO_ROOT / "prompts" / "system_v3.md"
TOOL_DESCRIPTIONS = REPO_ROOT / "prompts" / "tools_v3.md"
TOKEN_BUDGET = 800


def _approx_tokens(text: str) -> int:
    return math.ceil(len(text) / 4)


def test_v3_ist_die_aktuelle_version():
    assert PROMPT_VERSION == "v3"


def test_system_prompt_enthaelt_die_pflichtabschnitte():
    text = SYSTEM_PROMPT.read_text(encoding="utf-8")
    for heading in (
        "# Rolle",
        "# Pflicht zu Gesprächsbeginn",
        "# Harte Regeln",
        "# Ablauf",
    ):
        assert heading in text, f"Abschnitt {heading!r} fehlt in system_v3.md"


def test_system_prompt_nennt_jedes_tool_des_agenten():
    text = SYSTEM_PROMPT.read_text(encoding="utf-8")
    fehlend = [tool for tool in TOOLS if tool not in text]
    assert not fehlend, f"system_v3.md erwähnt diese Tools nicht: {fehlend}"


def test_tool_beschreibungen_haben_je_tool_eine_ueberschrift():
    text = TOOL_DESCRIPTIONS.read_text(encoding="utf-8")
    fehlend = [tool for tool in TOOLS if f"## {tool}" not in text]
    assert not fehlend, f"tools_v3.md hat keine Überschrift für: {fehlend}"


def test_lieferung_steht_im_prompt():
    """T-6.5: delivery is offered, and no line still says it is not."""
    text = SYSTEM_PROMPT.read_text(encoding="utf-8")
    assert "Lieferung kannst du noch nicht" not in text
    regel = next(z for z in text.splitlines() if z.startswith("4. Lieferung"))
    assert "`find_customer`" in regel
    flow = text.split("4. Lieferung")[1]
    for wort in (
        "`address_id`",
        "`check_delivery`",
        "`below_minimum`",
        "`out_of_zone`",
    ):
        assert wort in flow


def test_gespeicherte_daten_nennt_das_modell_nie():
    """L12: a prompt rule next to the code guard, and an objection goes to the
    team, which enters it (docs/09)."""
    text = SYSTEM_PROMPT.read_text(encoding="utf-8")
    regel = next(z for z in text.splitlines() if z.startswith("- Gespeicherte"))
    assert "nennst du nie" in regel
    assert "Widerspruch" in next(z for z in text.splitlines() if "Beschwerde" in z)


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


def test_wuensche_und_aufpreis_nur_aus_der_karte():
    """T-4.10, D8: der Wunsch kommt aus search_menu, der Grund fuer einen Aufpreis
    nur aus `reason` - das Modell erklaert und verhandelt keine Preise."""
    text = SYSTEM_PROMPT.read_text(encoding="utf-8")
    regel = next(z for z in text.splitlines() if z.startswith("- Wünsche:"))
    for wort in ("`wish`", "`options`", "`note`", "`unknown`", "`reason`", "Rabatt"):
        assert wort in regel, wort
    tools = TOOL_DESCRIPTIONS.read_text(encoding="utf-8")
    assert "`wish`" in tools.split("## search_menu")[1].split("## ")[0]
    assert "`reason`" in tools.split("## get_item_details")[1].split("## ")[0]


def test_allergenfrage_und_eigene_allergie_getrennt():
    """Codex PR #139: die Frage nach Allergenen eines Gerichts geht an
    get_item_details, die eigene Allergie des Gastes kommt als Hinweis aus
    search_menu - die Regeln widersprechen sich nicht."""
    text = SYSTEM_PROMPT.read_text(encoding="utf-8")
    regel = next(z for z in text.splitlines() if z.startswith("- Allergien:"))
    assert "Frage" in regel and "eigene Allergie" in regel


def test_out_of_zone_offers_pickup_only_through_its_say():
    """#684: the prompt offered pickup after `out_of_zone` without a condition,
    though the code leaves the offer out of the `say` when pickup is closed."""
    text = SYSTEM_PROMPT.read_text(encoding="utf-8")
    line = next(line for line in text.splitlines() if "`out_of_zone`" in line)
    assert "Abholung anbieten" not in line
    assert "nur `say`" in line
