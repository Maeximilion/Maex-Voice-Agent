"""`docker-compose.yml`: der api-Service traegt jeden Pfad, den die Suite liest.

Die Container-Mounts sind die einzige Stelle, an der `make test` von der CI abweichen
kann, ohne dass am Code etwas fehlt: die CI laeuft auf einem vollen Checkout, der
Container sieht nur, was gemountet ist. Fehlt ein Pfad, faellt pytest lokal mit
FileNotFoundError aus, waehrend die CI gruen bleibt - der Fehler zeigt sich also genau
dort, wo niemand hinsieht.

Dieser Test prueft statisch und ohne Docker, was die Suite an Repo-Pfaden anfasst:
jeden Dateiverweis aus den Slash-Befehlen und jeden Pfad, den ein Test ueber
`parents[2]` oder `REPO_ROOT` oeffnet. Jeder davon muss in einem Mount liegen.
"""

import re
from pathlib import PurePosixPath

import yaml

from api.tests.test_commands import (
    DIR_REF_RE,
    FILE_REF_RE,
    GENERATED_DIRS,
    REPO_ROOT,
    _command_files,
)

COMPOSE = REPO_ROOT / "docker-compose.yml"
# Verzeichnisse mit Python, das die Suite ausfuehrt - dort stehen die Pfadzugriffe.
SOURCE_DIRS = ("api", "evals", "sim", "scripts")
# `... parents[2] / "deploy" / "Caddyfile"` wird als "deploy/Caddyfile" gelesen: die
# ganze Kette, nicht nur das erste Segment - sonst faende der Test `.claude/commands`
# nicht wieder, das genau deshalb enger gemountet ist als `.claude`.
ROOT_PATH_RE = re.compile(r'(?:parents\[2\]|REPO_ROOT)((?:\s*/\s*"[^"]+")+)')
SEGMENT_RE = re.compile(r'"([^"]+)"')


def _api_mounts() -> set[str]:
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    mounts = set()
    for entry in compose["services"]["api"]["volumes"]:
        host = entry.split(":", 1)[0]
        if host.startswith("./"):
            mounts.add(host[2:].rstrip("/"))
    return mounts


def _is_mounted(ref: str, mounts: set[str]) -> bool:
    """Ein Pfad ist abgedeckt, wenn er selbst oder eines seiner Elternteile gemountet ist."""
    parts = PurePosixPath(ref.strip("/")).parts
    return any("/".join(parts[:tiefe]) in mounts for tiefe in range(1, len(parts) + 1))


def _refs_aus_befehlen() -> set[str]:
    refs: set[str] = set()
    for path in _command_files():
        text = path.read_text(encoding="utf-8")
        refs |= set(FILE_REF_RE.findall(text))
        refs |= {ref for ref in DIR_REF_RE.findall(text) if ref not in GENERATED_DIRS}
    return refs


def _refs_aus_tests() -> set[str]:
    refs: set[str] = set()
    for verzeichnis in SOURCE_DIRS:
        for datei in (REPO_ROOT / verzeichnis).rglob("*.py"):
            for kette in ROOT_PATH_RE.findall(datei.read_text(encoding="utf-8")):
                refs.add("/".join(SEGMENT_RE.findall(kette)))
    return refs


def test_slash_befehle_verweisen_nur_auf_gemountete_pfade():
    mounts = _api_mounts()
    fehlend = sorted(
        ref for ref in _refs_aus_befehlen() if not _is_mounted(ref, mounts)
    )
    assert not fehlend, (
        f"von .claude/commands verwiesen, aber im api-Container nicht gemountet: {fehlend}"
    )


def test_tests_lesen_nur_gemountete_pfade():
    mounts = _api_mounts()
    fehlend = sorted(ref for ref in _refs_aus_tests() if not _is_mounted(ref, mounts))
    assert not fehlend, (
        f"ueber REPO_ROOT geoeffnet, aber im api-Container nicht gemountet: {fehlend}"
    )


def test_die_compose_datei_mountet_sich_selbst():
    """Ohne diesen Mount findet dieser Test die Datei im Container nicht."""
    assert _is_mounted("docker-compose.yml", _api_mounts())


def test_dispatcher_braucht_die_testpfade_nicht():
    """Der dispatcher fuehrt weder pytest noch ruff aus - seine Mounts bleiben klein."""
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    hosts = {
        entry.split(":", 1)[0] for entry in compose["services"]["dispatcher"]["volumes"]
    }
    assert hosts == {"./api", "./scripts", "./db"}


def _traegt_geheimnisse(mount: str) -> bool:
    """Alles unter `.claude/` ausser den Befehlen ist tabu, ebenso jede .env.

    Als Positivliste und nicht als Verbotsliste: unter `.claude/` liegen im
    Haupt-Checkout die Worktrees mit eigenen `.env`-Dateien, und ein Mount von
    `.claude/worktrees` oder einem einzelnen Worktree darunter waere genauso
    falsch wie `.claude` selbst - nur faellt er einer Verbotsliste nicht auf.
    """
    teile = PurePosixPath(mount).parts
    if teile[:1] == (".claude",):
        return teile[:2] != (".claude", "commands")
    return PurePosixPath(mount).name == ".env"


def test_geheimnisse_bleiben_draussen():
    """CLAUDE.md §8: .env und die Worktrees darunter gehoeren nicht in den Container."""
    verboten = sorted(mount for mount in _api_mounts() if _traegt_geheimnisse(mount))
    assert not verboten, f"Mount traegt fremde Geheimnisse in den Container: {verboten}"
