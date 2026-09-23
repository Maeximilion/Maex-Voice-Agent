"""agent/prompt.py: liest prompts/system_vN.md und hängt einen Menü-Index an, wenn einer da ist."""

from pathlib import Path

from api.agent.prompt import PROMPT_VERSION, build_system_prompt

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
