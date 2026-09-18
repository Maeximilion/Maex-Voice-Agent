"""`.claude/commands/*.md`: die Slash-Befehle zeigen nur auf Dinge, die es wirklich gibt (T-0.7).

Ein Slash-Befehl ist eine Anweisung an Claude Code, keine ausfuehrbare Datei - ein falscher
Pfad darin faellt erst mitten in der Sitzung auf, wenn der Befehl schon laeuft. Dieser Test
prueft deshalb statisch, was sich statisch pruefen laesst: Vollstaendigkeit der Befehle,
existierende Pfade, existierende Make-Ziele, keine Emojis (CLAUDE.md §8).
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
COMMAND_DIR = REPO_ROOT / ".claude" / "commands"
MAKEFILE = REPO_ROOT / "Makefile"

# CLAUDE.md §6 nennt genau diese sieben Befehle.
EXPECTED_COMMANDS = ("start", "task", "done", "bug", "eval", "gate", "handover")

# Dateiverweise: alles, was wie ein Repo-Pfad mit bekannter Endung aussieht.
FILE_REF_RE = re.compile(r"[A-Za-z0-9_.\-/]+\.(?:md|py|sh|yml|yaml|json)")
# Verzeichnisverweise nur in Backticks, sonst faengt das Muster halbe Saetze ein.
DIR_REF_RE = re.compile(r"`([A-Za-z0-9_.\-]+(?:/[A-Za-z0-9_.\-]+)*/)`")
MAKE_TARGET_RE = re.compile(r"`make ([a-z-]+)`")
# Verzeichnisse, die erst zur Laufzeit entstehen und deshalb nicht im Repo liegen
# (CLAUDE.md §4: evals/reports/ ist ignoriert).
GENERATED_DIRS = frozenset({"evals/reports/"})
EMOJI_RE = re.compile("[\U0001f300-\U0001faff☀-➿]")


def _command_files() -> list[Path]:
    return sorted(COMMAND_DIR.glob("*.md"))


def _make_targets() -> set[str]:
    return {
        match.group(1)
        for match in re.finditer(
            r"^([a-z-]+):", MAKEFILE.read_text(encoding="utf-8"), re.MULTILINE
        )
    }


def test_alle_befehle_aus_claude_md_existieren():
    vorhanden = {path.stem for path in _command_files()}
    assert set(EXPECTED_COMMANDS) <= vorhanden, sorted(
        set(EXPECTED_COMMANDS) - vorhanden
    )


def test_verwiesene_dateien_existieren():
    fehlend: list[str] = []
    for path in _command_files():
        text = path.read_text(encoding="utf-8")
        for ref in sorted(set(FILE_REF_RE.findall(text))):
            if not (REPO_ROOT / ref).exists():
                fehlend.append(f"{path.name}: {ref}")
    assert not fehlend, fehlend


def test_verwiesene_verzeichnisse_existieren():
    fehlend = [
        f"{path.name}: {ref}"
        for path in _command_files()
        for ref in DIR_REF_RE.findall(path.read_text(encoding="utf-8"))
        if ref not in GENERATED_DIRS and not (REPO_ROOT / ref).is_dir()
    ]
    assert not fehlend, fehlend


def test_verwiesene_make_ziele_existieren():
    ziele = _make_targets()
    fehlend = [
        f"{path.name}: make {ref}"
        for path in _command_files()
        for ref in MAKE_TARGET_RE.findall(path.read_text(encoding="utf-8"))
        if ref not in ziele
    ]
    assert not fehlend, fehlend


def test_befehle_enthalten_keine_emojis():
    treffer = [
        f"{path.name}: {EMOJI_RE.findall(path.read_text(encoding='utf-8'))}"
        for path in _command_files()
        if EMOJI_RE.search(path.read_text(encoding="utf-8"))
    ]
    assert not treffer, treffer
