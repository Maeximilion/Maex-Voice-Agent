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
# Pfade, die niemals in den Container gehoeren: die Umgebungsdatei und alles unter
# .claude/ - dort liegen im Haupt-Checkout die Worktrees samt eigener .env.
GESCHUETZT = (".env", ".claude")


def _quelle_und_ziel(entry: str | dict) -> tuple[str | None, str]:
    """Host-Pfad und Container-Ziel eines Volume-Eintrags, oder None bei einem Volume.

    Die Unterscheidung laeuft ueber die Form, nicht ueber das Praefix `./`: ein
    benanntes Volume ist ein blosser Name ohne Trennzeichen (`pgdata:/var/...`),
    alles andere ist ein Bind und muss geprueft werden - auch `../nachbar/.env`
    und absolute Pfade, die ohne diese Regel unbesehen durchgingen.
    """
    if isinstance(entry, dict):  # lange Compose-Schreibweise
        if entry.get("type", "bind") != "bind":
            return None, ""
        return entry.get("source"), entry.get("target", "")
    praefix, rest = "", entry
    if WINDOWS_ABS_RE.match(entry):  # C:\... - der Doppelpunkt gehoert zum Laufwerk
        praefix, rest = entry[:2], entry[2:]
    kopf, _, schwanz = rest.partition(":")
    if not praefix and NAMED_VOLUME_RE.match(kopf):
        return None, ""
    return praefix + kopf, schwanz.split(":", 1)[0]


def _binds() -> set[tuple[str, str]]:
    """Alle Binds des api-Service als (normalisierte Quelle, Ziel).

    Normalisiert, weil Docker den Pfad aufloest und jede Pruefung darunter sonst rein
    lexikalisch waere: `./.claude/commands/../../.env` sieht wie ein Nachfahre von
    `commands/` aus und ist in Wahrheit die `.env` im Repo-Wurzelverzeichnis.
    """
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    binds = set()
    for entry in compose["services"]["api"]["volumes"]:
        host, ziel = _quelle_und_ziel(entry)
        if host:
            binds.add((posixpath.normpath(host.replace("\\", "/")).rstrip("/"), ziel))
    return binds


def _bind_quellen() -> set[str]:
    return {quelle for quelle, _ in _binds()}


def _nicht_aufloesbar(quelle: str) -> bool:
    """`${PWD}` und `$HOME` loest Compose auf, `yaml.safe_load` nicht.

    Was hier als Variable ankommt, kann nach der Aufloesung auf alles zeigen - auch
    auf die Repo-Wurzel. Ungeprueft durchlassen waere die groesste der Luecken.
    """
    return "$" in quelle


def _zeigt_aus_dem_repo(quelle: str) -> bool:
    """Absolut oder oberhalb der Wurzel - beides ist von hier aus nicht pruefbar."""
    return quelle.startswith("/") or bool(
        WINDOWS_ABS_RE.match(quelle) or PurePosixPath(quelle).parts[:1] == ("..",)
    )


def _api_mounts() -> set[str]:
    """Die Repo-Pfade, die im Container wirklich unter `/app/<pfad>` liegen.

    Das Ziel gehoert zur Pruefung: `./deploy:/tmp/deploy` mountet zwar `deploy`,
    aber nicht dorthin, wo die Suite es sucht (REPO_ROOT ist `/app`). Ohne den
    Abgleich haette ein geaendertes Ziel als Abdeckung gezaehlt.
    """
    return {
        quelle
        for quelle, ziel in _binds()
        if not _zeigt_aus_dem_repo(quelle)
        and not _nicht_aufloesbar(quelle)
        and ziel == f"/app/{quelle}"
    }


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


def _teile(pfad: str) -> tuple[str, ...]:
    return PurePosixPath(pfad).parts if pfad not in (".", "") else ()


def _ist_unter(quelle: str, ordner: str) -> bool:
    teile, oben = _teile(quelle), _teile(ordner)
    return teile[: len(oben)] == oben


def _beruehrt(quelle: str, geschuetzt: str) -> bool:
    """Wahr, wenn einer der beiden Pfade im anderen liegt - in welcher Richtung auch immer.

    Die Richtung nach oben ist die, die leicht vergessen wird: `.` ist der
    gemeinsame Vorfahr von allem und traegt damit `.env` und die Worktrees
    genauso herein wie ein Mount, der direkt auf sie zeigt.
    """
    teile, geschuetzte = _teile(quelle), _teile(geschuetzt)
    tiefe = min(len(teile), len(geschuetzte))
    return teile[:tiefe] == geschuetzte[:tiefe]


def _traegt_geheimnisse(mount: str) -> bool:
    """Wahr, sobald der Mount einen geschuetzten Pfad beruehrt.

    Geschuetzt sind `.env` und `.claude/` - dort liegen im Haupt-Checkout die
    Worktrees mit ihren eigenen `.env`-Dateien. Geprueft wird die Beruehrung in
    beide Richtungen, nicht die Gleichheit: `.claude/worktrees` zeigt hinein,
    `.` umfasst alles, und beides traegt dieselben Geheimnisse herein.

    Einzige Ausnahme ist `.claude/commands`, das die Testsuite wirklich liest.
    Was aus dem Repo herauszeigt, ist ohnehin tabu: dort liegen die
    Nachbar-Checkouts, und absolute Pfade sind von hier aus nicht pruefbar.
    """
    if _zeigt_aus_dem_repo(mount) or _nicht_aufloesbar(mount):
        return True
    if PurePosixPath(mount).name == ".env":  # auch tiefer liegende .env
        return True
    if _ist_unter(mount, ".claude/commands"):  # nach der .env-Pruefung, nicht davor
        return False
    return any(_beruehrt(mount, pfad) for pfad in GESCHUETZT)


def _versteckte_env(quelle: str) -> list[str]:
    """Jede `.env`, die in einem gemounteten Verzeichnis tatsaechlich liegt.

    Der Pfadvergleich allein reicht hier nicht: `.gitignore` faengt `.env` in jeder
    Tiefe ab, eine solche Datei existiert also lokal, ohne je in Git oder CI
    aufzutauchen. Ein Verzeichnis-Mount traegt sie trotzdem in den Container.
    Diese Pruefung sieht deshalb als einzige aufs Dateisystem - sie schlaegt genau
    dort an, wo ein Geheimnis wirklich liegt, und bleibt sonst still.
    """
    ordner = REPO_ROOT / quelle
    if not ordner.is_dir():
        return []
    return sorted(p.relative_to(REPO_ROOT).as_posix() for p in ordner.rglob(".env"))


def test_geheimnisse_bleiben_draussen():
    """CLAUDE.md §8: .env und die Worktrees darunter gehoeren nicht in den Container."""
    verboten = sorted(q for q in _bind_quellen() if _traegt_geheimnisse(q))
    verboten += [
        f"{quelle} enthaelt {datei}"
        for quelle in sorted(_api_mounts())
        for datei in _versteckte_env(quelle)
    ]
    assert not verboten, f"Mount traegt fremde Geheimnisse in den Container: {verboten}"
