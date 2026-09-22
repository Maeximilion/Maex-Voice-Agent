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

import posixpath
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
# Ein benanntes Volume ist ein blosser Name: keine Trennzeichen, kein Punkt-Praefix.
NAMED_VOLUME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
WINDOWS_ABS_RE = re.compile(r"^[A-Za-z]:[\\/]")


def _quelle(entry: str | dict) -> str | None:
    """Der Host-Pfad eines Volume-Eintrags, oder None bei einem benannten Volume.

    Die Unterscheidung laeuft ueber die Form, nicht ueber das Praefix `./`: ein
    benanntes Volume ist ein blosser Name ohne Trennzeichen (`pgdata:/var/...`),
    alles andere ist ein Bind und muss geprueft werden - auch `../nachbar/.env`
    und absolute Pfade, die ohne diese Regel unbesehen durchgingen.
    """
    if isinstance(entry, dict):  # lange Compose-Schreibweise
        return entry.get("source") if entry.get("type", "bind") == "bind" else None
    if WINDOWS_ABS_RE.match(entry):  # C:\... - der Doppelpunkt gehoert zum Laufwerk
        return entry[:2] + entry[2:].partition(":")[0]
    kopf = entry.split(":", 1)[0]
    return None if NAMED_VOLUME_RE.match(kopf) else kopf


def _bind_quellen() -> set[str]:
    """Alle Bind-Quellen des api-Service, normalisiert.

    Normalisiert, weil Docker den Pfad aufloest und jede Pruefung darunter sonst rein
    lexikalisch waere: `./.claude/commands/../../.env` sieht wie ein Nachfahre von
    `commands/` aus und ist in Wahrheit die `.env` im Repo-Wurzelverzeichnis.
    """
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    quellen = set()
    for entry in compose["services"]["api"]["volumes"]:
        host = _quelle(entry)
        if host:
            quellen.add(posixpath.normpath(host.replace("\\", "/")).rstrip("/"))
    return quellen


def _zeigt_aus_dem_repo(quelle: str) -> bool:
    """Absolut oder oberhalb der Wurzel - beides ist von hier aus nicht pruefbar."""
    return quelle.startswith("/") or bool(
        WINDOWS_ABS_RE.match(quelle) or PurePosixPath(quelle).parts[:1] == ("..",)
    )


def _api_mounts() -> set[str]:
    """Die Bind-Quellen, die tatsaechlich im Repo liegen - nur die decken Pfade ab."""
    return {q for q in _bind_quellen() if not _zeigt_aus_dem_repo(q)}


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

    Die Pfade kommen normalisiert aus `_bind_quellen()`. Was aus dem Repo
    herauszeigt, ist erst recht tabu: dort liegen die Nachbar-Checkouts, und was
    ein absoluter Pfad enthaelt, laesst sich von hier aus gar nicht pruefen.
    """
    if _zeigt_aus_dem_repo(mount):
        return True
    teile = PurePosixPath(mount).parts
    if teile[:1] == (".claude",):
        return teile[:2] != (".claude", "commands")
    return PurePosixPath(mount).name == ".env"


def test_geheimnisse_bleiben_draussen():
    """CLAUDE.md §8: .env und die Worktrees darunter gehoeren nicht in den Container."""
    verboten = sorted(q for q in _bind_quellen() if _traegt_geheimnisse(q))
    assert not verboten, f"Mount traegt fremde Geheimnisse in den Container: {verboten}"
